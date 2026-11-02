import asyncio
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
    s = sb.Sandbox([tmp_path])
    c = ctx(tmp_path, sandbox=s)
    try:
        out = await shell(c, f"touch inside.txt && echo ok; touch {outside}; echo rc=$?")
        assert "ok" in out and "rc=1" in out and "Operation not permitted" in out
        assert (tmp_path / "inside.txt").exists() and not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_build_sandbox_modes(tmp_path, monkeypatch):
    assert sb.build_sandbox("off", tmp_path, [], True) is None
    monkeypatch.setattr(sb, "available", lambda: False)
    assert sb.build_sandbox("auto", tmp_path, [], True) is None
    with pytest.raises(ValueError):
        sb.build_sandbox("on", tmp_path, [], True)
    monkeypatch.setattr(sb, "available", lambda: True)
    s = sb.build_sandbox("auto", tmp_path, [tmp_path / "extra"], False)
    assert tmp_path.resolve() in s.writable and (tmp_path / "extra").resolve() in s.writable
    assert "network-outbound" in s.profile()


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
