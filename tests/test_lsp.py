import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.agent import lsp_prompt
from jimbo.commands import describe_lsp
from jimbo.config import ServerConfig, Settings
from jimbo.core.agents import AgentSpec
from jimbo.core.approval import ApprovalGate, PlanMode, Verdict
from jimbo.core.deps import Deps
from jimbo.lsp.client import str_index, utf16_col
from jimbo.lsp.manager import LspManager
from jimbo.lsp.servers import RESTART_ON, resolve_servers
from jimbo.tools import lsp as t
from jimbo.tools.fs import edit_file, write_file
from jimbo.wiring import build_session

FAKE = [sys.executable, str(Path(__file__).with_name("fake_lsp.py"))]


def only(*found):
    return lambda command: command if command in found else None


class Recorder:
    """Allows everything except, when start=False, starting a server. Records what wasn't read-only."""

    name = "ask"

    def __init__(self, start=True):
        self.start, self.asked, self.calls = start, [], []

    async def evaluate(self, call):
        if call.read_only:
            return Verdict(action="allow", decided_by="readonly")
        self.asked.append(call.summary)
        self.calls.append(call)
        ok = self.start or call.tool != "lsp_start"
        return Verdict(action="allow" if ok else "deny", reason=None if ok else "not now", decided_by="user")


@pytest.fixture
async def make(tmp_path, monkeypatch):
    monkeypatch.setattr("jimbo.lsp.client.PROGRESS_GRACE", 0)  # the fake server reports no indexing
    managers = []

    def make(files=None, env=None, start=True, timeout=5.0, wait=2.0):
        ws = (tmp_path / "ws").resolve()
        ws.mkdir(exist_ok=True)
        for name, text in (files or {}).items():
            (ws / name).write_text(text)
        s = Settings(workspace=ws, user_dir=tmp_path / "user", lsp_servers={"fake": ServerConfig(
            command=FAKE, extensions=[".fk"], env=env or {}, settings={"fake": {"option": 7}})})
        choices, _ = resolve_servers(s, which=only(sys.executable))
        rec = Recorder(start)
        deps = Deps(workspace=ws, gate=ApprovalGate([PlanMode(lambda: deps.mode == "plan", None), rec]))
        deps.lsp = LspManager(ws, choices, timeout=timeout, wait=wait)
        managers.append(deps.lsp)
        return SimpleNamespace(tool_call_id="c1", deps=deps), rec

    yield make
    for m in managers:
        await m.close()


FILES = {"a.fk": "def alpha\n    beta()\nclass Box\n    def lid\n", "b.fk": "alpha()\nERROR: broken\n"}


def test_utf16_columns():
    assert utf16_col("a😀b", 2) == 3
    assert str_index("a😀b", 3) == 2


def test_find_symbol():
    line = "bool Ledger::remove_account(const std::string& name) {"
    assert t.find_symbol(line, "Ledger::remove_account") == line.index("remove_account")
    assert t.find_symbol(line, "remove_account") == line.index("remove_account")
    assert t.find_symbol(line, "remove") is None
    assert t.find_symbol("x = self.total + 1", "self.total") == 9


def test_resolve_servers():
    s = Settings(lsp_servers={"pyright": ServerConfig(command=["my-pyright"]), "odd": ServerConfig(command=["x"])},
                 lsp_disabled=["clangd"])
    choices, warnings = resolve_servers(s, which=only("my-pyright", "gopls", "rust-analyzer"))
    rows = {c.language: c for c in choices}
    assert rows["Python"].server.command == ("my-pyright",) and ".py" in rows["Python"].server.ids
    assert rows["C, C++"].server is None and rows["C, C++"].disabled == ("clangd",)
    assert rows["Go"].server.name == "gopls"
    assert rows["TypeScript, JavaScript"].server is None
    assert warnings == ["lsp_servers.odd: set command and extensions (it isn't a built-in server)"]
    choices, _ = resolve_servers(Settings(), which=only("pyright-langserver", "pylsp"))
    assert {c.language: c for c in choices}["Python"].server.name == "pyright"  # first found, in order


