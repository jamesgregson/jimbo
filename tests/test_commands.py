import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.core.commands import CommandError, CommandRegistry, QuitSession, parse_frontmatter
from jimbo.core.runner import run_frontend
from jimbo.wiring import build_session


def outputs(fe):
    return [e.data for e in fe.events if e.kind == "command_output"]


def last(fe):
    return outputs(fe)[-1]


def make(tmp_path, fake=None, **kw):
    """`fake` is the stand-in model object; `model=` in kw is the model *setting*."""
    fe = ScriptedFrontend()
    return build_session(settings(tmp_path, **kw), fe, model=fake or TestModel()), fe


def recording_model(seen):
    async def stream(messages, info):
        seen.append(messages[-1].parts[-1].content)
        yield "ok"
    return FunctionModel(stream_function=stream)


# ---- parsing and dispatch ----

@pytest.mark.parametrize("text,handled", [
    ("/help", True), ("  /help  ", True), ("/help with args", True), ("/help\nmulti\nline", True),
    ("/nope", True),  # unknown commands are handled (with an error), not sent to the model
    ("hello", False), ("/Users/me/file.py is broken", False), ("/ help", False), ("/1x", False),
    ("please /help", False),
])
async def test_what_counts_as_a_command(tmp_path, text, handled):
    s, fe = make(tmp_path)
    assert await s.commands.dispatch(s, text) is handled
    assert not any(e.kind == "user_message" for e in fe.events)


async def test_args_are_passed_stripped(tmp_path):
    s, fe = make(tmp_path)
    got = []

    async def echo(session, args):
        got.append(args)
        return None  # None: no command_output event

    s.commands.add("echo", "echo args", echo)
    await s.commands.dispatch(s, "/echo   a  b\nc  ")
    await s.commands.dispatch(s, "/echo")
    assert got == ["a  b\nc", ""] and outputs(fe) == []


async def test_handler_errors(tmp_path):
    s, fe = make(tmp_path)

    async def bad_args(session, args):
        raise CommandError("usage: /x <thing>")

    async def crash(session, args):
        raise RuntimeError("boom")

    s.commands.add("x", "", bad_args)
    s.commands.add("crash", "", crash)
    await s.commands.dispatch(s, "/x")
    assert last(fe) == {"command": "x", "ok": False, "text": "usage: /x <thing>"}
    await s.commands.dispatch(s, "/crash")
    assert fe.events[-1].kind == "error" and fe.events[-1].data == {"type": "RuntimeError", "message": "boom"}
    await s.commands.dispatch(s, "/nope")
    assert last(fe) == {"command": "nope", "ok": False, "text": "unknown command /nope; try /help"}


def test_describe_is_sorted():
    reg = CommandRegistry()
    for name in ("zeta", "alpha", "mid"):
        reg.add(name, f"{name} help", None)
    assert [c["name"] for c in reg.describe()] == ["alpha", "mid", "zeta"]
    assert "mid" in reg and "other" not in reg


# ---- run_frontend ----

async def test_run_frontend_dispatches_and_quits(tmp_path):
    fe = ScriptedFrontend(["/help", "", "hi", "/quit", "never sent"])
    s = build_session(settings(tmp_path), fe, model=TestModel())
    await run_frontend(s, fe)
    kinds = [e.kind for e in fe.events]
    assert kinds[0] == "commands" and "command_output" in kinds
    assert {"name": "quit", "help": "exit jimbo"} in fe.events[0].data["commands"]
    assert [e.data["text"] for e in fe.events if e.kind == "user_message"] == ["hi"]
    assert fe.inputs == ["never sent"]


# ---- built-ins ----

async def test_help_lists_every_command(tmp_path):
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, "/help")
    text = last(fe)["text"]
    for name in ("approve", "clear", "help", "model", "quit", "todos", "usage"):
        assert f"/{name} — " in text


async def test_clear(tmp_path):
    s, fe = make(tmp_path)
    await s.send("hi")
    assert s.history and s.context_tokens > 0
    await s.commands.dispatch(s, "/clear")
    assert s.history == [] and s.context_tokens == 0
    assert last(fe)["text"] == "Conversation cleared."


async def test_model_show_switch_and_reject(tmp_path):
    s, fe = make(tmp_path, model="ollama:first")
    await s.commands.dispatch(s, "/model")
    assert last(fe)["text"] == "model: ollama:first"
    before = s.agent
    await s.commands.dispatch(s, "/model openrouter:vendor/m")
    assert last(fe) == {"command": "model", "ok": True, "text": "model: openrouter:vendor/m"}
    assert s.agent is not before
    for bad, why in [("bogus", "<provider>:<name>"), ("nope:m", "unknown provider")]:
        agent = s.agent
        await s.commands.dispatch(s, f"/model {bad}")
        assert not last(fe)["ok"] and why in last(fe)["text"]
        assert s.agent is agent  # a failed switch keeps the current model
    await s.commands.dispatch(s, "/model")
    assert last(fe)["text"] == "model: openrouter:vendor/m"


