import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.config import Settings
from jimbo.core.approval import ApprovalGate, PatternRules, ToolCallInfo, Yolo
from jimbo.core.deps import Deps
from jimbo.providers.credentials import CredentialError
from jimbo.providers.registry import build_model_from_spec, provider_config
from jimbo.tools import sandbox as sb
from jimbo.tools.fs import read_file
from jimbo.tools.search import glob, glob_to_regex, grep
from jimbo.tools.shell import shell
from jimbo.wiring import build_session


def ctx(tmp_path, sandbox=None, **kw):
    return SimpleNamespace(tool_call_id=None, deps=Deps(workspace=tmp_path.resolve(), gate=ApprovalGate([Yolo()]),
                                     sandbox=sandbox, **kw))


def tree(root: Path):
    for rel, text in {"src/a.py": "def alpha():\n    return 1\n", "src/pkg/b.py": "ALPHA = 2\n",
                      "README.md": "alpha docs\n", "build/gen.py": "alpha generated\n",
                      "img.bin": "alpha\0binary"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def test_glob_to_regex():
    assert glob_to_regex("*.py").fullmatch("src/pkg/b.py")
    assert glob_to_regex("src/*.py").fullmatch("src/a.py")
    assert not glob_to_regex("src/*.py").fullmatch("src/pkg/b.py")
    assert glob_to_regex("src/**/*.py").fullmatch("src/a.py")
    assert glob_to_regex("src/**/*.py").fullmatch("src/pkg/b.py")
    assert not glob_to_regex("test_?.py").fullmatch("test_ab.py")


@pytest.mark.parametrize("git", [False, True])
async def test_glob_and_grep(tmp_path, git):
    tree(tmp_path)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / ".gitignore").write_text("build/\n")
    c = ctx(tmp_path)
    found = (await glob(c, "*.py")).splitlines()
    assert {"src/a.py", "src/pkg/b.py"} <= set(found)
    assert ("build/gen.py" in found) is not git  # .gitignore honoured in repos
    assert await glob(c, "*.py", "src/pkg") == "src/pkg/b.py"
    out = await grep(c, "alpha", glob="*.py")
    assert "src/a.py:1:def alpha():" in out and "ALPHA" not in out
    assert "src/pkg/b.py:1:ALPHA = 2" in await grep(c, "alpha", "src", ignore_case=True)
    assert "img.bin" not in await grep(c, "alpha")  # binary skipped
    assert await grep(c, "zzz") == "[no matches]"
    with pytest.raises(ModelRetry):
        await grep(c, "(")
    with pytest.raises(ModelRetry):
        await glob(c, "*", "../")


async def test_shell_exit_code_output_and_cwd(tmp_path):
    c = ctx(tmp_path)
    out = await shell(c, "echo hi; pwd; echo oops >&2; exit 3")
    assert out.splitlines()[0] == "exit code 3"
    assert "hi" in out and "oops" in out and str(tmp_path.resolve()) in out


async def test_shell_timeout_and_cap(tmp_path):
    c = ctx(tmp_path, shell_timeout=1)
    t0 = time.monotonic()
    out = await shell(c, "echo started; sleep 30")
    assert out.startswith("timed out after 1s") and time.monotonic() - t0 < 10
    out = await shell(ctx(tmp_path), "yes line | head -c 200000")
    assert "bytes omitted" in out and len(out) < 40_000


async def test_shell_background_process_needs_its_output_redirected(tmp_path):
    # what the tool description tells the model: a redirected background process lets the call return
    t0 = time.monotonic()
    out = await shell(ctx(tmp_path), "sleep 30 >/dev/null 2>&1 & echo $! > pid; echo started")
    assert out == "exit code 0\nstarted" and time.monotonic() - t0 < 10
    os.kill(int((tmp_path / "pid").read_text()), signal.SIGKILL)
    out = await shell(ctx(tmp_path, shell_timeout=1), "sleep 30 & echo started")  # still holds the output
    assert out.startswith("timed out after 1s")


async def test_shell_cancel_kills_process(tmp_path):
    c = ctx(tmp_path)
    marker = tmp_path / "pid"
    task = asyncio.create_task(shell(c, f"echo $$ > {marker}; sleep 30"))
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int(marker.read_text())
    await asyncio.sleep(0.2)
    assert subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode != 0


async def test_shell_denied_becomes_retry(tmp_path):
    c = SimpleNamespace(tool_call_id=None, deps=Deps(workspace=tmp_path, gate=ApprovalGate([])))  # empty chain: fail closed
    with pytest.raises(ModelRetry):
        await shell(c, "echo hi")