def test_workspace_config_cannot_set_servers(tmp_path, capsys):
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "config.toml").write_text('lsp_disabled = ["gopls"]\n')
    (tmp_path / ".jimbo").mkdir()
    (tmp_path / ".jimbo" / "config.toml").write_text(
        'lsp_disabled = ["clangd"]\nlsp_wait = 1.0\n[lsp_servers.evil]\ncommand = ["sh", "-c", "boom"]\n')
    s = Settings(workspace=tmp_path, user_dir=tmp_path / "user")
    assert s.lsp_servers == {} and s.lsp_disabled == ["gopls", "clangd"] and s.lsp_wait == 1.0
    assert "lsp_servers" in capsys.readouterr().err


async def test_read_tools(make):
    ctx, rec = make(FILES)
    out = await t.lsp_references(ctx, "a.fk", 1, "alpha")
    assert out.splitlines() == ["2 reference(s) to alpha in 2 file(s):", "a.fk:1  def alpha", "b.fk:1  alpha()"]
    assert rec.asked == ["start language server fake for fk"]
    start = rec.calls[0]  # the prompt names the command, and offers no "always": yes already lasts the session
    assert start.detail.startswith(f"$ {sys.executable}") and "until jimbo exits" in start.detail
    assert start.wording["title"] == "Start a language server?" and start.wording["always"] is None
    out = await t.lsp_definition(ctx, "b.fk", 1, "alpha")
    assert out.startswith("Definition of alpha:\na.fk:1  def alpha\n\n`alpha`: a fake symbol")
    assert "config [7]" in out  # the server's workspace/configuration request got the server's settings
    assert await t.lsp_symbols(ctx, path="a.fk") == "function alpha  line 1\nclass Box  line 3\n  function lid  line 4"
    assert await t.lsp_symbols(ctx, query="alpha") == "function alpha  a.fk:1"
    assert await t.lsp_diagnostics(ctx, "b.fk") == "b.fk:2: error: broken"
    assert await t.lsp_diagnostics(ctx) == "b.fk:2: error: broken"
    assert rec.asked == ["start language server fake for fk"]  # started once; the tools are read-only


async def test_bad_arguments(make):
    ctx, _ = make({**FILES, "notes.txt": "hi\n"})
    with pytest.raises(ModelRetry, match="'gamma' is not on line 1 of a.fk; the line is: def alpha"):
        await t.lsp_references(ctx, "a.fk", 1, "gamma")
    with pytest.raises(ModelRetry, match="no line 9"):
        await t.lsp_definition(ctx, "a.fk", 9, "alpha")
    with pytest.raises(ModelRetry, match="outside the workspace"):
        await t.lsp_definition(ctx, "../x.fk", 1, "alpha")
    assert await t.lsp_definition(ctx, "notes.txt", 1, "hi") == \
        "no language server for .txt files; there is one for: fk"


async def test_secret_files_are_refused(make):
    ctx, _ = make({".env": "KEY=1\n"})
    ctx.deps.secret_files = [".env"]
    with pytest.raises(ModelRetry, match="secret"):
        await t.lsp_symbols(ctx, path=".env")


async def test_declined_server_is_not_asked_again(make):
    ctx, rec = make(FILES, start=False)
    assert "was not started" in await t.lsp_references(ctx, "a.fk", 1, "alpha")
    assert "declined it this session" in await t.lsp_references(ctx, "a.fk", 1, "alpha")
    out = await edit_file(ctx, "b.fk", "broken", "still broken\nERROR: new")
    assert "Errors" not in out
    assert rec.asked == ["start language server fake for fk", "edit b.fk"]
    await ctx.deps.lsp.start("fake")  # /lsp start
    assert (await t.lsp_references(ctx, "a.fk", 1, "alpha")).startswith("2 reference(s)")


