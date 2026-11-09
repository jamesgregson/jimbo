"""Sandboxes for shell commands and language servers: macOS `sandbox-exec` (seatbelt), or a Docker or Podman
container.

A sandbox starts processes (`spawn`) and stops them with everything they started (`kill`). With no sandbox, the
module functions `spawn` and `kill` run processes directly."""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ..config import Settings

# auto: podman, then docker, then seatbelt, then none. seatbelt: seatbelt if available, else none. on: seatbelt, or
# refuse to start. docker, podman: that container, or refuse to start. off: none.
SandboxMode = Literal["auto", "seatbelt", "on", "off", "docker", "podman"]
AUTO_ORDER = ("podman", "docker")  # tried before seatbelt
PROBE_TIMEOUT = 5  # seconds auto waits for a container runtime to answer
SHELL = shutil.which("bash") or "/bin/sh"

# Device files a normal command writes to; everything else outside `writable` is denied.
_DEVICES = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$") (subpath "/dev/fd")'


class SandboxError(Exception):
    """The sandbox can't run a process. The message is for the model or the user."""


class Sandbox(Protocol):
    kind: Literal["seatbelt", "docker", "podman"]
    container: bool  # commands run in a container, which has its own processes, files and programs
    network: bool
    shell: str  # the shell `shell` commands run with

    def describe(self) -> str: ...

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                    **kw) -> asyncio.subprocess.Process: ...

    async def kill(self, proc: asyncio.subprocess.Process) -> None: ...

    async def close(self) -> None: ...


def kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: `cmd | cmd`, background jobs, ...
    except (ProcessLookupError, PermissionError):
        pass


# Environment variables commands don't get from jimbo, matched without case: keys, tokens, passwords and the SSH
# agent. Containers get none of jimbo's environment anyway.
SECRET_ENV = ["*KEY*", "*TOKEN*", "*SECRET*", "*PASSWORD*", "*PASSWD*", "*CREDENTIAL*", "*_AUTH*", "AWS_*", "AZURE_*",
              "GOOGLE_APPLICATION_CREDENTIALS", "SSH_AUTH_SOCK"]
# Set for every command: git never asks for or uses credentials, and `git status` doesn't write .git/index.
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
           "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
           "GIT_CONFIG_KEY_1": "core.sshCommand", "GIT_CONFIG_VALUE_1": "false"}
_env = {"keep": set(), "drop": set()}  # set by configure_env


def configure_env(keep: list[str] = (), drop: list[str] = ()) -> None:
    """Names to pass to commands even if they look secret (shell_env_keep), and more names to remove (the
    providers' key variables)."""
    _env["keep"], _env["drop"] = set(keep), set(drop)


def is_secret_env(name: str) -> bool:
    if name in _env["keep"]:
        return False
    return name in _env["drop"] or any(fnmatchcase(name.upper(), p) for p in SECRET_ENV)


def command_environ(extra: dict[str, str] | None = None) -> dict[str, str]:
    """jimbo's environment without secret variables, plus `extra`, for a process on this machine."""
    return {**{k: v for k, v in os.environ.items() if not is_secret_env(k)}, **(extra or {})}


async def _exec(argv: list[str], cwd: Path | None, env: dict[str, str] | None, kw: dict) -> asyncio.subprocess.Process:
    # Own group: Ctrl+C goes to jimbo only, and kill_group takes the whole tree.
    return await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=command_environ(env),
                                                start_new_session=True, **kw)


async def spawn(sandbox: Sandbox | None, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                **kw) -> asyncio.subprocess.Process:
    if sandbox:
        return await sandbox.spawn(argv, cwd=cwd, env=env, **kw)
    return await _exec(argv, cwd, env, kw)


async def kill(sandbox: Sandbox | None, proc: asyncio.subprocess.Process) -> None:
    if sandbox:
        await sandbox.kill(proc)
    else:
        kill_group(proc)


# Folders under the home folder that commands may read: toolchains, caches and git's own settings. The rest of the
# home folder (keys, credentials, documents, browser profiles, ~/.jimbo) is unreadable.
HOME_READABLE = [".cache", "Library/Caches", ".local", ".cargo", ".rustup", ".npm", ".nvm", ".pyenv", ".rbenv",
                 ".bun", ".deno", ".volta", "go", ".gradle", ".m2", ".sdkman", ".asdf", ".rye", ".dotnet", ".nuget",
                 ".swiftpm", ".conda", "miniconda3", "anaconda3", "miniforge3", "Library/Python", ".nix-profile",
                 ".gitconfig", ".config/git", ".gitignore", ".gitignore_global"]