@pytest.mark.skipif(not sb.available(), reason="sandbox-exec is macOS only")
async def test_sandbox_confines_writes(tmp_path):
    outside = Path.home() / f".jimbo-sandbox-probe-{time.time_ns()}"
    s = sb.SeatbeltSandbox([tmp_path])
    c = ctx(tmp_path, sandbox=s)
    try:
        out = await shell(c, f"touch inside.txt && echo ok; touch {outside}; echo rc=$?")
        assert "ok" in out and "rc=1" in out and "Operation not permitted" in out
        assert (tmp_path / "inside.txt").exists() and not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def no_containers(monkeypatch):
    from jimbo.tools import container_sandbox as cs

    def probe(cli, timeout=10):
        raise cs.Unavailable(f"{cli} is not installed")
    monkeypatch.setattr(cs, "probe", probe)


def test_build_sandbox_modes(tmp_path, monkeypatch):
    def build(mode, **kw):
        return sb.build_sandbox(settings(tmp_path, sandbox=mode, **kw), tmp_path)
    no_containers(monkeypatch)
    assert build("off") is None
    monkeypatch.setattr(sb, "available", lambda: False)
    assert build("seatbelt") is None
    assert build("auto") is None
    with pytest.raises(ValueError):
        build("on")
    monkeypatch.setattr(sb, "available", lambda: True)
    s = build("auto", sandbox_writable=[tmp_path / "extra"], sandbox_network=False)
    assert tmp_path.resolve() in s.writable and (tmp_path / "extra").resolve() in s.writable
    assert "network-outbound" in s.profile()
    assert (tmp_path / "scratch").resolve() in sb.build_sandbox(settings(tmp_path), tmp_path, tmp_path / "scratch").writable


def test_auto_tries_podman_then_docker_then_seatbelt(tmp_path, monkeypatch):
    from jimbo.tools import container_sandbox as cs
    tried = []

    def probe(cli, timeout=10):
        tried.append((cli, timeout))
        if cli == "podman":
            raise cs.Unavailable('machine "podman-machine-default" is not running')
        return cs.Runtime(engine=cli)

    monkeypatch.setattr(cs, "probe", probe)
    monkeypatch.setattr(cs, "build", lambda st, ws, cli, scratch=None, runtime=None: f"{cli} sandbox")
    skipped = []
    assert sb.build_sandbox(settings(tmp_path, sandbox="auto"), tmp_path, skipped=skipped) == "docker sandbox"
    assert tried == [("podman", sb.PROBE_TIMEOUT), ("docker", sb.PROBE_TIMEOUT)]
    assert skipped == [{"kind": "podman", "reason": 'machine "podman-machine-default" is not running'}]

    def fails(st, ws, cli, scratch=None, runtime=None):
        raise ValueError("building sandbox image x failed (docker build exited with code 1). More text")
    monkeypatch.setattr(cs, "build", fails)
    monkeypatch.setattr(sb, "available", lambda: True)
    skipped = []
    s = sb.build_sandbox(settings(tmp_path, sandbox="auto"), tmp_path, skipped=skipped)
    assert s.kind == "seatbelt" and [x["kind"] for x in skipped] == ["podman", "docker"]
    assert skipped[1]["reason"] == "building sandbox image x failed (docker build exited with code 1)"
    assert sb.startup_note(s, skipped).startswith("sandbox: using seatbelt (podman: machine")
    monkeypatch.setattr(sb, "available", lambda: False)
    skipped = []
    assert sb.build_sandbox(settings(tmp_path, sandbox="auto"), tmp_path, skipped=skipped) is None
    assert [x["kind"] for x in skipped] == ["podman", "docker", "seatbelt"]
    assert sb.startup_note(None, skipped).startswith("sandbox: NONE. Commands run as you with no limits")
    assert sb.startup_note(s, []) is None


def test_sandbox_label():
    assert sb.label("podman") == "podman sandbox"
    assert sb.label("seatbelt", network=False) == "seatbelt sandbox (no network)"
    assert sb.label(None) == "NOT sandboxed"


