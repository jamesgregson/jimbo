import asyncio
import json
from io import StringIO

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from helpers import ScriptedFrontend, settings
from jimbo.agent import AGENT_TOOL_NAMES
from jimbo.config import Settings
from jimbo.core.agents import load_agents
from jimbo.core.approval import ApprovalDecision
from jimbo.core.commands import parse_frontmatter
from jimbo.core.events import Event
from jimbo.core.models import ModelRouter
from jimbo.core.runner import _handle, _steer_or_refuse
from jimbo.frontends.cli_plain import PlainFrontend
from jimbo.frontends.tui import TuiFrontend
from jimbo.providers.preflight import ModelFacts
from jimbo.wiring import build_session


def call(tool, **args):
    return {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}


def scripted(*steps, log=None):
    """A model that plays `steps` (text, or a tool call), one per request, recording what it was told."""
    it = iter(steps)

    async def stream(messages, info):
        if log is not None:
            log.append(info)
            log.instructions = next(m.instructions for m in reversed(messages) if isinstance(m, ModelRequest))
        yield next(it)
    return FunctionModel(stream_function=stream)


class Log(list):
    instructions = ""


def write_agent(root, name, text):
    d = root / ".jimbo" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


def make(tmp_path, *main, models=None, inspector=None, frontend=None, **kw):
    """A session whose main model plays `main`, with `models` mapping spec -> model for everything else."""
    models = models or {}
    aliases = {"main": "t:main", "fast": "t:fast", "smart": "t:smart", **kw.pop("model_aliases", {})}
    fe = frontend or ScriptedFrontend()
    built = {"t:main": scripted(*main), **models}
    s = build_session(settings(tmp_path, model_aliases=aliases, **kw), fe, model_builder=lambda spec, _: built[spec],
                      model_inspector=inspector)
    return s, fe


def kinds(fe, kind):
    return [e.data for e in fe.events if e.kind == kind]


# ---- R1: renamed tasks ----

@pytest.mark.parametrize("toml", ["[models]\nshell_assess = 'main'\n", "[models]\ncompaction = 'main'\n",
                                  "[tasks.shell_assess]\nmode = 'off'\n"])
def test_old_task_names_stop_startup(tmp_path, toml):
    user = tmp_path / "user"
    user.mkdir()
    (user / "config.toml").write_text(toml)
    with pytest.raises(ValueError, match="renamed"):
        Settings(workspace=tmp_path, user_dir=user)


# ---- R2: loading agents ----

def test_project_agents_replace_personal_and_built_in(tmp_path):
    user, repo = tmp_path / "user", tmp_path / "repo"
    for d, text in ((user, "personal"), (repo, "project")):
        d.mkdir()
        (d / "reviewer.md").write_text(text)
    (repo / "explore.md").write_text("mine")
    agents, problems = load_agents([user, repo], AGENT_TOOL_NAMES)
    assert agents["reviewer"].body == "project" and agents["explore"].body == "mine" and problems == []
    assert agents["general"].path is None


@pytest.mark.parametrize("name,text", [
    ("main", "x"), ("jimbo-x", "x"), ("bad", "---\ntools: [\n---\nx"), ("bad", "---\ntools: [nope]\n---\nx"),
    ("bad", "---\ntools: [run_agent]\n---\nx"), ("bad", "---\ntools: [exit_plan]\n---\nx"),
    ("bad", "---\ntools: read_file\n---\nx"), ("bad", "---\nskills: x\n---\nx")])
def test_bad_agent_files_are_skipped_with_a_warning(tmp_path, name, text):
    (tmp_path / f"{name}.md").write_text(text)
    agents, problems = load_agents([tmp_path], AGENT_TOOL_NAMES)
    assert name not in agents and len(problems) == 1


def test_startup_prints_load_problems(tmp_path, capsys):
    write_agent(tmp_path, "bad", "---\ntools: [nope]\n---\nx")
    write_agent(tmp_path, "good", "---\nskills: [missing]\n---\nx")
    make(tmp_path)
    err = capsys.readouterr().err
    assert "skipped agent" in err and "unknown skill 'missing'" in err


def test_strict_frontmatter_raises_and_lenient_does_not():
    bad = "---\na: [\n---\nbody"
    assert parse_frontmatter(bad)[1] == "body"
    with pytest.raises(Exception):
        parse_frontmatter(bad, strict=True)


