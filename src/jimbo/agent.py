import platform

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from .config import Settings
from .core.deps import Deps
from .core.skills import format_skill_index
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


def project_context(ctx: RunContext[Deps]) -> str:
    """Dynamic instructions: AGENTS.md contents and the skill index, read from deps on every request."""
    parts = []
    if ctx.deps.mode == "plan":
        parts.append(PLAN_PROMPT)
    if ctx.deps.project_instructions:
        parts.append(ctx.deps.project_instructions)
    if ctx.deps.skills:
        parts.append("# Skills\nCall load_skill(name) to get a skill's full instructions when its "
                     "description matches the task:\n" + format_skill_index(ctx.deps.skills))
    return "\n\n".join(parts)


async def midrun_history(ctx: RunContext[Deps], messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor run before every model request: long tool loops compact between requests, not
    only between turns, and messages the user typed during the run (steering) join the next request."""
    if hook := ctx.deps.midrun_compact:
        messages = await hook(messages)
    if hook := ctx.deps.midrun_steer:  # after compaction, so a fresh steer is never summarized away
        messages = await hook(messages)
    return messages


def build_agent(settings: Settings, model: Model) -> Agent[Deps, str]:
    return Agent(
        model,
        deps_type=Deps,
        instructions=[SYSTEM_PROMPT.format(os=platform.system()), project_context],
        tools=[*FS_TOOLS, *SEARCH_TOOLS, shell, *TODO_TOOLS, *SKILL_TOOLS, *PLAN_TOOLS],
        retries=settings.tool_retries,
        capabilities=[ProcessHistory(midrun_history)],
    )