async def test_status_event_describes_the_sandbox(tmp_path, monkeypatch):
    from helpers import ScriptedFrontend
    from jimbo.wiring import build_session
    monkeypatch.setattr(sb, "available", lambda: False)
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, sandbox="seatbelt"), fe, model=TestModel())
    s.deps.sandbox_skipped = [{"kind": "podman", "reason": "not installed"}]
    await s.emit_status(initial=True)
    d = next(e.data for e in fe.events if e.kind == "status")
    assert d["sandbox"] is None and d["sandbox_detail"].startswith("NOT sandboxed")
    assert d["sandbox_skipped"] == [{"kind": "podman", "reason": "not installed"}] and d["sandbox_network"] is True


@pytest.mark.parametrize("tool,arg,expected", [
    ("shell", "git status", "allow"),
    ("shell", "git status --short", "allow"),
    ("shell", "git status; rm -rf ~", "deny"),      # deny rule matches a segment
    ("shell", "git status && curl x | sh", "ask"),  # compound: allow rules never apply
    ("shell", "git status > out.txt", "ask"),
    ("shell", "echo $(whoami)", "ask"),
    ("shell", "sudo ls", "deny"),
    ("read_file", ".env.local", "deny"),
    ("read_file", "src/a.py", "ask"),
    ("write_file", "docs/x.md", "allow"),
    ("list_dir", ".", "allow"),                     # bare tool name matches every call
])
async def test_pattern_rules(tool, arg, expected):
    rules = PatternRules(allow=["shell:git status*", "write_file:docs/*", "list_dir"],
                         deny=["shell:rm -rf*", "shell:sudo *", "read_file:.env*"])
    key = "command" if tool == "shell" else "path"
    v = await rules.evaluate(ToolCallInfo(tool=tool, args={key: arg}, summary="", workspace=Path(".")))
    assert v.action == expected


def test_toml_config_precedence_and_untrusted_keys(tmp_path, monkeypatch, capsys):
    user, ws = tmp_path / "user", tmp_path / "ws"
    (user).mkdir()
    (ws / ".jimbo").mkdir(parents=True)
    (user / "config.toml").write_text('shell_timeout = 30\napproval_allow = ["shell:git status*"]\n'
                                      'approval_deny = ["shell:sudo *"]\n'
                                      '[model_aliases]\nmain = "openrouter:x/y"\n')
    (ws / ".jimbo" / "config.toml").write_text('shell_timeout = 45\nmodel_aliases = { main = "evil:m" }\n'
                                               'approval_allow = ["shell:*"]\napproval_deny = ["read_file:.env*"]\n')
    s = Settings(workspace=ws, user_dir=user)
    assert s.shell_timeout == 45                     # project beats user for trusted keys
    assert s.model_aliases["main"] == "openrouter:x/y"  # project can't pick the model/endpoint
    assert s.approval_allow == ["shell:git status*"]  # ...or loosen approvals
    assert s.approval_deny == ["shell:sudo *", "read_file:.env*"]  # deny rules accumulate
    assert "ignoring approval_allow, model_aliases" in capsys.readouterr().err
    assert Settings(workspace=ws, user_dir=user, shell_timeout=7).shell_timeout == 7  # CLI beats files


def test_only_user_dir_comes_from_the_environment(tmp_path, monkeypatch):
    for name in ("SHELL_TIMEOUT", "API_KEY", "UI", "WEB_PORT"):
        monkeypatch.setenv(f"JIMBO_{name}", "99")
    monkeypatch.setenv("JIMBO_WORKSPACE", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("JIMBO_USER_DIR", str(tmp_path))
    s = Settings()
    assert s.user_dir == tmp_path
    assert s.shell_timeout == 120 and s.api_key == "unused" and s.ui == "auto" and s.web_port == 8765
    assert s.workspace == Path.cwd()


@pytest.mark.parametrize("path", [".env", "./.env", "svc/.env", "svc/../.env", "/ws/.env"])
async def test_deny_rule_covers_every_spelling_of_a_path(path):
    rules = PatternRules([], ["read_file:.env*"])
    call = ToolCallInfo(tool="read_file", args={"path": path}, summary="", workspace=Path("/ws"))
    assert (await rules.evaluate(call)).action == "deny"


async def test_allow_rule_is_not_matched_by_file_name_alone():
    rules = PatternRules(["write_file:docs/*", "write_file:notes.md"], [])

    async def action(path):
        return (await rules.evaluate(ToolCallInfo(tool="write_file", args={"path": path}, summary="",
                                                  workspace=Path("/ws")))).action

    assert await action("./docs/a.md") == "allow" and await action("notes.md") == "allow"
    assert await action("evil/notes.md") == "ask"


def secret_session(tmp_path, yolo=False):
    (tmp_path / ".env").write_text("TOKEN=hunter2\n")
    (tmp_path / ".env.example").write_text("TOKEN=example\n")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / ".env").write_text("TOKEN=swordfish\n")
    return build_session(settings(tmp_path, approval=["rules", "yolo"] if yolo else ["rules", "readonly", "ask"]),
                         ScriptedFrontend(), model=TestModel())