# ---- R3: choosing the model ----

def router(**kw):
    s = Settings.model_construct(model_aliases={"main": "p:main", "fast": "p:fast", "smart": "p:smart"},
                                 models=kw.get("models", {}))
    return ModelRouter(s, lambda spec, _: spec)


def test_model_precedence():
    r = router(models={"reviewer": "smart"})
    assert r.for_agent("other", "fast")[0].spec == "p:fast"
    assert r.for_agent("other")[0].spec == "p:main"
    assert r.for_agent("other", "main")[0].spec == "p:main"
    choice, _ = r.for_agent("reviewer", "fast")  # [models] beats the file
    assert (choice.spec, choice.source) == ("p:smart", "config")
    r.set_override("reviewer", "main")  # the session beats [models]
    assert r.for_agent("reviewer", "fast")[0].source == "session"


@pytest.mark.parametrize("requested", ["nope", "openai:gpt-x"])
def test_files_can_only_name_aliases(requested):
    choice, warning = router().for_agent("x", requested)
    assert choice.spec == "p:main" and requested in warning


def test_config_may_use_a_raw_spec():
    assert router(models={"x": "p:raw"}).for_agent("x", "fast")[0].spec == "p:raw"


async def test_agent_runs_on_its_own_model(tmp_path):
    write_agent(tmp_path, "reviewer", "---\nmodel: fast\n---\nReview.")
    s, fe = make(tmp_path, models={"t:fast": scripted("fast report")})
    await s.commands.dispatch(s, "@reviewer look")
    assert kinds(fe, "assistant_text")[-1]["text"] == "fast report"


async def test_agent_that_cant_use_its_model_does_not_start(tmp_path):
    write_agent(tmp_path, "reviewer", "---\nmodel: fast\n---\nReview.")

    async def inspector(spec, settings):
        return ModelFacts("no tool support")

    s, fe = make(tmp_path, models={"t:fast": scripted()}, inspector=inspector)
    await s.commands.dispatch(s, "@reviewer look")
    assert "no tool support" in kinds(fe, "error")[-1]["message"] and s.failed


async def test_agent_uses_its_models_context_window(tmp_path):
    write_agent(tmp_path, "reviewer", "---\nmodel: fast\n---\nReview.")
    seen = []

    async def inspector(spec, settings):
        seen.append(spec)
        return ModelFacts(context=32000, notes=["a note"])

    s, fe = make(tmp_path, "unused", models={"t:fast": scripted("one", "two")}, inspector=inspector)
    real = s.deps.spawn_agent

    async def spawn(*a, **k):
        import jimbo.wiring as w
        original, sessions = w.AgentSession, []

        def capture(*args, **kwargs):
            sessions.append(kwargs)
            return original(*args, **kwargs)
        w.AgentSession = capture
        try:
            return await real(*a, **k)
        finally:
            w.AgentSession = original
            assert sessions[0]["context_window"] == 32000

    s.deps.spawn_agent = spawn
    await s.commands.dispatch(s, "@reviewer one")
    await s.commands.dispatch(s, "@reviewer two")
    assert seen == ["t:fast"]  # checked once
    assert [c["text"] for c in kinds(fe, "command_output")].count("a note") == 1


# ---- R4: running an agent ----

def writes(name):
    return call("write_file", path=name, content="x")


async def test_undo_covers_parent_and_agent_files(tmp_path):
    agent = scripted(writes("agent.txt"), "wrote it")
    write_agent(tmp_path, "worker", "---\ndescription: writes\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, writes("parent.txt"), call("run_agent", agent="worker", task="write"), "done",
                 models={"t:fast": agent}, approval=["yolo"])
    await s.send("go")
    assert (tmp_path / "agent.txt").exists() and (tmp_path / "parent.txt").exists()
    await s.commands.dispatch(s, "/undo")
    assert not (tmp_path / "agent.txt").exists() and not (tmp_path / "parent.txt").exists()


async def test_undo_covers_a_direct_run_as_its_own_turn(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, writes("first.txt"), "done", models={"t:fast": scripted(writes("agent.txt"), "wrote it")},
                 approval=["yolo"])
    await s.send("go")
    await s.commands.dispatch(s, "@worker write")
    await s.commands.dispatch(s, "/undo")
    assert not (tmp_path / "agent.txt").exists() and (tmp_path / "first.txt").exists()


