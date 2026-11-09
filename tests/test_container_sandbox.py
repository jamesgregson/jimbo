"""The container sandbox. Unit tests need no runtime; the rest run against the bundled image once per runtime
(Docker, Podman) and are skipped for a runtime that is missing or has no image."""

import asyncio
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.core.approval import ApprovalGate, Yolo
from jimbo.core.deps import Deps
from jimbo.lsp.manager import LspManager
from jimbo.lsp.servers import executables, resolve_servers
from jimbo.tools import container_sandbox as ds
from jimbo.tools.sandbox import SandboxError, build_sandbox
from jimbo.tools.shell import kill_background, shell, shell_kill
from jimbo.wiring import build_session


def ctx(box, workspace):
    return SimpleNamespace(tool_call_id=None, deps=Deps(workspace=workspace, gate=ApprovalGate([Yolo()]),
                                                        sandbox=box))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- without Docker ----

def test_run_argv(tmp_path):
    ws = tmp_path.resolve()
    (tmp_path / "ro").mkdir()
    box = ds.ContainerSandbox("img:1", ws, readonly=[tmp_path / "ro"], network=False, env={"A": "1"}, memory="2g")
    argv = box.run_argv()
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "-d"] and "--userns=keep-id" not in argv
    assert f"-v {ws}:{ws}" in joined and f"-v {ws / 'ro'}:{ws / 'ro'}:ro" in joined
    assert "--cap-drop ALL" in joined and "--security-opt no-new-privileges" in joined
    assert f"--user {os.getuid()}:{os.getgid()}" in joined and "--network none" in joined
    assert "--network" not in ds.ContainerSandbox("img:1", ws).run_argv()
    assert "--memory 2g" in joined and "-e A=1" in joined and "--cpus" not in joined
    assert argv[-3:] == ["sleep", "img:1", "infinity"]
    assert "OPENROUTER" not in joined and f"label {ds.PID_LABEL}={os.getpid()}" in joined.replace("--label ", "label ")


def test_git_folders_are_mounted_read_only(tmp_path):
    ws = tmp_path.resolve()
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / ".git" / "config").write_text("")
    joined = " ".join(ds.ContainerSandbox("img:1", ws, git_write=False).run_argv())
    assert f"-v {ws / '.git'}:{ws / '.git'}:ro" in joined
    box = ds.ContainerSandbox("img:1", ws, git_write=True)
    joined = " ".join(box.run_argv())
    assert f"{ws / '.git'}:{ws / '.git'}:ro" not in joined
    assert f"-v {ws / '.git' / 'hooks'}:{ws / '.git' / 'hooks'}:ro" in joined and "config:ro" in joined
    assert "hooks and config read-only" in box.describe()
    assert "git read-only" in ds.ContainerSandbox("img:1", ws, git_write=False).describe()


def test_run_argv_podman(tmp_path, monkeypatch):
    ws = tmp_path.resolve()
    rootless = ds.ContainerSandbox("img:1", ws, runtime=ds.Runtime("podman", "podman", rootless=True)).run_argv()
    assert rootless[:2] == ["podman", "run"] and "--userns=keep-id" in rootless
    assert "--userns=keep-id" not in ds.ContainerSandbox("img:1", ws, runtime=ds.Runtime("podman", "podman")).run_argv()
    selinux = ds.Runtime("podman", "podman", rootless=True, selinux=True)
    monkeypatch.setattr(ds.sys, "platform", "linux")
    assert "label=disable" in ds.ContainerSandbox("img:1", ws, runtime=selinux).run_argv()
    monkeypatch.setattr(ds.sys, "platform", "darwin")  # the VM's shared folders are labelled for containers
    assert "label=disable" not in ds.ContainerSandbox("img:1", ws, runtime=selinux).run_argv()