async def test_agent_start_prompt_names_the_agent(make):
    ctx, rec = make(FILES)
    ctx.deps.agent = AgentSpec("explore", "", "")
    await t.lsp_symbols(ctx, path="a.fk")
    assert rec.asked == ["[explore] start language server fake for fk"]


async def test_errors_after_edit(make):
    ctx, rec = make({"a.fk": "def alpha\nERROR: old\n"})
    out = await edit_file(ctx, "a.fk", "def alpha", "def alpha\nERROR: new one")
    assert out == ("edited a.fk (1 replacement(s))\n\nErrors after this edit (fake):\na.fk:2: new one\n"
                   "(1 error was there before this edit)")
    assert rec.asked == ["edit a.fk", "start language server fake for fk"]  # asked after the edit is written
    out = await write_file(ctx, "c.fk", "def gamma\nERROR: bad\n")
    assert out.endswith("Errors after this edit (fake):\nc.fk:2: bad")
    assert (await edit_file(ctx, "a.fk", "new one", "newer")).endswith("a.fk:2: newer\n(1 error was there before this edit)")
    assert await edit_file(ctx, "a.fk", "def alpha", "def alpha2") == "edited a.fk (1 replacement(s))"


async def test_slow_server_adds_nothing(make):
    ctx, _ = make({"a.fk": "def alpha\n"}, env={"FAKE_LSP_SILENT": "1"}, wait=0.3)
    assert await edit_file(ctx, "a.fk", "def alpha", "ERROR: x") == "edited a.fk (1 replacement(s))"


async def test_rename(make):
    ctx, rec = make({"a.fk": "def alpha\n", "b.fk": "alpha()\nalpha()\n"})
    ctx.deps.checkpoints.begin()
    out = await t.lsp_rename(ctx, "b.fk", 1, "alpha", "omega")
    assert out.splitlines()[:3] == ["renamed alpha to omega: 3 edit(s) in 2 file(s):", "  a.fk (1)", "  b.fk (2)"]
    assert rec.asked[-1] == "rename alpha to omega in 2 file(s)"
    ws = ctx.deps.workspace
    assert (ws / "a.fk").read_text() == "def omega\n" and (ws / "b.fk").read_text() == "omega()\nomega()\n"
    ctx.deps.checkpoints.undo()
    assert (ws / "a.fk").read_text() == "def alpha\n" and (ws / "b.fk").read_text() == "alpha()\nalpha()\n"


async def test_plan_mode(make):
    ctx, rec = make(FILES)
    ctx.deps.mode = "plan"
    assert (await t.lsp_references(ctx, "a.fk", 1, "alpha")).startswith("2 reference(s)")  # the start got through
    with pytest.raises(ModelRetry, match="plan mode"):
        await t.lsp_rename(ctx, "a.fk", 1, "alpha", "omega")
    assert (ctx.deps.workspace / "a.fk").read_text() == FILES["a.fk"]


def test_apply_edits():
    text = "a😀b\r\nxyz\n"
    edits = [{"range": {"start": {"line": 0, "character": 3}, "end": {"line": 0, "character": 4}}, "newText": "B"},
             {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 1}}, "newText": "X"}]
    assert t.apply_edits(text, edits) == "a😀B\r\nXyz\n"


async def test_crashing_server_restarts_then_stays_off(make):
    ctx, rec = make(FILES, env={"FAKE_LSP_CRASH": "textDocument/references"})
    for _ in range(3):
        assert "exited" in await t.lsp_references(ctx, "a.fk", 1, "alpha")
    assert "exited 3 times and is off" in await t.lsp_references(ctx, "a.fk", 1, "alpha")
    assert rec.asked == ["start language server fake for fk"]  # restarts don't ask again


