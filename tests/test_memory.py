"""remember: notes saved to ~/.jimbo/memory.md or .jimbo/memory.md, and read into the instructions."""

from pydantic_ai.messages import ToolReturnPart

from helpers import ScriptedFrontend, settings
from jimbo.core.skills import load_project_instructions
from jimbo.wiring import build_session
from test_plan import model, retries


def results(s):
    return [str(p.content) for m in s.history for p in m.parts if isinstance(p, ToolReturnPart)]


def session(tmp_path, calls, allow=True):
    fe = ScriptedFrontend(allow=allow)
    return build_session(settings(tmp_path), fe, model=model(calls)), fe


async def test_notes_go_to_the_scope_asked_for_and_apply_now(tmp_path):
    s, _ = session(tmp_path, [("remember", {"note": "I prefer short answers", "scope": "user"}),
                              ("remember", {"note": "- Run tests with make check", "scope": "project"})])
    await s.send("remember these")
    assert (tmp_path / "user" / "memory.md").read_text() == "- I prefer short answers\n"
    assert (tmp_path / ".jimbo" / "memory.md").read_text() == "- Run tests with make check\n"
    assert "I prefer short answers" in s.deps.project_instructions  # without a restart
    assert "project memory (.jimbo/memory.md)" in s.deps.project_instructions


async def test_replace_and_duplicates(tmp_path):
    (tmp_path / ".jimbo").mkdir()
    (tmp_path / ".jimbo" / "memory.md").write_text("# notes\n- use pip\n- keep it short\n")
    s, _ = session(tmp_path, [("remember", {"note": "keep it short", "scope": "project"}),
                              ("remember", {"note": "use uv, not pip", "scope": "project", "replaces": "use pip"}),
                              ("remember", {"note": "x", "scope": "project", "replaces": "not there"})])
    await s.send("go")
    assert (tmp_path / ".jimbo" / "memory.md").read_text() == "# notes\n- use uv, not pip\n- keep it short\n"
    assert results(s)[0].startswith("Already in") and any("matches 0" in r for r in retries(s))


async def test_a_denied_note_isnt_saved_and_undo_removes_one(tmp_path):
    s, _ = session(tmp_path, [("remember", {"note": "n", "scope": "user"})], allow=False)
    await s.send("go")
    assert not (tmp_path / "user" / "memory.md").exists()
    s, fe = session(tmp_path, [("remember", {"note": "n", "scope": "user"})])
    await s.send("go")
    await s.commands.dispatch(s, "/diff")
    assert "+- n" in [e for e in fe.events if e.kind == "command_output"][-1].data["text"]
    await s.commands.dispatch(s, "/undo")
    assert not (tmp_path / "user" / "memory.md").exists() and "- n" not in s.deps.project_instructions


def test_memory_is_read_after_instructions(tmp_path):
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "memory.md").write_text("- mine")
    (tmp_path / "AGENTS.md").write_text("rules")
    text = load_project_instructions(tmp_path / "user", tmp_path)
    assert text.index("rules") < text.index("- mine")