def test_parse_info():
    podman = {"host": {"security": {"rootless": True, "selinuxEnabled": True}, "cgroupVersion": "v2"}}
    assert ds.parse_info("podman", podman) == ds.Runtime("podman", "podman", True, True, True)
    podman["host"]["cgroupVersion"] = "v1"
    assert not ds.parse_info("docker", podman).limits_enforced and ds.parse_info("docker", podman).engine == "podman"
    docker = {"SecurityOptions": ["name=seccomp,profile=default", "name=rootless"]}
    assert ds.parse_info("docker", docker) == ds.Runtime("docker", "docker", rootless=True)


def test_first_sentence():
    assert ds._first_sentence("\nCannot connect to Podman. Please verify your connection\nError: x") == \
        "Cannot connect to Podman"
    assert ds._first_sentence("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker "
                              "daemon running?") == "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


def test_old_tags_keep_the_current_image():
    tags = ["localhost/jimbo-sandbox:1.0", "localhost/jimbo-sandbox:0.9", "jimbo-sandbox:<none>"]
    assert ds.old_tags(tags, "jimbo-sandbox:1.0") == ["localhost/jimbo-sandbox:0.9"]


def test_unshared(tmp_path):
    assert ds.unshared(f"Error: statfs {tmp_path}: no such file or directory") == str(tmp_path)
    assert ds.unshared(f"Error: statfs {tmp_path / 'gone'}: no such file or directory") is None
    assert ds.unshared("Error: something else") is None


def test_status_podman(tmp_path):
    box = ds.ContainerSandbox("img:1", tmp_path, runtime=ds.Runtime("docker", "podman", rootless=True,
                                                                     limits_enforced=False))
    status = box.status()
    assert status.startswith("podman sandbox (through the docker command): container jimbo-")
    assert "rootless: yes" in status and "not enforced (rootless Podman on cgroups v1)" in status
    assert box.describe().startswith(f"in podman container {box.name}") and box.kind == "podman" and box.container


def test_mounts_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="won't mount /"):
        ds.check_mounts([Path("/")])
    with pytest.raises(ValueError, match="won't mount"):
        ds.check_mounts([Path.home().resolve()])
    sock = tmp_path / "run" / "docker.sock"
    sock.parent.mkdir()
    sock.touch()
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    with pytest.raises(ValueError, match="container socket"):
        ds.check_mounts([tmp_path.resolve()])
    ds.check_mounts([tmp_path.resolve() / "other"])
    podman = tmp_path / "xdg" / "podman" / "podman.sock"
    podman.parent.mkdir(parents=True)
    podman.touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    with pytest.raises(ValueError, match="container socket"):
        ds.check_mounts([(tmp_path / "xdg").resolve()])
    machine = tmp_path / "TMP" / "podman"
    machine.mkdir(parents=True)
    monkeypatch.setattr(ds.sys, "platform", "darwin")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "TMP"))
    with pytest.raises(ValueError, match="container socket"):
        ds.check_mounts([(tmp_path / "TMP").resolve()])


def test_parse_port():
    assert ds.parse_port(5173) == (5173, 5173)
    assert ds.parse_port("8080:5173") == (8080, 5173)
    for bad in ("x", "0", "70000", "1:y"):
        with pytest.raises(ValueError):
            ds.parse_port(bad)


def test_describe(tmp_path):
    box = ds.ContainerSandbox("img:1", tmp_path, readonly=[tmp_path / "cfg"])
    assert box.describe() == (f"in docker container {box.name}, image img:1; the workspace and {tmp_path / 'cfg'} "
                              "(read-only) are mounted; .git hooks and config read-only; network allowed")
    assert "only the workspace is mounted" in ds.ContainerSandbox("img:1", tmp_path).describe()
    assert "not started" in box.status()


def test_unavailable_refuses_to_start(tmp_path, monkeypatch):
    def probe(cli):
        raise ds.Unavailable(f"Cannot connect to {cli}")
    monkeypatch.setattr(ds, "probe", probe)
    with pytest.raises(ValueError, match='sandbox = "docker" but Docker is not available: Cannot connect to docker. '
                                         'Start Docker, or set'):
        build_sandbox(settings(tmp_path, sandbox="docker"), tmp_path)
    monkeypatch.setattr(ds.sys, "platform", "darwin")
    with pytest.raises(ValueError, match='sandbox = "podman" but Podman is not available: Cannot connect to podman. '
                                         'Run "podman machine init" once and then "podman machine start", or set'):
        build_sandbox(settings(tmp_path, sandbox="podman"), tmp_path)


