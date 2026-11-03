import platform

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from .config import Settings
from .core.agents import AgentSpec, format_agent_index
from .core.deps import Deps
from .core.skills import format_skill_index
from .tools.agents import AGENT_TOOLS
from .tools.skills import SKILL_TOOLS
from .tools.fs import FS_TOOLS
from .tools.search import SEARCH_TOOLS
from .tools.plan import PLAN_TOOLS
from .tools.shell import shell
from .tools.todos import TODO_TOOLS

SYSTEM_PROMPT = """\
You are jimbo, a coding agent working inside a single workspace directory on {os}.
Use the provided tools to inspect the code before answering; do not guess file contents.
Find files with glob and search contents with grep; read them with read_file.
You can create and modify files with write_file (whole file) and edit_file (exact replace);
read a file before editing it. Paths are relative to the workspace root.
Use shell to run commands (tests, builds, git); it is non-interactive and runs in the workspace root.
For tasks with 3 or more steps, plan them with todo_write first and update it as you progress.
Be concise.
If a tool call is denied, do not retry the same call; explain what you wanted to do and why instead.
"""


PLAN_PROMPT = """\
# Plan mode
You are in plan mode: only read-only tools work (read_file, glob, grep, todo_write); edits and shell are denied.
Investigate the code, then call exit_plan(plan) with a concrete plan as a markdown list of steps
(one "- " or "1. " item per step). The user reviews it; once approved, plan mode ends and you can execute it.
If the plan is rejected, revise it using the feedback and call exit_plan again."""


AGENT_PROMPT = """\
# Agent
You are the agent "{name}", doing one task. Nobody can answer questions while you work.
Your final message is your report, and it is the only thing returned, so put everything the reader needs in it.

{body}"""


AGENT_PLAN_PROMPT = """\
# Plan mode
The session is in plan mode: only read-only tools work; edits and shell are denied. Report what you find."""


WRAP_UP_REQUESTS = 3  # warn the model when this many requests (or fewer) are left in the turn

WRAP_UP_PROMPT = """\
# Wrap up now
You have {left} model request(s) left in this turn, including this one; after that the turn is stopped.
Do not start new work. Leave the workspace in a sensible state: finish or undo any half-made edit,
and update the todo list. Then end with a message saying what is done, what is not, and what to do next.
{last}"""


def wrap_up_notice(deps: Deps, requests_made: int) -> str | None:
    if deps.request_limit is None:
        return None
    left = deps.request_limit - requests_made
    if left > WRAP_UP_REQUESTS:
        return None
    last = "This is the last request: reply with text only, no tool calls." if left <= 1 else ""
    return WRAP_UP_PROMPT.format(left=left, last=last).rstrip()


def project_context(ctx: RunContext[Deps]) -> str:
    """Dynamic instructions: AGENTS.md contents, the skill and agent indexes, and a wrap-up warning when the
    turn is nearly out of requests, read from deps on every request."""
    deps, parts = ctx.deps, []
    if deps.mode == "plan":
        parts.append(AGENT_PLAN_PROMPT if deps.agent else PLAN_PROMPT)
    if deps.project_instructions:
        parts.append(deps.project_instructions)
    for name in deps.agent.skills if deps.agent else ():
        if skill := deps.skills.get(name):
            parts.append(f"# Skill {name}\nSkill directory: {skill.path.parent}\n\n{skill.body}")
    if deps.skills and deps.can_load_skills:
        parts.append("# Skills\nCall load_skill(name) to get a skill's full instructions when its "
                     "description matches the task:\n" + format_skill_index(deps.skills))
    if index := format_agent_index(deps.agents):
        parts.append("# Agents\nCall run_agent(agent, task) to hand a self-contained task to an agent. It starts "
                     "with no conversation history and returns one report:\n" + index)
    if notice := wrap_up_notice(deps, ctx.usage.requests):  # last, so the parts above stay cacheable
        parts.append(notice)
    return "\n\n".join(parts)


async def midrun_history(ctx: RunContext[Deps], messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor run before every model request: long tool loops compact between requests, not
    only between turns, and messages the user typed during the run (steering) join the next request."""
    if hook := ctx.deps.midrun_compact:
        messages = await hook(messages)
    if hook := ctx.deps.midrun_steer:  # after compaction, so a fresh steer is never summarized away
        messages = await hook(messages)
    return messages


def _tool_name(tool) -> str:
    return tool.name if isinstance(tool, Tool) else tool.__name__


ALL_TOOLS = [*FS_TOOLS, *SEARCH_TOOLS, shell, *TODO_TOOLS, *SKILL_TOOLS]  # what agents may have
AGENT_TOOL_NAMES = {_tool_name(t) for t in ALL_TOOLS}


def build_agent(settings: Settings, model: Model, spec: AgentSpec | None = None) -> Agent[Deps, str]:
    """The main agent, or with `spec` an agent run: its own prompt, and only the tools it lists."""
    system = SYSTEM_PROMPT.format(os=platform.system())
    if spec is None:
        instructions, tools = [system], [*ALL_TOOLS, *AGENT_TOOLS, *PLAN_TOOLS]
    else:
        instructions = [system, AGENT_PROMPT.format(name=spec.name, body=spec.body)]
        tools = [t for t in ALL_TOOLS if spec.tools is None or _tool_name(t) in spec.tools]
    return Agent(
        model,
        deps_type=Deps,
        instructions=[*instructions, project_context],
        tools=tools,
        retries=settings.tool_retries,
        capabilities=[ProcessHistory(midrun_history)],
    )