async def waiting_agent(tmp_path, *main, direct=False):
    started, cancelled = asyncio.Event(), []

    async def stream(messages, info):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        yield "never"

    write_agent(tmp_path, "slow", "---\ndescription: slow\nmodel: fast\n---\nWait.")
    s, fe = make(tmp_path, *main, models={"t:fast": FunctionModel(stream_function=stream)})
    run = asyncio.create_task(s.commands.dispatch(s, "@slow wait") if direct else s.send("go"))
    await asyncio.wait_for(started.wait(), 5)
    assert s.is_running and s.cancel()
    await asyncio.wait_for(run, 5)
    assert cancelled and not s.is_running
    return s, fe


async def test_ctrl_c_stops_a_delegated_agent_and_its_parent(tmp_path):
    s, fe = await waiting_agent(tmp_path, call("run_agent", agent="slow", task="wait"))
    assert kinds(fe, "run_finished")[-1] == {"ok": False, "cancelled": True}


async def test_ctrl_c_stops_a_direct_agent(tmp_path):
    s, fe = await waiting_agent(tmp_path, direct=True)
    assert kinds(fe, "run_finished")[-1] == {"ok": False, "cancelled": True}


async def test_agent_usage_counts_toward_the_turn(tmp_path):
    write_agent(tmp_path, "worker", "---\ndescription: w\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, call("run_agent", agent="worker", task="t"), "done",
                 models={"t:fast": scripted(call("read_file", path="nope"), "report")})
    await s.send("go")
    assert s.turn_usage.requests == 4 and s.total_usage.requests == 4  # 2 main + 2 agent


async def test_direct_run_usage_counts(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted("report")})
    await s.commands.dispatch(s, "@worker go")
    assert s.total_usage.requests == 1 and kinds(fe, "usage")[-1]["requests"] == 1


async def test_requests_are_kept_for_the_parent(tmp_path):
    write_agent(tmp_path, "loopy", "---\ndescription: l\nmodel: fast\n---\nLoop.")
    agent = scripted(*[call("glob", pattern="*")] * 20)
    s, fe = make(tmp_path, call("run_agent", agent="loopy", task="t"), "parent still works",
                 models={"t:fast": agent}, request_limit=8)
    assert await s.send("go") == "parent still works"
    assert s.turn_usage.requests <= 8


async def test_agent_does_not_start_without_requests_left(tmp_path):
    s, fe = make(tmp_path, call("run_agent", agent="explore", task="t"), "ok", request_limit=4)
    await s.send("go")
    assert "too few requests" in kinds(fe, "tool_result")[0]["content"]


async def test_agents_cant_start_agents(tmp_path):
    log = Log()
    write_agent(tmp_path, "worker", "---\ndescription: w\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted("report", log=log)})
    await s.commands.dispatch(s, "@worker go")
    names = {t.name for t in log[0].function_tools}
    assert "run_agent" not in names and "exit_plan" not in names and "read_file" in names
    assert "# Agents" not in log.instructions


async def test_failed_agent_returns_its_last_text_and_the_reason(tmp_path):
    async def stream(messages, info):
        yield "looking into it"
        raise RuntimeError("server died")

    write_agent(tmp_path, "worker", "---\ndescription: w\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, call("run_agent", agent="worker", task="t"), "it failed",
                 models={"t:fast": FunctionModel(stream_function=stream)})
    assert await s.send("go") == "it failed"
    report = next(r for r in kinds(fe, "tool_result") if r["tool"] == "run_agent")["content"]
    assert "server died" in report and "looking into it" in report
    assert kinds(fe, "agent_report") == [{"ok": False, "text": report, "agent": {"name": "worker", "model": "t:fast"}}]


async def test_agent_todos_and_skill_scopes_stay_separate(tmp_path):
    d = tmp_path / ".jimbo" / "skills" / "tuned"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: d\nmodels:\n  jimbo-compaction: smart\n---\nBody.\n")
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    agent = scripted(call("load_skill", name="tuned"),
                     call("todo_write", items=[{"content": "agent todo"}]), "report")
    s, fe = make(tmp_path, models={"t:fast": agent})
    await s.commands.dispatch(s, "@worker go")
    assert s.deps.todos == [] and s.deps.models.explain("jimbo-compaction").source == "main"


async def test_events_are_tagged_with_the_agent(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(call("glob", pattern="*"), "report")})
    await s.commands.dispatch(s, "@worker go")
    tool = kinds(fe, "tool_call")[0]
    assert tool["agent"] == {"name": "worker", "model": "t:fast"} and tool["id"].startswith("worker.")
    assert kinds(fe, "tool_result")[0]["id"] == tool["id"]