def test_missing_cli():
    with pytest.raises(ds.Unavailable, match="no-such-runtime is not installed"):
        ds.probe("no-such-runtime")


async def test_sandbox_command_without_docker(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, sandbox="off"), fe, model=TestModel())
    await s.commands.dispatch(s, "/sandbox")
    await s.commands.dispatch(s, "/sandbox reset")
    out = [e.data for e in fe.events if e.kind == "command_output"]
    assert out[0]["text"] == "not sandboxed" and not out[1]["ok"]


# ---- with Docker or Podman ----

IMAGE = ds.bundled_image()


def _ready(cli: str) -> ds.Runtime | None:
    try:
        rt = ds.probe(cli)
    except ds.Unavailable:
        return None
    return rt if ds.has_image(IMAGE, cli) else None


READY = {cli: _ready(cli) for cli in ("docker", "podman")}


@pytest.fixture(params=[pytest.param(cli, marks=pytest.mark.skipif(
    READY[cli] is None, reason=f"needs {cli} and the {IMAGE} image (jimbo sandbox build {cli})"))
    for cli in READY])
def rt(request) -> ds.Runtime:
    return READY[request.param]


@pytest.fixture
async def box(rt, tmp_path):
    b = ds.ContainerSandbox(IMAGE, tmp_path.resolve(), runtime=rt)
    yield b
    await b.close()


def processes(box) -> str:
    return subprocess.run([box.cli, "exec", box.name, "ps", "-eo", "args"], capture_output=True, text=True).stdout


async def test_confines_reads_writes_and_env(box, tmp_path, monkeypatch):
    monkeypatch.setenv("JIMBO_TEST_SECRET", "s3cret")
    c = ctx(box, tmp_path.resolve())
    out = await shell(c, f"echo hi > inside.txt; ls {Path.home()}; echo rc=$?; touch /etc/x; env")
    assert (tmp_path / "inside.txt").read_text() == "hi\n" and (tmp_path / "inside.txt").stat().st_uid == os.getuid()
    assert "rc=2" in out and "Permission denied" in out and "s3cret" not in out and "HOME=/home/jimbo" in out


async def test_files_written_belong_to_the_user(box, tmp_path):
    await shell(ctx(box, tmp_path.resolve()), "mkdir -p d && echo x > d/f")
    st = (tmp_path / "d" / "f").stat()
    assert (st.st_uid, (tmp_path / "d").stat().st_uid) == (os.getuid(), os.getuid())


async def test_cache_dir_is_writable(box, tmp_path):
    out = await shell(ctx(box, tmp_path.resolve()), "touch ~/.cache/probe && echo ok")
    assert out.endswith("ok")


async def test_no_network(rt, tmp_path):
    b = ds.ContainerSandbox(IMAGE, tmp_path.resolve(), runtime=rt, network=False)
    try:
        out = await shell(ctx(b, tmp_path.resolve()), "curl -sS --max-time 5 https://example.com; echo rc=$?")
        assert "rc=0" not in out
    finally:
        await b.close()


async def test_timeout_kills_everything_in_the_container(box, tmp_path):
    out = await shell(ctx(box, tmp_path.resolve()), "sleep 301 & sleep 302", timeout=2)
    assert "timed out after 2s" in out
    ps = processes(box)
    assert "sleep 301" not in ps and "sleep 302" not in ps


