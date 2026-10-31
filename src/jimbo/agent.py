import platform

from pydantic_ai import Agent
from pydantic_ai.models import Model

from .config import Settings
from .core.deps import Deps
from .tools.fs import FS_TOOLS
from .tools.search import SEARCH_TOOLS
from .tools.shell import shell

SYSTEM_PROMPT = """\
You are jimbo, a coding agent working inside a single workspace directory on {os}.
Use the provided tools to inspect the code before answering; do not guess file contents.
Find files with glob and search contents with grep; read them with read_file.
You can create and modify files with write_file (whole file) and edit_file (exact replace);
read a file before editing it. Paths are relative to the workspace root.
Use shell to run commands (tests, builds, git); it is non-interactive and runs in the workspace root.
Be concise.
If a tool call is denied, do not retry the same call; explain what you wanted to do and why instead.
"""


def build_agent(settings: Settings, model: Model) -> Agent[Deps, str]:
    return Agent(
        model,
        deps_type=Deps,
        instructions=SYSTEM_PROMPT.format(os=platform.system()),
        tools=[*FS_TOOLS, *SEARCH_TOOLS, shell],
        retries=settings.tool_retries,
    )
