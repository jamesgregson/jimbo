from pydantic_ai.messages import ModelRequest, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

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
        seen.append((messages[-1].parts[-1].content, info.instructions or ""))
        yield "ok"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=model))
    await s.commands.dispatch(s, "/skills")
    out = [e.data for e in fe.events if e.kind == "command_output"][-1]["text"]
    assert "- deploy: deploy the app" in out
    await s.commands.dispatch(s, "/deploy to staging")
    # the message is the user's own words; the skill is in the instructions, and no longer offered for loading
    assert seen[0][0] == "to staging"
    assert "# Active skill: deploy" in seen[0][1] and "Run make deploy." in seen[0][1]
    assert "- deploy: deploy the app" not in seen[0][1]
    await s.send("carry on")  # it stays active on later turns
    assert seen[1][0] == "carry on" and "Run make deploy." in seen[1][1]
    await s.commands.dispatch(s, "/clear")
    await s.send("hello")
    assert "Run make deploy." not in seen[2][1] and "- deploy: deploy the app" in seen[2][1]
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
        deps, tool_call_id = s.deps, None

    assert "echo hi" in await read_file(Ctx, str(skill / "run.sh"))
    with pytest.raises(ModelRetry):
        await read_file(Ctx, str(tmp_path / "secret.txt"))
    with pytest.raises(ModelRetry):
        await read_file(Ctx, str(skill / ".." / ".." / ".." / "secret.txt"))


async def test_skill_files_apply_from_the_next_input(tmp_path):
    import os
    from jimbo.core.runner import _handle

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, bundled=True), fe, model=TestModel(call_tools=[]))
    helps = lambda: {c["name"]: c["help"] for c in s.commands.describe()}
    make_skill(tmp_path, "deploy", "---\ndescription: ship it\n---\nRun make deploy.")
    await _handle(s, "hi")
    assert s.deps.skills["deploy"].body == "Run make deploy." and helps()["deploy"] == "ship it"
    assert [e for e in fe.events if e.kind == "commands"]  # completion gets the new command

    path = tmp_path / ".jimbo" / "skills" / "deploy" / "SKILL.md"
    path.write_text("---\ndescription: ship it now\n---\nRun make release.")
    os.utime(path, ns=(1, 1))  # a different mtime, even on a coarse clock
    await s.refresh_files()
    assert s.deps.skills["deploy"].body == "Run make release." and helps()["deploy"] == "ship it now"

    make_skill(tmp_path, "review", "---\ndescription: mine\n---\nmy way")  # replaces the bundled /review
    await s.refresh_files()
    assert helps()["review"] == "mine"
    (tmp_path / ".jimbo" / "skills" / "review" / "SKILL.md").unlink()
    path.unlink()
    await s.refresh_files()
    assert "deploy" not in s.deps.skills and "deploy" not in helps()
    assert helps()["review"] != "mine" and "help" in helps()  # the bundled command is back; built-ins stay


async def test_the_model_can_reload_skills_mid_run(tmp_path):
    import json
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    make_skill(tmp_path, "old", "---\ndescription: old\n---\nv1")
    steps = iter([("write_file", {"path": ".jimbo/skills/new/SKILL.md", "content": "---\ndescription: new\n---\nhi"}),
                  ("write_file", {"path": ".jimbo/skills/old/SKILL.md", "content": "---\ndescription: old\n---\nv2"}),
                  ("reload_skills", {}), ("load_skill", {"name": "new"})])

    async def stream(messages, info):
        if (nxt := next(steps, None)) is not None:
            yield {0: DeltaToolCall(name=nxt[0], json_args=json.dumps(nxt[1]))}
        else:
            yield "done"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=stream))
    await s.send("add a skill and use it")
    results = [str(p.content) for m in s.history for p in m.parts if isinstance(p, ToolReturnPart)]
    assert "added: new" in results[2] and "changed: old" in results[2]
    assert results[3].endswith("hi")  # loaded in the same run
    assert "new" in {c["name"] for c in [e for e in fe.events if e.kind == "commands"][-1].data["commands"]}


async def test_reload_command_rereads_skills_and_agents(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=TestModel(call_tools=[]))
    make_skill(tmp_path, "deploy", "---\ndescription: ship it\n---\nRun make deploy.")
    (tmp_path / ".jimbo" / "agents").mkdir(parents=True)
    (tmp_path / ".jimbo" / "agents" / "helper.md").write_text("---\ndescription: helps\n---\nHelp.")
    await s.commands.dispatch(s, "/reload")
    text = [e for e in fe.events if e.kind == "command_output"][-1].data["text"]
    assert "added: deploy" in text and "Agents reloaded" in text
    assert "deploy" in s.deps.skills and "helper" in s.deps.agents
    await s.commands.dispatch(s, "/reload")
    assert "No skill changed." in [e for e in fe.events if e.kind == "command_output"][-1].data["text"]


async def test_an_active_skill_raises_the_turn_request_limit(tmp_path):
    make_skill(tmp_path, "long", "---\ndescription: long work\nrequest_limit: 80\n---\nWork.\n")
    make_skill(tmp_path, "short", "---\ndescription: short work\nrequest_limit: 5\n---\nWork.\n")
    limits = []

    async def model(messages, info):
        limits.append(s.deps.request_limit)
        yield "ok"

    s = build_session(settings(tmp_path, request_limit=50), ScriptedFrontend(),
                      model=FunctionModel(stream_function=model))
    await s.send("before")
    await s.commands.dispatch(s, "/long")
    await s.send("carry on")
    assert limits == [50, 80, 80] and s.request_limit == 50  # raised for the skill's turns only
    await s.commands.dispatch(s, "/short")  # a skill can't lower the setting
    assert limits[-1] == 50


async def test_the_active_skill_is_saved_with_the_session(tmp_path):
    make_skill(tmp_path, "deploy", "---\ndescription: deploy the app\n---\nRun make deploy.\n")

    async def model(messages, info):
        yield "ok"

    s = build_session(settings(tmp_path, log_dir=tmp_path / "logs"), ScriptedFrontend(),
                      model=FunctionModel(stream_function=model))
    await s.commands.dispatch(s, "/deploy")
    sid = s.session_id
    await s.commands.dispatch(s, "/clear")
    assert s.deps.active_skill is None
    await s.resume(sid)
    assert s.deps.active_skill == "deploy"