# ---- R5: the run_agent tool ----

async def test_main_model_runs_an_agent(tmp_path):
    log = Log()
    write_agent(tmp_path, "reviewer", "---\ndescription: reviews code\nmodel: fast\n---\nReview.")
    s, fe = make(tmp_path, call("run_agent", agent="reviewer", task="check"), "it's fine",
                 models={"t:fast": scripted("the report", log=log)})
    assert await s.send("go") == "it's fine"
    assert "the report" in kinds(fe, "tool_result")[0]["content"]
    assert 'You are the agent "reviewer"' in log.instructions
    assert "Review." in log.instructions


async def test_agent_index_and_hidden_agents(tmp_path):
    log = Log()
    write_agent(tmp_path, "private", "No description.")
    s, fe = make(tmp_path, call("run_agent", agent="private", task="t"), "ok")
    s.agent.model = scripted(call("run_agent", agent="private", task="t"), "ok", log=log)
    await s.send("go")
    assert "- explore:" in log.instructions and "private" not in log.instructions
    assert "unknown agent" in kinds(fe, "tool_result")[0]["content"]  # the model can't run it


async def test_tool_is_hidden_with_no_offered_agents(tmp_path):
    log = Log()
    write_agent(tmp_path, "explore", "No description.")
    write_agent(tmp_path, "general", "No description.")
    s, fe = make(tmp_path)
    s.agent.model = scripted("hi", log=log)
    await s.send("go")
    assert "run_agent" not in {t.name for t in log[0].function_tools}


async def test_report_is_cut_for_the_model_and_full_in_output(tmp_path):
    write_agent(tmp_path, "worker", "---\ndescription: w\nmodel: fast\n---\nWork.")
    report = "x" * 25_000
    s, fe = make(tmp_path, call("run_agent", agent="worker", task="t"), "done", models={"t:fast": scripted(report)})
    await s.send("go")
    assert len(kinds(fe, "tool_result")[0]["content"]) < 21_000
    assert kinds(fe, "agent_report")[0]["text"] == report  # the trace keeps it all
    await s.commands.dispatch(s, "/output")
    assert kinds(fe, "command_output")[-1]["text"] == report


# ---- R6: @name ----

async def test_at_name_joins_the_conversation(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted("the report")})
    assert await s.commands.dispatch(s, "@worker check this")
    assert kinds(fe, "user_message")[0]["text"] == "@worker check this"
    assert isinstance(s.history[-1], ModelResponse) and s.history[-1].parts[0] == TextPart("the report")
    assert kinds(fe, "run_finished")[-1] == {"ok": True}
    assert kinds(fe, "agent_report")[0]["text"] == "the report"


async def test_task_goes_to_the_agent_unchanged(tmp_path):
    seen = []

    async def stream(messages, info):
        seen.append(messages[-1].parts[-1].content)
        yield "ok"

    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": FunctionModel(stream_function=stream)})
    await s.commands.dispatch(s, "@worker check the thing")
    assert seen == ["check the thing"]


@pytest.mark.parametrize("text", ["@nobody hello", "look at @worker please", "@./worker what is this", "@worker.md x"])
async def test_other_at_text_goes_to_the_model(tmp_path, text):
    write_agent(tmp_path, "worker", "Work.")
    s, fe = make(tmp_path)
    assert not await s.commands.dispatch(s, text)


async def test_at_name_with_no_task_prints_usage(tmp_path):
    write_agent(tmp_path, "worker", "---\ndescription: does work\n---\nWork.")
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, "@worker")
    assert kinds(fe, "command_output")[-1]["text"] == "does work\nusage: @worker <task>"