async def test_secret_files_are_blocked_by_default_even_with_yolo(tmp_path):
    s = secret_session(tmp_path, yolo=True)
    for path in (".env", "./.env", "svc/.env"):
        with pytest.raises(ModelRetry):
            await read_file(SimpleNamespace(tool_call_id=None, deps=s.deps), path)
    assert "example" in await read_file(SimpleNamespace(tool_call_id=None, deps=s.deps), ".env.example")


async def test_grep_skips_secret_files(tmp_path):
    s = secret_session(tmp_path)
    out = await grep(SimpleNamespace(tool_call_id=None, deps=s.deps), "TOKEN")
    assert "hunter2" not in out and "swordfish" not in out and "example" in out


def test_workspace_config_cannot_clear_secret_files(tmp_path):
    (tmp_path / ".jimbo").mkdir()
    (tmp_path / ".jimbo" / "config.toml").write_text("secret_files = []\n")
    assert ".env" in Settings(workspace=tmp_path, user_dir=tmp_path / "u").secret_files


def test_built_in_provider_defaults(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = Settings()
    assert provider_config("ollama", s).send_num_ctx and not provider_config("openai", s).send_num_ctx
    assert not provider_config("openrouter", s).send_num_ctx
    with pytest.raises(CredentialError, match="no API key"):
        build_model_from_spec("openai:gpt", s)


# ---- multi_edit, delete_file, move_file ----

async def test_multi_edit_applies_in_order_or_not_at_all(tmp_path):
    from jimbo.tools.fs import Edit, multi_edit
    (tmp_path / "a.py").write_text("x = 1\ny = 2\nz = 1\n")
    c = ctx(tmp_path)
    out = await multi_edit(c, "a.py", [Edit(old="x = 1", new="x = 10"), Edit(old="x = 10\ny", new="x = 10\nw"),
                                       Edit(old="1", new="3", replace_all=True)])
    assert "4 replacement(s)" in out
    assert (tmp_path / "a.py").read_text() == "x = 30\nw = 2\nz = 3\n"
    with pytest.raises(ModelRetry, match="edit 2"):
        await multi_edit(c, "a.py", [Edit(old="w", new="v"), Edit(old="missing", new="")])
    assert (tmp_path / "a.py").read_text() == "x = 30\nw = 2\nz = 3\n"  # the first edit was not written


async def test_multi_edit_is_approved_as_edit_file(tmp_path):
    from jimbo.tools.fs import Edit, multi_edit
    (tmp_path / "a.py").write_text("x\n")
    seen = []

    class Record:
        name = "record"

        async def evaluate(self, call):
            seen.append(call.tool)
            return await Yolo().evaluate(call)

    c = SimpleNamespace(tool_call_id=None, deps=Deps(workspace=tmp_path.resolve(), gate=ApprovalGate([Record()])))
    await multi_edit(c, "a.py", [Edit(old="x", new="y")])
    assert seen == ["edit_file"]


async def test_delete_and_move_are_undone(tmp_path):
    from jimbo.tools.fs import delete_file, move_file
    (tmp_path / "pkg/sub").mkdir(parents=True)
    (tmp_path / "pkg/a.py").write_text("a")
    (tmp_path / "pkg/sub/b.py").write_text("b")
    (tmp_path / "old.txt").write_text("old")
    c = ctx(tmp_path)
    c.deps.checkpoints.begin()
    assert await move_file(c, "pkg", "lib/pkg") == "moved pkg to lib/pkg"
    assert (tmp_path / "lib/pkg/sub/b.py").read_text() == "b" and not (tmp_path / "pkg").exists()
    assert "deleted old.txt" == await delete_file(c, "old.txt")
    assert "2 files" in await delete_file(c, "lib")
    assert not (tmp_path / "lib").exists()
    c.deps.checkpoints.undo()
    assert (tmp_path / "pkg/a.py").read_text() == "a" and (tmp_path / "pkg/sub/b.py").read_text() == "b"
    assert (tmp_path / "old.txt").read_text() == "old"
    assert not (tmp_path / "lib/pkg/a.py").exists()


async def test_delete_and_move_refuse(tmp_path):
    from jimbo.tools.fs import delete_file, move_file
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "d").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("keep")
    (tmp_path / "link").symlink_to(outside)
    c = ctx(tmp_path)
    for coro, why in [(delete_file(c, "link"), "symbolic link"), (delete_file(c, "nope"), "does not exist"),
                      (delete_file(c, "."), "workspace itself"), (delete_file(c, "../x"), "outside"),
                      (move_file(c, "a.txt", "b.txt"), "already exists"), (move_file(c, "d", "d/e"), "into itself"),
                      (move_file(c, "a.txt", "../a.txt"), "outside")]:
        with pytest.raises(ModelRetry, match=why):
            await coro
    assert outside.read_text() == "keep" and (tmp_path / "a.txt").exists()


