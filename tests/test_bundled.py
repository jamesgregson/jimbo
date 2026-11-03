"""The agents, skills and commands shipped in src/jimbo/bundled, and how user files replace them."""

from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.agent import AGENT_TOOL_NAMES
from jimbo.config import BUNDLED_DIR, Settings
from jimbo.core.agents import BUILTIN_AGENTS, load_agents
from jimbo.core.commands import CommandRegistry, load_markdown_commands, parse_frontmatter
from jimbo.core.skills import load_skills
from jimbo.wiring import build_session

AGENTS = {p.stem for p in (BUNDLED_DIR / "agents").glob("*.md")}
COMMANDS = {p.stem for p in (BUNDLED_DIR / "commands").glob("*.md")}
SKILLS = {p.parent.name for p in (BUNDLED_DIR / "skills").glob("*/SKILL.md")}


def make(tmp_path, **kw):
    fe = ScriptedFrontend()
    return build_session(settings(tmp_path, bundled=True, **kw), fe, model=TestModel()), fe


def helps(session):
    return {c["name"]: c["help"] for c in session.commands.describe()}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


async def run(session, fe, command):
    await session.commands.dispatch(session, command)
    return [e.data for e in fe.events if e.kind == "command_output"][-1]["text"]


# ---- the files themselves ----

def test_the_set_is_not_empty():
    assert AGENTS and COMMANDS and SKILLS


def test_bundled_agents_load_cleanly():
    agents, problems = load_agents([BUNDLED_DIR / "agents"], AGENT_TOOL_NAMES)
    assert problems == []
    for name in AGENTS:
        assert agents[name].model is None  # a file can't know the user's aliases
        assert agents[name].description
        assert set(agents[name].skills) <= SKILLS


def test_bundled_skills_and_commands_have_descriptions_and_no_model():
    skills = load_skills([BUNDLED_DIR / "skills"])
    assert set(skills) == SKILLS and all(s.description and s.body for s in skills.values())
    assert all(not s.models for s in skills.values())
    for name in COMMANDS:
        meta, body = parse_frontmatter((BUNDLED_DIR / "commands" / f"{name}.md").read_text("utf-8"))
        assert meta["description"] and body and "model" not in meta
        assert meta.get("agent", "general") in AGENTS | set(BUILTIN_AGENTS)


def test_bundled_names_do_not_clash(tmp_path):
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=TestModel())  # bundled off: built-ins only
    builtin = set(helps(s))
    assert not COMMANDS & SKILLS and not (COMMANDS | SKILLS) & builtin
    assert not AGENTS & set(BUILTIN_AGENTS)


def test_every_bundled_file_is_available(tmp_path):
    s, _ = make(tmp_path)
    assert AGENTS <= set(s.deps.agents) and SKILLS <= set(s.deps.skills) and (COMMANDS | SKILLS) <= set(helps(s))


# ---- turning them off ----

def test_off_loads_nothing_but_keeps_built_in_agents(tmp_path):
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=TestModel())
    assert not AGENTS & set(s.deps.agents) and set(BUILTIN_AGENTS) <= set(s.deps.agents)
    assert not s.deps.skills and not COMMANDS & set(helps(s))


def test_a_project_config_can_turn_them_off(tmp_path):
    write(tmp_path / ".jimbo" / "config.toml", "bundled = false\n")
    assert Settings(workspace=tmp_path, user_dir=tmp_path / "user").bundled is False
    assert Settings(workspace=tmp_path / "other", user_dir=tmp_path / "user").bundled is True


# ---- user files win ----

def test_user_and_project_agents_replace_a_bundled_agent(tmp_path):
    write(tmp_path / "user" / "agents" / "reviewer.md", "personal")
    s, _ = make(tmp_path)
    assert s.deps.agents["reviewer"].body == "personal"
    write(tmp_path / ".jimbo" / "agents" / "reviewer.md", "project")
    s, _ = make(tmp_path)
    assert s.deps.agents["reviewer"].body == "project"


def test_a_skill_in_a_cross_tool_folder_replaces_a_bundled_skill(tmp_path):
    write(tmp_path / ".claude" / "skills" / "debug" / "SKILL.md", "---\ndescription: mine\n---\nmy way")
    s, _ = make(tmp_path)
    assert s.deps.skills["debug"].body == "my way" and helps(s)["debug"] == "mine"


def test_a_user_command_replaces_a_bundled_command(tmp_path):
    write(tmp_path / "user" / "commands" / "review.md", "---\ndescription: mine\n---\nmy way")
    s, _ = make(tmp_path)
    assert helps(s)["review"] == "mine"


def test_a_user_skill_replaces_a_bundled_command(tmp_path):
    write(tmp_path / ".jimbo" / "skills" / "review" / "SKILL.md", "---\ndescription: mine\n---\nmy way")
    s, _ = make(tmp_path)
    assert helps(s)["review"] == "mine"


def test_a_user_command_replaces_a_bundled_skills_command(tmp_path):
    write(tmp_path / "user" / "commands" / "debug.md", "---\ndescription: mine\n---\nmy way")
    s, _ = make(tmp_path)
    assert helps(s)["debug"] == "mine"


def test_a_later_load_never_overrides_what_is_registered(tmp_path):
    registry = CommandRegistry()
    registry.add("review", "first", None)
    load_markdown_commands(registry, [BUNDLED_DIR / "commands"])
    assert {c["name"]: c["help"] for c in registry.describe()}["review"] == "first"


# ---- use ----

async def test_review_runs_in_the_reviewer_agent(tmp_path):
    seen = []

    async def stream(messages, info):
        request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
        seen.append((request.instructions, request.parts[-1].content))
        yield "no bugs"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, bundled=True, model_aliases={"main": "t:main"}), fe,
                      model_builder=lambda spec, _: FunctionModel(stream_function=stream))
    await s.commands.dispatch(s, "/review the login change")
    instructions, prompt = seen[-1]
    assert 'You are the agent "reviewer"' in instructions and prompt == "Review this change: the login change\n"
    assert s.last_output == "no bugs"


async def test_bundled_lists_each_file_with_its_path(tmp_path):
    s, fe = make(tmp_path)
    text = await run(s, fe, "/bundled")
    assert len(text.splitlines()) == len(AGENTS) + len(COMMANDS) + len(SKILLS)
    assert str(BUNDLED_DIR / "agents" / "reviewer.md") in text
    assert str(BUNDLED_DIR / "commands" / "review.md") in text
    assert str(BUNDLED_DIR / "skills" / "debug" / "SKILL.md") in text


async def test_bundled_says_when_it_is_off(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=TestModel())
    assert "off" in await run(s, fe, "/bundled")