async def test_at_name_is_refused_during_a_run(tmp_path):
    write_agent(tmp_path, "worker", "Work.")
    s, fe = make(tmp_path)
    await _steer_or_refuse(s, "@worker go")
    assert s.take_steers() == [] and "can't run" in kinds(fe, "command_output")[-1]["text"]
    await _steer_or_refuse(s, "@nobody go")
    assert s.take_steers() == ["@nobody go"]


async def test_at_file_with_an_agents_name(tmp_path):
    (tmp_path / "worker").write_text("file contents")
    write_agent(tmp_path, "worker", "Work.")
    seen = []

    async def stream(messages, info):
        seen.append(messages[-1].parts[-1].content)
        yield "ok"

    s, fe = make(tmp_path)
    s.agent.model = FunctionModel(stream_function=stream)
    await s.send("@./worker what is this")
    assert "file contents" in seen[0]


# ---- R7: commands ----

def write_command(root, name, text):
    d = root / ".jimbo" / "commands"
    d.mkdir(parents=True)
    (d / f"{name}.md").write_text(text)


async def test_command_runs_in_its_agent_on_its_model(tmp_path):
    log, seen = Log(), []

    async def stream(messages, info):
        seen.append(messages[-1].parts[-1].content)
        log.append(info)
        yield "the changelog"

    write_agent(tmp_path, "writer", "---\nmodel: smart\ntools: [read_file]\n---\nWrite.")
    write_command(tmp_path, "changelog", "---\nagent: writer\nmodel: fast\n---\nChangelog for $ARGUMENTS.")
    s, fe = make(tmp_path, models={"t:fast": FunctionModel(stream_function=stream)})
    await s.commands.dispatch(s, "/changelog 2.0")
    assert seen == ["Changelog for 2.0."] and {t.name for t in log[0].function_tools} == {"read_file"}
    assert kinds(fe, "assistant_text")[-1]["text"] == "the changelog"


async def test_config_beats_a_commands_model(tmp_path):
    write_agent(tmp_path, "writer", "Write.")
    write_command(tmp_path, "changelog", "---\nagent: writer\nmodel: fast\n---\nGo.")
    s, fe = make(tmp_path, models={"t:smart": scripted("smart")})
    await s.commands.dispatch(s, "/models set writer smart")
    await s.commands.dispatch(s, "/changelog")
    assert kinds(fe, "assistant_text")[-1]["text"] == "smart"


async def test_command_with_an_unknown_agent_is_an_error(tmp_path):
    write_command(tmp_path, "changelog", "---\nagent: nobody\n---\nGo.")
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, "/changelog")
    assert "unknown agent" in kinds(fe, "command_output")[-1]["text"]


async def test_command_without_agent_uses_the_main_model(tmp_path):
    write_command(tmp_path, "hello", "Say hi.")
    s, fe = make(tmp_path, "hi from main")
    await s.commands.dispatch(s, "/hello")
    assert kinds(fe, "assistant_text")[-1]["text"] == "hi from main"


# ---- R8: skills in agents ----

def write_skill(root, name, body="Skill body."):
    d = root / ".jimbo" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: about {name}\n---\n{body}\n")
    return d


async def test_skills_are_preloaded_with_their_directory(tmp_path):
    log = Log()
    d = write_skill(tmp_path, "style", "Use tabs.")
    write_agent(tmp_path, "worker", "---\nmodel: fast\ntools: [read_file]\nskills: [style]\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted("report", log=log)})
    await s.commands.dispatch(s, "@worker go")
    assert "Use tabs." in log.instructions and str(d) in log.instructions
    assert "load_skill(name)" not in log.instructions  # it wasn't given the tool


async def test_agent_and_skill_can_share_a_name(tmp_path):
    write_skill(tmp_path, "whatever", "Skill body.")
    write_agent(tmp_path, "whatever", "---\nmodel: fast\n---\nUse the /whatever skill.")
    agent = scripted(call("load_skill", name="whatever"), "report")
    s, fe = make(tmp_path, models={"t:fast": agent})
    await s.commands.dispatch(s, "@whatever go")
    loaded = kinds(fe, "tool_result")[0]
    assert loaded["ok"] and "Skill body." in loaded["content"]


async def test_skill_command_still_runs_in_the_main_session(tmp_path):
    write_skill(tmp_path, "whatever")
    write_agent(tmp_path, "whatever", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, "from main", models={"t:fast": scripted("from agent")})
    await s.commands.dispatch(s, "/whatever")
    assert kinds(fe, "assistant_text")[-1]["text"] == "from main"


# ---- R9: approvals ----

async def test_prompts_name_the_agent(tmp_path):
    asked = []

    class Fe(ScriptedFrontend):
        async def request_approval(self, req):
            asked.append(req.summary)
            return ApprovalDecision(allow=True)

    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(call("shell", command="pytest -x"), "ok")}, frontend=Fe(),
                 tasks={"jimbo-shell-assess": {"mode": "off"}})
    await s.commands.dispatch(s, "@worker test")
    assert asked and asked[0].startswith("[worker] ") and "pytest -x" in asked[0]