async def test_file_tools_use_the_scratch_directory(tmp_path):
    from jimbo.tools.fs import delete_file, edit_file, move_file, write_file
    ws, scratch = tmp_path / "ws", tmp_path / "scratch"
    ws.mkdir(), scratch.mkdir()
    c = ctx(ws, scratch=scratch)
    c.deps.checkpoints.begin()
    await write_file(c, f"{scratch}/probe.py", "print(1)\n")
    await edit_file(c, f"{scratch}/probe.py", "1", "2")
    assert "print(2)" in await read_file(c, f"{scratch}/probe.py")
    await move_file(c, f"{scratch}/probe.py", "kept.py")  # out of scratch, into the workspace
    assert (ws / "kept.py").read_text() == "print(2)\n"
    await move_file(c, "kept.py", f"{scratch}/sub/back.py")
    await delete_file(c, f"{scratch}/sub")
    for coro, why in [(delete_file(c, str(scratch)), "scratch directory itself"),
                      (write_file(c, "/tmp/other.txt", "x"), f"outside the workspace; use the scratch directory {scratch}"),
                      (read_file(c, f"{tmp_path}/other.txt"), "use the scratch directory")]:
        with pytest.raises(ModelRetry, match=why):
            await coro
    c.deps.checkpoints.undo()
    assert not (scratch / "probe.py").exists() and not (ws / "kept.py").exists()
    with pytest.raises(ModelRetry, match="outside"):  # no scratch directory: the workspace only
        await write_file(ctx(ws), f"{scratch}/probe.py", "x")


def test_session_has_a_scratch_directory(tmp_path):
    from jimbo.agent import project_context
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=TestModel())
    assert s.deps.scratch == (tmp_path / "user" / "scratch" / s.session_id).resolve() and s.deps.scratch.is_dir()
    assert str(s.deps.scratch) in project_context(SimpleNamespace(deps=s.deps))


def test_old_scratch_folders_are_pruned(tmp_path):
    from jimbo.core.scratch import prune
    old, recent, touched = (tmp_path / n for n in ("old", "recent", "touched"))
    for d in (old, recent, touched):
        d.mkdir()
        (d / "f.txt").write_text("x")
    week_ago = time.time() - 8 * 86400
    for p in (old, old / "f.txt", touched):
        os.utime(p, (week_ago, week_ago))
    prune(tmp_path, 7)
    assert not old.exists() and recent.exists() and touched.exists()  # a recently changed file keeps its folder


async def test_move_rules_match_both_paths(tmp_path):
    rules = PatternRules(allow=["move_file:src/*"], deny=["move_file:*.env"])

    def info(src, dst):
        return ToolCallInfo(tool="move_file", args={"path": src, "destination": dst}, summary="", workspace=tmp_path)
    assert (await rules.evaluate(info("src/a", "src/b"))).action == "allow"
    assert (await rules.evaluate(info("src/a", "docs/b"))).action == "ask"
    assert (await rules.evaluate(info("src/a", "src/.env"))).action == "deny"


# ---- background shell ----

async def test_background_process_output_and_kill(tmp_path):
    from jimbo.tools.shell import kill_background, shell_kill, shell_output
    c = ctx(tmp_path)
    out = await shell(c, "echo ready; sleep 30", background=True)
    assert out.startswith("background process 1: running") and "ready" in out
    assert await shell_output(c, "1") == "running"  # nothing new since the last read
    out = await shell_kill(c, "1")
    assert out.startswith("killed")
    out = await shell(c, "echo one; sleep 1.5; echo two; exit 4", background=True)
    assert out.startswith("background process 2: running") and "one" in out  # the first second's output
    out = await shell_output(c, "2", wait=5)
    assert out.splitlines() == ["exited with code 4", "two"]
    with pytest.raises(ModelRetry, match="no background process"):
        await shell_output(c, "9")
    await shell(c, "sleep 30", background=True)
    await kill_background(c.deps)
    assert c.deps.processes["3"].status() == "killed"