async def test_background_and_port_forward(box, tmp_path):
    (tmp_path / "index.html").write_text("hello from the container")
    c = ctx(box, tmp_path.resolve())
    started = await shell(c, "python3 -m http.server 8000 --bind 127.0.0.1", background=True)
    assert started.startswith("background process 1: running")
    host = free_port()
    await box.open_port(host, 8000)
    with pytest.raises(SandboxError, match="already forwarded"):
        await box.open_port(host, 8000)

    def get() -> str:
        for _ in range(20):
            try:
                return urllib.request.urlopen(f"http://127.0.0.1:{host}/index.html", timeout=5).read().decode()
            except OSError:
                time.sleep(0.25)
        raise AssertionError("no answer through the forward")
    assert await asyncio.to_thread(get) == "hello from the container"
    assert f"127.0.0.1:{host} -> 8000" in box.status()
    assert await box.close_port(host) and not await box.close_port(host)
    await shell_kill(c, "1")
    assert "http.server" not in processes(box)


async def test_close_removes_the_container(box, tmp_path):
    await shell(ctx(box, tmp_path.resolve()), "true")
    name = box.name
    await box.close()
    r = subprocess.run([box.cli, "inspect", name], capture_output=True)
    assert r.returncode != 0 and not box.running


async def test_stopped_container_is_restarted(box, tmp_path):
    c = ctx(box, tmp_path.resolve())
    await shell(c, "true")
    subprocess.run([box.cli, "kill", box.name], capture_output=True, check=True)
    await asyncio.sleep(0.5)
    with pytest.raises(ModelRetry, match="the sandbox container stopped"):
        await shell(c, "true")
    assert (await shell(c, "echo back")).endswith("back")


def test_remove_stale(rt):
    name = f"jimbo-stale-{os.getpid()}"
    subprocess.run([rt.cli, "run", "-d", "--name", name, "--label", f"{ds.PID_LABEL}=999999", "--entrypoint",
                    "sleep", IMAGE, "infinity"], capture_output=True, check=True)
    try:
        ds.remove_stale(rt.cli)
        assert subprocess.run([rt.cli, "inspect", name], capture_output=True).returncode != 0
    finally:
        subprocess.run([rt.cli, "rm", "-f", name], capture_output=True)


def test_finds_language_servers_in_the_image(rt):
    found = ds.find_commands(IMAGE, ["clangd", "basedpyright-langserver", "rust-analyzer"], rt.cli)
    assert found == {"clangd", "basedpyright-langserver"}


async def test_session_status_and_command(rt, tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, sandbox=rt.cli), fe, model=TestModel())
    try:
        await s.emit_status(initial=True)
        status = next(e.data for e in fe.events if e.kind == "status")
        assert status["sandboxed"] and status["sandbox"] == rt.engine
        await s.commands.dispatch(s, "/sandbox")
        await s.commands.dispatch(s, "/sandbox port nope")
        out = [e.data for e in fe.events if e.kind == "command_output"]
        assert out[0]["text"].startswith(f"{rt.engine} sandbox: container jimbo-") and not out[1]["ok"]
    finally:
        await kill_background(s.deps)
        await s.deps.sandbox.close()


async def test_language_server_runs_in_the_container(box, tmp_path):
    ws = tmp_path.resolve()
    (ws / "a.py").write_text("undefined_name\n")
    s = settings(ws, lsp=True)
    found = ds.find_commands(IMAGE, executables(s), box.cli)
    choices, _ = resolve_servers(s, lambda name: name if name in found else None)
    manager = LspManager(ws, choices, sandbox=box)
    try:
        client = await manager.client_for(manager.server_for(ws / "a.py"), ctx(box, ws).deps, None)
        await manager.sync(client, ws / "a.py")
        for _ in range(80):
            if diags := client.diagnostics.get((ws / "a.py").as_uri()):
                break
            await asyncio.sleep(0.25)
        assert any('"undefined_name" is not defined' in d["message"] for d in diags)
    finally:
        await manager.close()
    assert "langserver" not in processes(box)