async def test_usage(tmp_path):
    s, fe = make(tmp_path, context_window=100_000)
    await s.commands.dispatch(s, "/usage")
    assert last(fe)["text"] == "0 requests · 0 in · 0 out\ncontext: 0 / 100000 tokens (0%)"
    await s.send("hi")
    await s.commands.dispatch(s, "/usage")
    u = s.total_usage
    assert last(fe)["text"].startswith(f"{u.requests} requests · {u.input_tokens} in · {u.output_tokens} out")
    s.context_window = None
    await s.commands.dispatch(s, "/usage")
    assert "window unknown" in last(fe)["text"]


async def test_approve(tmp_path):
    s, fe = make(tmp_path)
    fe.allow = False
    await s.commands.dispatch(s, "/approve")
    assert last(fe)["text"] == "approval: rules → readonly → remembered → ask"
    await s.commands.dispatch(s, "/approve yolo")
    assert last(fe)["text"] == "approval: rules → yolo"
    await s.deps.authorize("write_file", {}, "w")  # would raise if the frontend were asked
    await s.commands.dispatch(s, "/approve default")
    assert last(fe)["text"].endswith("→ ask")
    await s.commands.dispatch(s, "/approve maybe")
    assert last(fe) == {"command": "approve", "ok": False, "text": "usage: /approve [yolo|default]"}


async def test_approve_default_after_starting_in_yolo(tmp_path):
    s, fe = make(tmp_path, approval=["rules", "yolo"])  # what --yolo sets
    await s.commands.dispatch(s, "/approve")
    assert last(fe)["text"] == "approval: rules → yolo"
    await s.commands.dispatch(s, "/approve default")
    assert last(fe)["text"] == "approval: rules → readonly → remembered → ask"


async def test_yolo_keeps_deny_rules(tmp_path):
    from jimbo.core.deps import ToolDenied
    s, fe = make(tmp_path, approval_deny=["shell:sudo *"])
    await s.commands.dispatch(s, "/approve yolo")
    with pytest.raises(ToolDenied):
        await s.deps.authorize("shell", {"command": "sudo ls"}, "run")


async def test_quit_raises():
    reg = CommandRegistry()
    from jimbo.commands import register_builtins
    from jimbo.config import Settings
    register_builtins(reg, Settings(), {})
    with pytest.raises(QuitSession):
        await reg.dispatch(None, "/quit")


# ---- markdown commands ----

def test_parse_frontmatter():
    assert parse_frontmatter("---\ndescription: fix it\nother: a: b\n---\n\nBody\n") == (
        {"description": "fix it", "other": "a: b"}, "Body\n")  # invalid YAML falls back to flat parsing
    meta, body = parse_frontmatter("---\ndescription: >\n  line one\n  line two\nmetadata:\n  v: '1'\n---\nB")
    assert meta == {"description": "line one line two\n", "metadata": {"v": "1"}} and body == "B"
    assert parse_frontmatter("no frontmatter") == ({}, "no frontmatter")
    assert parse_frontmatter("---\nunclosed: yes\nbody") == ({}, "---\nunclosed: yes\nbody")


async def test_markdown_commands(tmp_path):
    cmds = tmp_path / ".jimbo" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "fix.md").write_text("---\ndescription: fix an issue\n---\nPlease fix $ARGUMENTS now.\n")
    (cmds / "review.md").write_text("Review the code.")
    (cmds / "help.md").write_text("shadowing a builtin is ignored")
    (cmds / "bad name.md").write_text("not a valid command name")
    (cmds / "notes.txt").write_text("not markdown")
    seen = []
    s, fe = make(tmp_path, fake=recording_model(seen))
    names = {c["name"]: c["help"] for c in s.commands.describe()}
    assert names["fix"] == "fix an issue"
    assert names["review"].startswith("prompt from ") and names["review"].endswith("review.md")
    assert "bad name" not in names and "notes" not in names
    await s.commands.dispatch(s, "/fix the login bug")
    await s.commands.dispatch(s, "/review src/a.py")
    await s.commands.dispatch(s, "/review")
    await s.commands.dispatch(s, "/help")
    assert seen == ["Please fix the login bug now.\n", "Review the code.\n\nsrc/a.py", "Review the code."]
    assert "shadowing" not in last(fe)["text"]
    # a markdown command is a normal turn: it shows up as the user's message
    assert [e.data["text"] for e in fe.events if e.kind == "user_message"][0] == "Please fix the login bug now.\n"


async def test_workspace_commands_override_user_commands(tmp_path):
    user = tmp_path / "user" / "commands"
    ws = tmp_path / ".jimbo" / "commands"
    user.mkdir(parents=True)
    ws.mkdir(parents=True)
    (user / "ship.md").write_text("user version")
    (user / "mine.md").write_text("only in user dir")
    (ws / "ship.md").write_text("project version")
    seen = []
    s, _ = make(tmp_path, fake=recording_model(seen))
    await s.commands.dispatch(s, "/ship")
    await s.commands.dispatch(s, "/mine")
    assert seen == ["project version", "only in user dir"]


async def test_missing_command_dirs_are_fine(tmp_path):
    s, _ = make(tmp_path)  # neither user/commands nor .jimbo/commands exists
    assert {c["name"] for c in s.commands.describe()} == {"approve", "clear", "compact", "help", "model", "quit", "skills", "todos", "usage"}
