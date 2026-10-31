import platform

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from .config import Settings
from .core.deps import Deps
from .core.skills import format_skill_index
from .tools.skills import SKILL_TOOLS
from .tools.fs import FS_TOOLS
from .tools.search import SEARCH_TOOLS
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


def project_context(ctx: RunContext[Deps]) -> str:
    """Dynamic instructions: AGENTS.md contents and the skill index, read from deps on every request."""
    parts = []
    if ctx.deps.project_instructions:
        parts.append(ctx.deps.project_instructions)
    if ctx.deps.skills:
        parts.append("# Skills\nCall load_skill(name) to get a skill's full instructions when its "
                     "description matches the task:\n" + format_skill_index(ctx.deps.skills))
    return "\n\n".join(parts)


def build_agent(settings: Settings, model: Model) -> Agent[Deps, str]:
    return Agent(
        model,
        deps_type=Deps,
        instructions=[SYSTEM_PROMPT.format(os=platform.system()), project_context],
        tools=[*FS_TOOLS, *SEARCH_TOOLS, shell, *TODO_TOOLS, *SKILL_TOOLS],
        retries=settings.tool_retries,
    )
