"""Container sandbox: shell commands and language servers run in one Docker or Podman container per session,
which sees the workspace and the paths the user added, and nothing else from the host.

The container starts at the first command and stays up until jimbo exits, so a later command can reach a server
an earlier one started. Each process gets a JIMBO_RUN marker in its environment, which everything it starts
inherits; killing a process kills every process in the container with its marker. Ports are forwarded by jimbo
(`<cli> exec ... socat`), so they can be opened and closed while the container runs."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import __version__
from .sandbox import SandboxError, kill_group

if TYPE_CHECKING:
    from ..config import Settings

Engine = Literal["docker", "podman"]
IMAGE_DIR = Path(__file__).parent.parent / "bundled" / "sandbox"
IMAGE_NAME = "jimbo-sandbox"
HOME = "/home/jimbo"
CACHE_VOLUME = "jimbo-cache"
PID_LABEL = "jimbo.host-pid"
PIDS_LIMIT = 1024
STOP_EXIT = 137  # SIGKILL, which is also what the kernel's out-of-memory killer sends

# Kills every process whose environment has JIMBO_RUN=$1. Twice, for processes forked during the first pass.
_KILL_RUN = ('for _ in 1 2; do for p in /proc/[0-9]*; do tr "\\0" "\\n" < "$p/environ" 2>/dev/null '
             '| grep -qxF "JIMBO_RUN=$1" && kill -KILL "${p#/proc/}" 2>/dev/null; done; done; true')
_FIND = 'for c in "$@"; do command -v "$c" >/dev/null 2>&1 && echo "$c"; done; true'


MAX_MASK_SCAN = 50_000  # files looked at for secret_files when the container starts
MAX_MASKED = 200


@dataclass(frozen=True)
class Runtime:
    """The container CLI and what `probe` found out about the engine behind it."""
    cli: str = "docker"  # the command jimbo runs
    engine: Engine = "docker"  # what answers it: podman-docker makes `docker` run Podman
    rootless: bool = False
    selinux: bool = False
    limits_enforced: bool = True  # rootless Podman on cgroups v1 ignores --memory, --cpus and --pids-limit

    @property
    def title(self) -> str:
        return self.engine.capitalize()


class Unavailable(Exception):
    """The container CLI is missing or its engine doesn't answer."""


def bundled_image() -> str:
    return f"{IMAGE_NAME}:{__version__}"


def _cli(cli: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout)


def _first_sentence(text: str) -> str:
    line = next((x.strip() for x in text.splitlines() if x.strip()), "")
    return line.removeprefix("Error: ").split(". ")[0].rstrip(".")


def parse_info(cli: str, info: dict) -> Runtime:
    """The Runtime from `<cli> info --format '{{json .}}'`. Podman and Docker print different fields."""
    if isinstance(info.get("host"), dict):
        host = info["host"]
        sec = host.get("security") or {}
        rootless = bool(sec.get("rootless"))
        return Runtime(cli, "podman", rootless, bool(sec.get("selinuxEnabled")),
                       not (rootless and host.get("cgroupVersion") == "v1"))
    opts = info.get("SecurityOptions") or []
    return Runtime(cli, "docker", any("name=rootless" in o for o in opts), any("name=selinux" in o for o in opts))


def probe(cli: str = "docker", timeout: float = 10) -> Runtime:
    """What runs behind `cli`. Raises Unavailable when it is missing or doesn't answer in `timeout` seconds."""
    try:
        r = _cli(cli, "info", "--format", "{{json .}}", timeout=timeout)
    except FileNotFoundError:
        raise Unavailable(f"{cli} is not installed") from None
    except subprocess.TimeoutExpired:
        raise Unavailable(f"{cli} info didn't answer in {timeout:g}s") from None
    try:
        info = json.loads(r.stdout) if r.returncode == 0 else {}
    except json.JSONDecodeError:
        info = {}
    if errors := info.get("ServerErrors"):  # docker exits 0 when the daemon is down
        raise Unavailable(_first_sentence(errors[0]))
    if r.returncode != 0 or not info:
        raise Unavailable(_first_sentence(r.stderr) or f"{cli} info exited with code {r.returncode}")
    return parse_info(cli, info)