async def test_plan_mode_denies_agent_writes(tmp_path):
    log = Log()
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(writes("x.txt"), "report", log=log)}, approval=["yolo"])
    s.deps.mode = "plan"
    await s.commands.dispatch(s, "@worker write")
    assert not (tmp_path / "x.txt").exists()
    assert "only read-only tools work" in log.instructions and "exit_plan" not in log.instructions


async def test_approval_rules_apply_to_agents(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(writes("x.txt"), "report")},
                 approval=["rules", "yolo"], approval_deny=["write_file"])
    await s.commands.dispatch(s, "@worker write")
    assert not (tmp_path / "x.txt").exists()


async def test_deny_rule_stops_the_model_starting_an_agent(tmp_path):
    write_agent(tmp_path, "worker", "---\ndescription: w\n---\nWork.")
    s, fe = make(tmp_path, call("run_agent", agent="worker", task="t"), "ok", approval_deny=["run_agent:worker"])
    await s.send("go")
    assert "denied by rule" in kinds(fe, "tool_result")[0]["content"]


# ---- R10: /agents ----

async def test_agents_lists_agents(tmp_path):
    write_agent(tmp_path, "worker", "---\ndescription: does work\n---\nWork.")
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, "/agents")
    text = kinds(fe, "command_output")[-1]["text"]
    assert "explore (built in):" in text and "worker: does work" in text


async def test_agents_new_creates_a_working_agent(tmp_path):
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, "/agents new helper")
    assert (tmp_path / ".jimbo" / "agents" / "helper.md").exists()
    await s.commands.dispatch(s, "/agents new mine --user")
    assert (tmp_path / "user" / "agents" / "mine.md").exists()
    await s.refresh_agents()
    agent = s.deps.agents["helper"]
    assert agent.description == "" and agent.tools is None  # only you can run it, until you describe it


@pytest.mark.parametrize("name", ["main", "jimbo-x", "bad/name"])
async def test_agents_new_refuses_bad_names(tmp_path, name):
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, f"/agents new {name}")
    assert "can't be an agent name" in kinds(fe, "command_output")[-1]["text"]


async def test_agents_new_refuses_to_overwrite(tmp_path):
    write_agent(tmp_path, "worker", "keep me")
    s, fe = make(tmp_path)
    await s.commands.dispatch(s, "/agents new worker")
    assert "already exists" in kinds(fe, "command_output")[-1]["text"]
    assert (tmp_path / ".jimbo" / "agents" / "worker.md").read_text() == "keep me"


async def test_agent_files_apply_from_the_next_input(tmp_path):
    s, fe = make(tmp_path, "from main", models={"t:fast": scripted("from agent")})
    await _handle(s, "@worker go")  # no such agent yet: goes to the main model
    assert kinds(fe, "assistant_text")[-1]["text"] == "from main"
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    await _handle(s, "@worker go")
    assert kinds(fe, "assistant_text")[-1]["text"] == "from agent"
    (tmp_path / ".jimbo" / "agents" / "worker.md").unlink()
    await s.refresh_agents()
    assert "worker" not in s.deps.agents


async def test_reload_reports_problems_once(tmp_path):
    s, fe = make(tmp_path)
    write_agent(tmp_path, "bad", "---\ntools: [nope]\n---\nx")
    await s.refresh_agents()
    await s.refresh_agents()
    assert sum("skipped agent" in c["text"] for c in kinds(fe, "command_output")) == 1


# ---- R11: frontends ----