async def test_timeout(make):
    ctx, _ = make(FILES, env={"FAKE_LSP_HANG": "textDocument/references"}, timeout=0.5)
    assert await t.lsp_references(ctx, "a.fk", 1, "alpha") == \
        "language server fake didn't answer textDocument/references in 0.5s"
    assert (await t.lsp_symbols(ctx, path="a.fk")).startswith("function alpha")  # still running


async def test_close_stops_servers(make):
    ctx, _ = make(FILES)
    await t.lsp_symbols(ctx, path="a.fk")
    client = ctx.deps.lsp.clients["fake"]
    await ctx.deps.lsp.close()
    assert client._proc.returncode is not None and not ctx.deps.lsp.clients


async def test_describe(make):
    ctx, _ = make(FILES)
    await t.lsp_symbols(ctx, path="a.fk")
    text = describe_lsp(ctx.deps.lsp)
    assert "  fk                      fake  running, 1 file(s) open" in text
    assert "Rust" in text and "not installed (tried rust-analyzer)" in text


async def test_session_wiring(tmp_path):
    frontend = ScriptedFrontend()
    s = build_session(settings(tmp_path), frontend, model=TestModel())
    assert s.deps.lsp is None and not s.deps.can_use_lsp
    await s.commands.dispatch(s, "/lsp")
    assert frontend.events[-1].data["text"] == "Language servers are off (lsp = false)."
    s = build_session(settings(tmp_path, lsp=True, lsp_servers={
        "fake": ServerConfig(command=FAKE, extensions=[".fk"])}), ScriptedFrontend(), model=TestModel())
    assert s.deps.can_use_lsp and "fk" in s.deps.lsp.languages()
    s.deps.agent = AgentSpec("reader", "", "", tools=["read_file"])
    assert not s.deps.can_use_lsp  # an agent without lsp tools gets no prompt about them


def test_prompt_lists_the_tools_a_session_has(make):
    ctx, _ = make()
    text = lsp_prompt(ctx.deps)
    assert text.startswith("# Language servers\nFor fk files, use these instead of grep:\n- the functions")
    assert "lsp_rename(" in text and text.endswith("list errors an edit introduced; fix them.")
    ctx.deps.agent = AgentSpec("explore", "", "", tools=["read_file", "lsp_references"])
    text = lsp_prompt(ctx.deps)
    assert "lsp_references(" in text and "lsp_rename" not in text and "lsp_symbols" not in text


async def test_blocked_file_gets_a_note(make, monkeypatch):
    monkeypatch.setitem(t.BLOCKING, "fake", ({"blocked"}, "fake stopped checking {file}"))
    ctx, _ = make({"a.fk": "def alpha\n"})
    out = await edit_file(ctx, "a.fk", "def alpha", "BLOCK: no header\ndef alpha")
    assert out.endswith("a.fk:1: no header\nfake stopped checking a.fk")
    out = await edit_file(ctx, "a.fk", "def alpha", "def beta")  # no new errors, but the file still isn't checked
    assert out == "edited a.fk (1 replacement(s))\n\nfake stopped checking a.fk"
    assert (await t.lsp_diagnostics(ctx, "a.fk")).endswith("\nfake stopped checking a.fk")


async def test_config_change_restarts_server_without_asking(make, monkeypatch):
    monkeypatch.setitem(RESTART_ON, "fake", ("fake.cfg",))
    ctx, rec = make(FILES)
    await t.lsp_symbols(ctx, path="a.fk")
    first = ctx.deps.lsp.clients["fake"]
    await t.lsp_symbols(ctx, path="a.fk")
    assert ctx.deps.lsp.clients["fake"] is first
    (ctx.deps.workspace / "fake.cfg").write_text("x")
    assert (await t.lsp_symbols(ctx, path="a.fk")).startswith("function alpha")
    assert ctx.deps.lsp.clients["fake"] is not first and not first.alive
    assert rec.asked == ["start language server fake for fk"] and not ctx.deps.lsp.exits