def unavailable(cli: str = "docker") -> str | None:
    """Why `cli` can't be used, or None if it can."""
    try:
        probe(cli)
    except Unavailable as e:
        return str(e)
    return None


def _advice(cli: str) -> str:
    if cli == "podman":
        return ('Run "podman machine init" once and then "podman machine start"' if sys.platform == "darwin"
                else "Start Podman")
    return "Start Docker"


def has_image(image: str, cli: str = "docker") -> bool:
    return _cli(cli, "image", "inspect", image).returncode == 0


def build_image(image: str, cli: str = "docker", out=sys.stderr) -> None:
    """Build the bundled Dockerfile as `image`, with the build output on `out`."""
    print(f"jimbo: building sandbox image {image} (first use, takes a few minutes)", file=out, flush=True)
    r = subprocess.run([cli, "build", "-t", image, str(IMAGE_DIR)], stdout=out, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise ValueError(f"building sandbox image {image} failed ({cli} build exited with code {r.returncode})")


def pull_image(image: str, cli: str = "docker", out=sys.stderr) -> None:
    print(f"jimbo: pulling sandbox image {image}", file=out, flush=True)
    r = subprocess.run([cli, "pull", image], stdout=out, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise ValueError(f"pulling sandbox image {image} failed ({cli} pull exited with code {r.returncode})")


def ensure_image(image: str, cli: str = "docker", out=sys.stderr) -> None:
    if has_image(image, cli):
        return
    if image == bundled_image():
        build_image(image, cli, out)
    else:
        pull_image(image, cli, out)


def old_tags(tags: list[str], image: str) -> list[str]:
    """The tags `rebuild` removes. Podman names a local build localhost/<name>."""
    return [t for t in tags if t.removeprefix("localhost/") != image and not t.endswith(":<none>")]


def rebuild(cli: str = "docker", out=sys.stdout) -> None:
    """`jimbo sandbox build`: build the bundled image again and remove older jimbo-sandbox tags."""
    try:
        rt = probe(cli)
    except Unavailable as e:
        raise ValueError(f"{cli} is not available: {e}") from None
    image = bundled_image()
    build_image(image, cli, out)
    tags = _cli(cli, "image", "ls", IMAGE_NAME, "--format", "{{.Repository}}:{{.Tag}}").stdout.split()
    for old in old_tags(tags, image):
        _cli(cli, "image", "rm", old)
        print(f"removed {old}", file=out)
    if rt.engine != cli:
        print(f"note: {cli} runs {rt.title}", file=out)


def remove_stale(cli: str = "docker") -> None:
    """Remove containers left by jimbo processes that have exited, for example after a crash."""
    r = _cli(cli, "ps", "-a", "--filter", f"label={PID_LABEL}", "--format",
             f'{{{{.Names}}}} {{{{.Label "{PID_LABEL}"}}}}')
    for line in r.stdout.splitlines():
        name, _, pid = line.partition(" ")
        if pid.isdigit() and not _alive(int(pid)):
            _cli(cli, "rm", "-f", name)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_commands(image: str, names: list[str], cli: str = "docker") -> set[str]:
    """Which of `names` are on PATH in `image`, from one throwaway container."""
    try:
        r = _cli(cli, "run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c", _FIND, "sh", *names,
                 timeout=60)
    except subprocess.TimeoutExpired:
        return set()
    return set(r.stdout.split()) if r.returncode == 0 else set()


def parse_port(spec: int | str) -> tuple[int, int]:
    """5173 or "8080:5173" (host:container) -> (host port, container port)."""
    host, _, container = str(spec).strip().partition(":")
    try:
        ports = int(host), int(container or host)
    except ValueError:
        raise ValueError(f"bad port {spec!r}: use 5173 or \"8080:5173\" (host:container)") from None
    if not all(0 < p < 65536 for p in ports):
        raise ValueError(f"bad port {spec!r}: ports are 1 to 65535")
    return ports


def control_paths() -> list[Path]:
    """Sockets and keys that let a process start containers, for Docker and Podman alike."""
    home = Path.home()
    paths = [Path("/var/run/docker.sock"), Path("/run/podman/podman.sock"),
             home / ".local" / "share" / "containers" / "podman" / "machine"]  # the machine's ssh key
    for var in ("DOCKER_HOST", "CONTAINER_HOST"):
        if (host := os.environ.get(var, "")).startswith("unix://"):
            paths.append(Path(host.removeprefix("unix://")))
    if runtime_dir := os.environ.get("XDG_RUNTIME_DIR"):
        paths.append(Path(runtime_dir) / "podman" / "podman.sock")
    if sys.platform == "darwin":  # podman machine's API socket, $TMPDIR/podman/<machine>-api.sock
        paths.append(Path(os.environ.get("TMPDIR", "/tmp")) / "podman")
    return [p.resolve() for p in paths if p.exists()] + paths


def check_mounts(paths: list[Path]) -> None:
    home = Path.home().resolve()
    for p in paths:
        if p == Path("/") or p == home:
            raise ValueError(f"the container sandbox won't mount {p}: mount the folders you need instead")
        if any(s == p or s.is_relative_to(p) for s in control_paths()):
            raise ValueError(f"the container sandbox won't mount {p}: it holds the container socket, and a "
                             "command with the socket can start a container that mounts anything")


def unshared(error: str) -> str | None:
    """The host path in a Podman machine `statfs <path>: no such file or directory` error, if it exists here:
    the path is on the Mac but not shared with the VM."""
    marker = "statfs "
    if marker not in error or "no such file or directory" not in error:
        return None
    path = error.split(marker, 1)[1].split(": no such file", 1)[0].strip()
    return path if Path(path).exists() else None


class _Forward:
    def __init__(self, host: int, container: int) -> None:
        self.host, self.container = host, container
        self.server: asyncio.Server | None = None
        self.conns: set[asyncio.Task] = set()


class ContainerSandbox:
    container = True

    def __init__(self, image: str, workspace: Path, *, runtime: Runtime = Runtime(), writable: list[Path] = (), readonly: list[Path] = (),
                 network: bool = True, ports: list[tuple[int, int]] = (), env: dict[str, str] | None = None,
                 memory: str = "", cpus: float = 0, git_write: bool = True, secret_files: list[str] = (),
                 protected: list[str] = ()) -> None:
        """`git_write` False: the workspace's git folders are mounted read-only. True: only .git/hooks and
        .git/config are. `secret_files`: workspace files with these names, as they are when the container starts,
        are covered with an empty file."""
        self.image = image
        self.git_write = git_write
        self.secret_files = list(secret_files)
        self.protected = list(protected)
        self.runtime = runtime
        self.cli = runtime.cli
        self.kind: Engine = runtime.engine
        self.workspace = workspace
        self.writable = [p.expanduser().resolve() for p in writable]
        self.readonly = [p.expanduser().resolve() for p in readonly]
        check_mounts([workspace, *self.writable, *self.readonly])
        self.network = network
        self.ports = list(ports)  # opened when the container starts
        self.env = dict(env or {})
        self.memory, self.cpus = memory, cpus
        self.name = f"jimbo-{secrets.token_hex(3)}"
        self.shell = "bash"
        self.started_at: float | None = None
        self.notes: list[str] = []  # problems opening sandbox_ports, shown by /sandbox
        self.forwards: dict[int, _Forward] = {}
        self._runs: dict[int, str] = {}  # host pid of an exec -> its JIMBO_RUN marker
        self._watch: asyncio.subprocess.Process | None = None  # `<cli> wait`, which exits with the container
        self._lock: asyncio.Lock | None = None

    # ---- the container ----

    def locked(self) -> list[Path]:
        """Existing paths in the workspace's git folders that are mounted read-only over the workspace."""
        from .sandbox import git_dirs
        dirs = git_dirs(self.workspace)
        paths = [p for d in dirs for p in (d / "hooks", d / "config")] if self.git_write else dirs
        return [p.resolve() for p in [*paths, *self.protected_found()] if p.exists()]

    def protected_found(self) -> list[Path]:
        """Existing workspace paths matching `protected`: a folder for `dir/**`, and matching files."""
        if not self.protected:
            return []
        from .search import SKIP_DIRS, glob_to_regex
        found = [self.workspace / g[:-3] for g in self.protected if g.endswith("/**") and "*" not in g[:-3]]
        rxs = [glob_to_regex(g) for g in self.protected]
        seen = 0
        for top, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                rel = Path(top, f).relative_to(self.workspace).as_posix()
                if any(rx.fullmatch(rel) for rx in rxs) and not any(Path(top, f).is_relative_to(d) for d in found):
                    found.append(Path(top, f))
            seen += len(files)
            if seen > MAX_MASK_SCAN:
                break
        return [p for p in found if not (p.is_relative_to(self.workspace / ".git"))]

    def masked(self) -> list[Path]:
        """Files in the workspace matching `secret_files`, covered with /dev/null in the container."""
        if not self.secret_files:
            return []
        from .search import SKIP_DIRS
        found, seen = [], 0
        for top, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            seen += len(files)
            found += [Path(top, f).resolve() for f in files if any(fnmatchcase(f, g) for g in self.secret_files)]
            if seen > MAX_MASK_SCAN or len(found) > MAX_MASKED:
                break
        return found[:MAX_MASKED]

    def run_argv(self) -> list[str]:
        mounts = [(self.workspace, ""), *((p, "") for p in self.writable), *((p, ":ro") for p in self.readonly),
                  *((p, ":ro") for p in self.locked())]
        masks = [x for p in self.masked() for x in ("-v", f"/dev/null:{p}:ro")]
        argv = [self.cli, "run", "-d", "--init", "--name", self.name, "--label", f"{PID_LABEL}={os.getpid()}",
                "--label", f"jimbo.workspace={self.workspace}", "--user", f"{os.getuid()}:{os.getgid()}",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(PIDS_LIMIT)]
        if self.kind == "podman" and self.runtime.rootless:
            argv.append("--userns=keep-id")  # else files written in the workspace belong to a subuid on the host
        if self.runtime.selinux and sys.platform == "linux":
            # SELinux denies bind mounts unless labelled; relabelling would change the user's files. A Mac's
            # VM labels its shared folders so containers can use them, and label=disable would unconfine.
            argv += ["--security-opt", "label=disable"]
        if not self.network:
            argv += ["--network", "none"]
        if self.memory:
            argv += ["--memory", self.memory]
        if self.cpus:
            argv += ["--cpus", str(self.cpus)]
        for p, ro in dict(mounts).items():
            argv += ["-v", f"{p}:{p}{ro}"]
        argv += [*masks, "-v", f"{CACHE_VOLUME}:{HOME}/.cache"]
        for k, v in {"HOME": HOME, "TERM": "dumb", "LANG": "C.UTF-8", **self.env}.items():
            argv += ["-e", f"{k}={v}"]
        return [*argv, "-w", str(self.workspace), "--entrypoint", "sleep", self.image, "infinity"]

    @property
    def running(self) -> bool:
        return self.started_at is not None

    async def ensure_started(self) -> None:
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:  # parallel tool calls start one container
            if self.running and self._watch is not None and self._watch.returncode is not None:
                reason = await self._stopped_reason()
                await self._remove()
                raise SandboxError(f"the sandbox container stopped ({reason}); it will be restarted on the next "
                                   "command, without anything installed outside the workspace")
            if self.running:
                return
            await self._start()
        self.notes = []
        for host, container in self.ports:
            try:
                await self.open_port(host, container)
            except SandboxError as e:
                self.notes.append(str(e))

    async def _start(self) -> None:
        proc = await asyncio.create_subprocess_exec(*self.run_argv(), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            await _run(self.cli, "rm", "-f", self.name)
            text = err.decode(errors="replace").strip()
            if self.kind == "podman" and sys.platform == "darwin" and (path := unshared(text)):
                raise SandboxError(f"the sandbox container didn't start: {path} is not shared with the podman "
                                   'machine VM. Add it with "podman machine init --volume", or move the workspace '
                                   "under /Users.")
            raise SandboxError(f"the sandbox container didn't start: {text}")
        self.started_at = time.time()
        # The cache volume is root's when docker creates it; any user id may write to it. (Podman with keep-id
        # gives it to the user, so this fails there, and isn't needed.)
        await _run(self.cli, "exec", "-u", "0", self.name, "sh", "-c",
                   f"mkdir -p {HOME}/.cache && chmod 1777 {HOME} {HOME}/.cache")
        if (await _run(self.cli, "exec", self.name, "sh", "-c", "command -v bash")) != 0:
            self.shell = "sh"
        self._watch = await asyncio.create_subprocess_exec(self.cli, "wait", self.name,
                                                           stdout=asyncio.subprocess.DEVNULL,
                                                           stderr=asyncio.subprocess.DEVNULL)

    async def _stopped_reason(self) -> str:
        p = await asyncio.create_subprocess_exec(self.cli, "inspect", "-f", "{{.State.ExitCode}} {{.State.OOMKilled}}",
                                                 self.name, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        code, _, oom = out.decode().strip().partition(" ")
        return f"exit code {code or '?'}" + (", out of memory" if oom == "true" else "")

    async def _remove(self) -> None:
        if self._watch and self._watch.returncode is None:
            self._watch.kill()
            await self._watch.wait()
        self._watch = None
        if self.running:
            await _run(self.cli, "rm", "-f", self.name)
        self.started_at = None
        self._runs.clear()

    async def close(self) -> None:
        """Close port forwards and remove the container. The next process starts a new one."""
        for host in list(self.forwards):
            await self.close_port(host)
        await self._remove()

    # ---- processes ----

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                    **kw) -> asyncio.subprocess.Process:
        await self.ensure_started()
        run = secrets.token_hex(8)
        flags = ["-i"] if kw.get("stdin") == asyncio.subprocess.PIPE else []
        for k, v in {**(env or {}), "JIMBO_RUN": run}.items():
            flags += ["-e", f"{k}={v}"]
        # Mounts are at resolved paths (/var/folders is /private/var/folders on macOS).
        proc = await asyncio.create_subprocess_exec(self.cli, "exec", *flags, "-w", str(cwd.resolve()), self.name,
                                                    *argv, start_new_session=True, **kw)
        self._runs[proc.pid] = run
        return proc

    async def kill(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the process and everything it started inside the container, then the local exec client."""
        run = self._runs.pop(proc.pid, None)
        if run and self.running:
            await _run(self.cli, "exec", self.name, "sh", "-c", _KILL_RUN, "sh", run)
        kill_group(proc)

    # ---- ports ----

    async def open_port(self, host: int, container: int) -> None:
        if host in self.forwards:
            raise SandboxError(f"host port {host} is already forwarded to container port "
                               f"{self.forwards[host].container}")
        await self.ensure_started()
        if (await _run(self.cli, "exec", self.name, "sh", "-c", "command -v socat")) != 0:
            raise SandboxError("the sandbox image has no socat; port forwarding needs it")
        fwd = _Forward(host, container)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            fwd.conns.add(task)
            try:
                await self._relay(container, reader, writer)
            finally:
                fwd.conns.discard(task)
        try:
            fwd.server = await asyncio.start_server(handle, "127.0.0.1", host)
        except OSError:
            raise SandboxError(f"host port {host} is in use") from None
        self.forwards[host] = fwd

    async def _relay(self, container: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            proc = await self.spawn(["socat", "-", f"TCP:localhost:{container}"], cwd=self.workspace,
                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.DEVNULL)
        except SandboxError:
            writer.close()
            return
        assert proc.stdin is not None and proc.stdout is not None

        async def up() -> None:
            while data := await reader.read(65536):
                proc.stdin.write(data)
                await proc.stdin.drain()
            proc.stdin.close()

        async def down() -> None:
            while data := await proc.stdout.read(65536):
                writer.write(data)
                await writer.drain()
        try:
            await asyncio.gather(up(), down())
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            if proc.returncode is None:
                await self.kill(proc)
            await proc.wait()
            self._runs.pop(proc.pid, None)

    async def close_port(self, host: int) -> bool:
        if (fwd := self.forwards.pop(host, None)) is None:
            return False
        assert fwd.server is not None
        fwd.server.close()
        conns = list(fwd.conns)
        for task in conns:
            task.cancel()
        await asyncio.gather(*conns, return_exceptions=True)
        await fwd.server.wait_closed()
        return True

    # ---- text ----

    def mounts_text(self) -> str:
        extra = [*(str(p) for p in self.writable), *(f"{p} (read-only)" for p in self.readonly)]
        return f"the workspace and {', '.join(extra)} are mounted" if extra else "only the workspace is mounted"

    def describe(self) -> str:
        net = "network allowed" if self.network else "no network"
        shell = "" if self.shell == "bash" else "; no bash in the image, using sh"
        git = "; .git hooks and config read-only" if self.git_write else "; git read-only"
        if self.protected:
            git += "; protected_paths read-only (those there when the container started)"
        if self.secret_files:
            git += "; secret files hidden (only those there when the container started)"
        return f"in {self.kind} container {self.name}, image {self.image}; {self.mounts_text()}{git}; {net}{shell}"

    def status(self) -> str:
        """What /sandbox shows."""
        if self.running:
            state = f"running, started {datetime.fromtimestamp(self.started_at).strftime('%H:%M')}"
        else:
            state = "not started; it starts at the next command"
        mounts = [f"{self.workspace} (read-write)", *(f"{p} (read-write)" for p in self.writable),
                  *(f"{p} (read-only)" for p in self.readonly), f"cache volume {CACHE_VOLUME}"]
        ports = ", ".join(f"127.0.0.1:{f.host} -> {f.container} ({len(f.conns)} connection(s) open)"
                          for f in self.forwards.values()) or "none"
        limits = (f"memory {self.memory or 'no limit'}, cpus {self.cpus or 'no limit'}, {PIDS_LIMIT} processes"
                  if self.runtime.limits_enforced else "not enforced (rootless Podman on cgroups v1)")
        through = f" (through the {self.cli} command)" if self.cli != self.kind else ""
        lines = [f"{self.kind} sandbox{through}: container {self.name} ({state})", f"image:    {self.image}",
                 f"mounts:   {', '.join(mounts)}",
                 f"network:  {'allowed' if self.network else 'none'}", f"ports:    {ports}", f"limits:   {limits}"]
        if self.kind == "podman":
            lines.append(f"rootless: {'yes' if self.runtime.rootless else 'no'}")
        return "\n".join([*lines, *self.notes])


async def _run(*argv: str) -> int:
    p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                             stderr=asyncio.subprocess.DEVNULL)
    return await p.wait()


def build(settings: "Settings", workspace: Path, cli: str = "docker", scratch: Path | None = None,
          runtime: Runtime | None = None) -> ContainerSandbox:
    """The sandbox for `sandbox = "docker"` or `"podman"`, with `scratch` mounted too. Checks the runtime (unless
    `runtime` is the answer from a probe already made) and gets the image now, before any UI starts."""
    try:
        runtime = runtime or probe(cli)
    except Unavailable as e:
        raise ValueError(f'sandbox = "{cli}" but {cli.capitalize()} is not available: {e}. {_advice(cli)}, or set '
                         'sandbox to "auto" or "off".') from None
    image = settings.sandbox_image or bundled_image()
    writable = [*([scratch] if scratch else []), *settings.sandbox_writable]
    sandbox = ContainerSandbox(image, workspace, runtime=runtime, writable=writable,
                               readonly=settings.sandbox_readonly, network=settings.sandbox_network,
                               ports=[parse_port(p) for p in settings.sandbox_ports], env=settings.sandbox_env,
                               memory=settings.sandbox_memory, cpus=settings.sandbox_cpus,
                               git_write=settings.git_write, protected=settings.protected(),
                               secret_files=settings.secret_files if settings.secret_files_shell else [])
    remove_stale(cli)
    ensure_image(image, cli)
    return sandbox