def tagged(kind, **data):
    return Event(seq=1, session_id="s", kind=kind, data={**data, "agent": {"name": "worker", "model": "t:fast"}})


async def test_plain_shows_the_agent(capsys):
    fe = PlainFrontend()
    await fe.render(tagged("tool_call", id="1", tool="glob", args={"pattern": "*"}))
    await fe.render(tagged("error", type="X", message="boom"))
    err = capsys.readouterr().err
    assert "[tool] worker (t:fast) › glob" in err and "[error] worker (t:fast) › X" in err


async def test_tui_shows_the_agent():
    out = StringIO()
    fe = TuiFrontend(Console(file=out, force_terminal=False, width=100), None)
    await fe.render(tagged("tool_call", id="1", tool="glob", args={"pattern": "*"}))
    await fe.render(tagged("tool_result", id="1", tool="glob", ok=True, content="a.py"))
    text = out.getvalue()
    assert "▸ worker (t:fast) › glob" in text and "✓ worker (t:fast) › a.py" in text


# ---- approvals belong to their tool call ----

async def test_denied_calls_are_not_ok(tmp_path):
    s, fe = make(tmp_path, call("write_file", path="x.txt", content="x"), "ok", approval_deny=["write_file"])
    await s.send("go")
    result = kinds(fe, "tool_result")[0]
    assert result["ok"] is False and "denied by rule" in result["content"]


async def test_successful_calls_are_ok(tmp_path):
    s, fe = make(tmp_path, call("glob", pattern="*"), "ok")
    await s.send("go")
    assert kinds(fe, "tool_result")[0]["ok"] is True


async def test_approval_events_carry_the_tool_call_id(tmp_path):
    s, fe = make(tmp_path, call("shell", command="ls"), "ok", tasks={"jimbo-shell-assess": {"mode": "off"}})
    await s.send("go")
    call_id = kinds(fe, "tool_call")[0]["id"]
    assert kinds(fe, "approval_requested")[0]["call_id"] == call_id
    assert kinds(fe, "approval_resolved")[0]["call_id"] == call_id


async def test_agent_approvals_match_the_forwarded_id(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(call("glob", pattern="*"), "report")})
    await s.commands.dispatch(s, "@worker go")
    assert kinds(fe, "approval_resolved")[0]["call_id"] == kinds(fe, "tool_call")[0]["id"]


async def test_model_is_told_to_wrap_up_near_the_request_limit(tmp_path):
    seen, steps = [], iter([call("glob", pattern="*")] * 4 + ["summary"])

    async def stream(messages, info):
        seen.append(next(m.instructions for m in reversed(messages) if isinstance(m, ModelRequest)))
        yield next(steps)
    s, fe = make(tmp_path, models={"t:main": FunctionModel(stream_function=stream)}, request_limit=5)
    assert await s.send("go") == "summary"
    assert ["# Wrap up now" in i for i in seen] == [False, False, True, True, True]
    assert "1 model request(s) left" in seen[-1] and "no tool calls" in seen[-1]
    assert "no tool calls" not in seen[-2]
    assert kinds(fe, "wrap_up") == [{"left": 3}, {"left": 2}, {"left": 1}]  # the trace shows what the model was told


async def test_agent_wrap_up_reaches_the_parent(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(call("glob", pattern="*"), "report")}, request_limit=2)
    await s.commands.dispatch(s, "@worker go")
    assert kinds(fe, "wrap_up") == [{"left": 2, "agent": {"name": "worker", "model": "t:fast"}},
                                    {"left": 1, "agent": {"name": "worker", "model": "t:fast"}}]


async def test_agent_thinking_text_and_context_reach_the_parent_tagged(tmp_path):
    write_agent(tmp_path, "worker", "---\nmodel: fast\n---\nWork.")
    s, fe = make(tmp_path, models={"t:fast": scripted(call("glob", pattern="*"), "the report")})
    await s.commands.dispatch(s, "@worker go")
    tag = {"name": "worker", "model": "t:fast"}
    assert "".join(e["text"] for e in kinds(fe, "assistant_text_delta") if e.get("agent") == tag) == "the report"
    assert [e["turn"]["requests"] for e in kinds(fe, "context") if e.get("agent") == tag] == [1, 2, 2]