async def test_eval_trial_runs_in_its_own_container(rt, tmp_path):
    import json
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
    from evals.runner import RunConfig, run_suite
    from evals.tasks import load_tasks

    async def stream(messages, info):
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            yield "done"
        else:
            # conftest.py runs when the check's pytest starts: it records which system the check ran on
            command = ("grep ^ID= /etc/os-release; printf 'import pathlib\\npathlib.Path(\"where\").write_text("
                       "pathlib.Path(\"/etc/os-release\").read_text())\\n' > conftest.py")
            yield {0: DeltaToolCall(name="shell", json_args=json.dumps({"command": command}))}

    cfg = RunConfig(sandbox=rt.cli, user_config=tmp_path / "none.toml", lsp=False, keep=True,
                    main_model=FunctionModel(stream_function=stream))
    run_dir = await run_suite(load_tasks(["bug-paginate"]), cfg, results_dir=tmp_path)
    log = (run_dir / "bug-paginate" / "1" / "session.jsonl").read_text()
    assert "ID=debian" in log
    row = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
    ws = Path(row["workspace"])
    try:
        assert "ID=debian" in (ws / "where").read_text()  # the check ran in the container, not on the host
    finally:
        shutil.rmtree(ws.parent, ignore_errors=True)
    left = subprocess.run([rt.cli, "ps", "-a", "--filter", f"label={ds.PID_LABEL}={os.getpid()}", "-q"],
                          capture_output=True, text=True).stdout
    assert left.strip() == ""


def test_sandbox_flag_overrides_the_config(tmp_path):
    from jimbo.config import Settings
    from jimbo.main import overrides, parser
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "config.toml").write_text('sandbox = "docker"\n')

    def mode(*argv):
        return Settings(user_dir=tmp_path / "user", **overrides(parser().parse_args([*argv, "--workspace",
                                                                                      str(tmp_path)]))).sandbox
    assert mode() == "docker"
    assert mode("--sandbox", "off") == "off"
    assert mode("--sandbox", "auto") == "auto"
    with pytest.raises(SystemExit):
        parser().parse_args(["--sandbox", "lxc"])
    assert mode("--sandbox", "podman") == "podman"


def test_build_cli(tmp_path, monkeypatch):
    from jimbo.config import USER_DIR_ENV
    from jimbo.main import build_cli
    monkeypatch.setenv(USER_DIR_ENV, str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert build_cli(["podman"]) == "podman" and build_cli([]) == "docker"
    (tmp_path / "config.toml").write_text('sandbox = "podman"\n')
    assert build_cli([]) == "podman"
    with pytest.raises(ValueError, match="usage"):
        build_cli(["lxc"])


async def test_git_is_read_only_in_the_container(rt, tmp_path):
    ws = tmp_path.resolve()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "a.txt").write_text("A")
    b = ds.ContainerSandbox(IMAGE, ws, runtime=rt, git_write=False)
    try:
        out = await shell(ctx(b, ws), "git status --short; git add a.txt; echo rc=$?")
        assert "?? a.txt" in out and "rc=128" in out
    finally:
        await b.close()


def test_secret_files_are_masked_only_when_asked(tmp_path):
    ws = tmp_path.resolve()
    (ws / "svc").mkdir()
    (ws / ".env").write_text("A=1")
    (ws / "svc" / "key.pem").write_text("K")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / ".env").write_text("skip")
    assert ds.ContainerSandbox("img:1", ws).masked() == []
    box = ds.ContainerSandbox("img:1", ws, secret_files=[".env", "*.pem"])
    assert sorted(box.masked()) == [ws / ".env", ws / "svc" / "key.pem"]
    joined = " ".join(box.run_argv())
    assert f"-v /dev/null:{ws / '.env'}:ro" in joined and "secret files hidden" in box.describe()


async def test_masked_secret_files_read_as_empty(rt, tmp_path):
    ws = tmp_path.resolve()
    (ws / ".env").write_text("SECRET=1")
    b = ds.ContainerSandbox(IMAGE, ws, runtime=rt, secret_files=[".env"])
    try:
        out = await shell(ctx(b, ws), "cat .env; echo rc=$?; echo x > .env")  # the write goes to /dev/null
        assert "SECRET" not in out and "rc=0" in out
    finally:
        await b.close()
    assert (ws / ".env").read_text() == "SECRET=1"