async def test_background_output_keeps_the_newest(tmp_path, monkeypatch):
    from jimbo.tools import shell as sh
    monkeypatch.setattr(sh, "MAX_OUTPUT_BYTES", 100)
    c = ctx(tmp_path)
    await shell(c, "sleep 1.5; seq 1 1000", background=True)
    out = await sh.shell_output(c, "1", wait=5)
    assert "earlier bytes omitted" in out and out.endswith("1000") and "\n1\n" not in out


# ---- web_fetch ----

async def test_web_fetch(tmp_path, monkeypatch):
    from jimbo.tools import web
    from pydantic_ai.messages import BinaryContent
    pages = {"https://docs.example.com/x": {"url": "https://docs.example.com/x", "title": "X", "content": "body"},
             "https://example.com/doc.pdf": BinaryContent(b"%PDF", media_type="application/pdf")}

    async def fetch(url):
        return pages[url]
    monkeypatch.setattr(web, "_fetcher", lambda: fetch)
    c = ctx(tmp_path)
    assert await web.web_fetch(c, "https://docs.example.com/x") == "# X\nhttps://docs.example.com/x\n\nbody"
    with pytest.raises(ModelRetry, match="application/pdf"):
        await web.web_fetch(c, "https://example.com/doc.pdf")
    with pytest.raises(ModelRetry, match="not an http"):
        await web.web_fetch(c, "file:///etc/passwd")


def test_web_fetch_always_is_per_domain(tmp_path):
    from jimbo.core.approval import RememberedDecisions
    r = RememberedDecisions()

    def info(domain):
        return ToolCallInfo(tool="web_fetch", args={"url": f"https://{domain}/", "domain": domain}, summary="",
                            workspace=tmp_path)
    r.add(info("docs.python.org"))
    assert r.allows(info("docs.python.org")) and not r.allows(info("evil.example"))


async def test_plan_mode_asks_for_web_fetch(tmp_path):
    from jimbo.core.approval import PlanMode
    plan = PlanMode(lambda: True, None)
    fetch = ToolCallInfo(tool="web_fetch", args={"url": "https://a.b"}, summary="", workspace=tmp_path)
    delete = ToolCallInfo(tool="delete_file", args={"path": "a"}, summary="", workspace=tmp_path)
    assert (await plan.evaluate(fetch)).action == "ask"
    assert (await plan.evaluate(delete)).action == "deny"


async def test_web_fetch_names_curl_for_local_addresses(tmp_path):
    from jimbo.tools import web
    c = ctx(tmp_path)
    with pytest.raises(ModelRetry, match="use shell with curl"):
        await web.web_fetch(c, "http://127.0.0.1:9/")


@pytest.mark.parametrize("status, failed", [("exit code 0", False), ("exit code 10", True), ("timed out after 5s (killed)", True),
                                            ("background process 1: running", False), ("killed", False),
                                            ("background process 2: exited with code 3", True)])
def test_shell_failed(status, failed):
    from jimbo.core.stats import shell_failed
    assert shell_failed(status) is failed


# ---- show_image ----

async def test_show_image(tmp_path):
    from jimbo.tools.show import show_image
    shown = []

    async def show(file, path, media_type, caption):
        shown.append((file.name, path, media_type, caption))
    (tmp_path / "out").mkdir()
    (tmp_path / "out/plot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    (tmp_path / "chart.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "notes.txt").write_text("x")
    c = ctx(tmp_path, show_image=show)
    assert await show_image(c, "out/plot.png", " sales by month ") == "Showed out/plot.png to the user."
    await show_image(c, "chart.svg")
    assert shown == [("plot.png", "out/plot.png", "image/png", "sales by month"),
                     ("chart.svg", "chart.svg", "image/svg+xml", "")]
    for path, why in [("notes.txt", "not a .png"), ("nope.png", "not a file"), ("../x.png", "outside")]:
        with pytest.raises(ModelRetry, match=why):
            await show_image(c, path)


def test_show_image_is_offered_only_by_the_web_ui(tmp_path):
    from jimbo.frontends.web import WebFrontend
    assert build_session(settings(tmp_path), ScriptedFrontend(), model=TestModel()).deps.show_image is None
    web = build_session(settings(tmp_path), WebFrontend(open_browser=False), model=TestModel())
    assert web.deps.show_image is not None
