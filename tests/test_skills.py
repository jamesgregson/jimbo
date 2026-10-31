from pydantic_ai.messages import ModelRequest, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.config import Settings
from jimbo.core.skills import load_project_instructions, load_skills
from jimbo.wiring import build_session


def make_skill(root, name, text):
    d = root / ".jimbo" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text)


def instructions_of(messages):
    return next(m.instructions for m in reversed(messages) if isinstance(m, ModelRequest))


def test_project_instructions_user_then_workspace(tmp_path):
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "AGENTS.md").write_text("be nice")
    (tmp_path / "AGENTS.md").write_text("use tabs\n")
    (tmp_path / "JIMBO.md").write_text("   ")  # blank files are ignored
    text = load_project_instructions(tmp_path / "user", tmp_path)
    assert text.index("be nice") < text.index("use tabs") and "JIMBO.md" not in text


def test_project_instructions_missing_and_capped(tmp_path):
    assert load_project_instructions(tmp_path / "nope", tmp_path) == ""
    (tmp_path / "AGENTS.md").write_text("x" * 50_000)
    assert len(load_project_instructions(tmp_path / "nope", tmp_path)) < 21_000


def test_load_skills(tmp_path):
    make_skill(tmp_path, "deploy", "---\nname: ship-it\ndescription: deploy the app\n---\nRun make deploy.\n")
    make_skill(tmp_path, "plain", "just a body")
    make_skill(tmp_path, "bad name", "---\ndescription: x\n---\nignored")
    skills = load_skills([tmp_path / ".jimbo" / "skills"])
    assert set(skills) == {"ship-it", "plain"}
    assert skills["ship-it"].description == "deploy the app" and skills["ship-it"].body == "Run make deploy.\n"


async def test_instructions_and_load_skill_tool(tmp_path):
    (tmp_path / "AGENTS.md").write_text("always use pytest")
    make_skill(tmp_path, "deploy", "---\ndescription: deploy the app\n---\nRun make deploy.\n")
    seen, calls = [], []

    async def model(messages, info):
        seen.append(instructions_of(messages))
        if len(calls) == 0:
            calls.append(1)
            from pydantic_ai.models.function import DeltaToolCall
            yield {0: DeltaToolCall(name="load_skill", json_args='{"name": "deploy"}')}
        else:
            yield "done"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=model))
    await s.send("ship it")
    assert "always use pytest" in seen[0] and "- deploy: deploy the app" in seen[0]
    returns = [p for m in s.history for p in m.parts if p.part_kind == "tool-return"]
    assert "Run make deploy." in returns[0].content


async def test_skill_command_and_listing(tmp_path):
    make_skill(tmp_path, "deploy", "---\ndescription: deploy the app\n---\nRun make deploy.\n")
    make_skill(tmp_path, "help", "must not shadow the builtin")
    seen = []

    async def model(messages, info):
        seen.append(messages[-1].parts[-1].content)
        yield "ok"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=model))
    await s.commands.dispatch(s, "/skills")
    out = [e.data for e in fe.events if e.kind == "command_output"][-1]["text"]
    assert "- deploy: deploy the app" in out
    await s.commands.dispatch(s, "/deploy to staging")
    assert "Run make deploy." in seen[0] and seen[0].endswith("to staging")
    assert {c["name"]: c["help"] for c in s.commands.describe()}["help"] == "list commands"


async def test_unknown_skill_is_retried(tmp_path):
    make_skill(tmp_path, "deploy", "body")
    fe = ScriptedFrontend()
    n = []

    async def model(messages, info):
        if not n:
            n.append(1)
            from pydantic_ai.models.function import DeltaToolCall
            yield {0: DeltaToolCall(name="load_skill", json_args='{"name": "zzz"}')}
        else:
            yield "ok"

    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=model))
    await s.send("x")
    retries = [p for m in s.history for p in m.parts if getattr(p, "outcome", None) == "retried"]
    assert "unknown skill" in retries[0].content


def test_yaml_frontmatter_and_extra_dirs(tmp_path):
    for sub in (".agents", ".claude"):
        d = tmp_path / sub / "skills" / "s"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: s\ndescription: >\n  folded\n  text\nlicense: MIT\n---\nbody")
    (tmp_path / ".claude" / "skills" / "s" / "SKILL.md").write_text("---\ndescription: loses\n---\nclaude")
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=FunctionModel(stream_function=lambda m, i: None))
    assert s.deps.skills["s"].description == "folded text" and s.deps.skills["s"].body == "body"  # .agents wins


async def test_read_file_allowed_in_skill_dir_only(tmp_path):
    import pytest
    from pydantic_ai import ModelRetry
    from jimbo.tools.fs import read_file
    ws = tmp_path / "ws"
    ws.mkdir()
    skill = tmp_path / "user" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
    (skill / "run.sh").write_text("echo hi")
    (tmp_path / "secret.txt").write_text("nope")
    s = build_session(Settings(workspace=ws, user_dir=tmp_path / "user"), ScriptedFrontend(),
                      model=FunctionModel(stream_function=lambda m, i: None))

    class Ctx:
        deps = s.deps

    assert "echo hi" in await read_file(Ctx, str(skill / "run.sh"))
    with pytest.raises(ModelRetry):
        await read_file(Ctx, str(tmp_path / "secret.txt"))
    with pytest.raises(ModelRetry):
        await read_file(Ctx, str(skill / ".." / ".." / ".." / "secret.txt"))