def _darwin_dir(name: str) -> Path | None:
    """A per-user folder macOS keeps under /var/folders, e.g. DARWIN_USER_CACHE_DIR."""
    try:
        r = subprocess.run(["getconf", name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def default_writable(workspace: Path, system_temp: bool = False) -> list[Path]:
    """Where commands may write besides the scratch directory. Not /tmp, unless `system_temp`: TMPDIR points into
    the scratch directory instead (see shell.py). macOS's own temp and cache folders stay writable, because Apple's
    tools (mktemp, clang, xcrun) use them whatever TMPDIR says."""
    home = Path.home()
    paths = [workspace, home / ".cache", home / "Library" / "Caches"]  # uv, pip, etc. keep caches here
    if system_temp:
        return [*paths, Path("/private/tmp"), Path("/private/var/folders")]
    user = [d for n in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR") if (d := _darwin_dir(n))]
    return [*paths, *user]


def home_readable(home: Path | None = None) -> list[Path]:
    """HOME_READABLE, and the folders on PATH that are in the home folder, so the programs in them run."""
    home = home or Path.home()
    on_path = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return [*(home / p for p in HOME_READABLE), *(p for p in on_path if p.is_relative_to(home))]


def git_dirs(workspace: Path) -> list[Path]:
    """The git folders of the workspace: .git, or for a worktree or submodule the folders its .git file points to."""
    dot = workspace / ".git"
    if not dot.is_file():
        return [dot]
    try:
        line = dot.read_text("utf-8").strip()
    except OSError:
        return [dot]
    if not line.startswith("gitdir:"):
        return [dot]
    gitdir = (workspace / line.removeprefix("gitdir:").strip()).resolve()
    dirs = [gitdir]
    try:
        common = (gitdir / "commondir").read_text("utf-8").strip()
        dirs.append((gitdir / common).resolve())
    except OSError:
        pass
    return dirs


def _sbpl_regex(text: str) -> str:
    """`text` escaped for a regex in a seatbelt profile."""
    out = "".join("\\" + c if c in ".^$|?*+()[]{}\\" else c for c in text)
    return out.replace('"', '\\"')


def _path_glob_regex(glob: str) -> str:
    """A workspace-relative glob (protected_paths: `**` any depth, no `/` means any folder) as a seatbelt regex for
    the path after the workspace, covering what is inside a matching folder too."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(_sbpl_regex(glob[i]))
            i += 1
    prefix = "" if "/" in glob else "(.*/)?"
    return prefix + "".join(out) + "(/.*)?$"


def _glob_regex(glob: str) -> str:
    """A file-name glob such as `*.pem` as a seatbelt regex matching that name at the end of a path."""
    body = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else _sbpl_regex(c) for c in glob)
    return f"/{body}$"


class SeatbeltSandbox:
    kind: Literal["seatbelt"] = "seatbelt"
    container = False
    shell = SHELL

    def __init__(self, writable: list[Path], network: bool = True, *, readable: list[Path] | None = None,
                 home: Path | None = None, workspace: Path | None = None, git_write: bool = True,
                 secret_files: list[str] = (), protected: list[str] = ()) -> None:
        """`readable` None: reads aren't limited. Otherwise commands can't read `home` and /Volumes, except the
        `readable` folders. `workspace` with `git_write` False: its git folders are read-only; with `git_write`
        True, only .git/hooks and .git/config are. `secret_files`: file names commands can't read. `protected`:
        workspace-relative globs commands can't write (protected_paths)."""
        # The kernel checks real paths (/tmp is /private/tmp), so resolve symlinks first.
        self.writable = list(dict.fromkeys(p.expanduser().resolve() for p in writable))
        self.network = network
        self.readable = None if readable is None else list(dict.fromkeys(p.expanduser().resolve() for p in readable))
        self.home = (home or Path.home()).resolve()
        self.workspace = workspace.resolve() if workspace else None
        self.git_write = git_write
        self.secret_files = list(secret_files)
        self.protected = list(protected)

    def read_only(self) -> list[Path]:
        """Paths under writable folders that commands still can't write."""
        if self.workspace is None:
            return []
        dirs = git_dirs(self.workspace)
        if self.git_write:
            return [p for d in dirs for p in (d / "hooks", d / "config")]
        return dirs

    def profile(self) -> str:
        paths = " ".join(f'(subpath (param "W{i}"))' for i in range(len(self.writable)))
        rules = ["(version 1)", "(allow default)"]
        if self.readable is not None:
            rules.append('(deny file-read-data file-read-xattr (subpath (param "HOME")) (subpath "/Volumes"))')
            allowed = " ".join(f'(subpath (param "R{i}"))' for i in range(len(self.readable)))
            if allowed:
                rules.append(f"(allow file-read-data file-read-xattr {allowed})")
        if self.secret_files:
            names = " ".join(f'(regex #"{_glob_regex(g)}")' for g in self.secret_files)
            rules.append(f"(deny file-read-data {names} (literal (param \"GITCRED\")))")
        rules += ["(deny file-write*)", f"(allow file-write* {_DEVICES} {paths})"]
        if self.workspace is not None:
            locked = " ".join(f'(subpath (param "G{i}"))' for i in range(len(self.read_only())))
            ws = _sbpl_regex(str(self.workspace))
            nested = "" if self.git_write else f' (regex #"^{ws}/(.*/)?\\.git(/|$)")'
            prot = "".join(f' (regex #"^{ws}/{_path_glob_regex(g)}")' for g in self.protected)
            rules.append(f"(deny file-write* {locked}{nested}{prot})")
        if not self.network:
            rules.append('(deny network-outbound (remote ip "*:*"))')
        return "\n".join(rules)

    def params(self) -> list[str]:
        values = {f"W{i}": p for i, p in enumerate(self.writable)}
        values |= {f"R{i}": p for i, p in enumerate(self.readable or [])}
        values |= {f"G{i}": p for i, p in enumerate(self.read_only())}
        values |= {"HOME": self.home, "GITCRED": self.home / ".git-credentials"}
        return [x for k, v in values.items() for x in ("-D", f"{k}={v}")]

    def describe(self) -> str:
        net = "network allowed" if self.network else "no network"
        reads = ("reads limited to the workspace, the scratch directory and tool folders in your home folder"
                 if self.readable is not None else "reads not limited")
        git = "" if self.workspace is None else ("; .git hooks and config read-only" if self.git_write
                                                 else "; git read-only")
        if self.protected and self.workspace is not None:
            git += "; protected_paths read-only"
        return f"seatbelt sandbox: {reads}; writes limited to {', '.join(map(str, self.writable))}{git}; {net}"

    def wrap(self, argv: list[str]) -> list[str]:
        return ["sandbox-exec", *self.params(), "-p", self.profile(), *argv]

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                    **kw) -> asyncio.subprocess.Process:
        return await _exec(self.wrap(argv), cwd, env, kw)

    async def kill(self, proc: asyncio.subprocess.Process) -> None:
        kill_group(proc)

    async def close(self) -> None:
        pass


@cache
def available() -> bool:
    """sandbox-exec exists and works; it fails when jimbo itself already runs inside a sandbox."""
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return False
    try:
        r = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def build_sandbox(settings: "Settings", workspace: Path, scratch: Path | None = None,
                  skipped: list[dict] | None = None) -> Sandbox | None:
    """The sandbox `settings.sandbox` asks for, with `scratch` (the scratch root) writable too. Raises ValueError
    when it is required and can't be had. In auto mode, each sandbox tried and not used is added to `skipped` as
    {"kind", "reason"}."""
    mode = settings.sandbox
    skipped = [] if skipped is None else skipped
    if mode == "off":
        return None
    if mode in ("docker", "podman"):
        from . import container_sandbox
        return container_sandbox.build(settings, workspace, mode, scratch)
    if mode == "auto":
        from . import container_sandbox
        for cli in AUTO_ORDER:
            try:
                runtime = container_sandbox.probe(cli, timeout=PROBE_TIMEOUT)
                return container_sandbox.build(settings, workspace, cli, scratch, runtime=runtime)
            except container_sandbox.Unavailable as e:
                skipped.append({"kind": cli, "reason": str(e)})
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                skipped.append({"kind": cli, "reason": str(e).split(". ")[0]})
    if not available():
        if mode == "on":
            raise ValueError("sandbox='on' but sandbox-exec is unavailable (macOS only, and not nested in "
                             "another sandbox); use 'auto' or 'off'")
        if mode == "auto":
            skipped.append({"kind": "seatbelt", "reason": "sandbox-exec is unavailable (macOS only)"
                            if sys.platform != "darwin" else "sandbox-exec doesn't work here (already sandboxed?)"})
        return None
    extra = [scratch] if scratch else []
    user = settings.user_dir.expanduser()
    skills = [user / "skills", user.parent / ".claude" / "skills", user.parent / ".agents" / "skills"]
    readable = [workspace, *extra, *skills, *home_readable(), *settings.sandbox_readable]
    return SeatbeltSandbox([*default_writable(workspace, settings.sandbox_system_temp), *extra,
                            *settings.sandbox_writable], settings.sandbox_network, readable=readable,
                           workspace=workspace, git_write=settings.git_write, protected=settings.protected(),
                           secret_files=settings.secret_files if settings.secret_files_shell else [])


def label(kind: str | None, network: bool = True) -> str:
    """How every UI names the sandbox: "podman sandbox", "seatbelt sandbox (no network)" or "NOT sandboxed"."""
    if kind is None:
        return "NOT sandboxed"
    return f"{kind} sandbox" + ("" if network else " (no network)")


def startup_note(sandbox: Sandbox | None, skipped: list[dict]) -> str | None:
    """What auto chose and why, for stderr; None when nothing was skipped."""
    if not skipped:
        return None
    why = "; ".join(f"{s['kind']}: {s['reason']}" for s in skipped)
    if sandbox is None:
        return f"sandbox: NONE. Commands run as you with no limits ({why})"
    return f"sandbox: using {sandbox.kind} ({why})"
