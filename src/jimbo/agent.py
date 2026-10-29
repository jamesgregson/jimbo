import platform

from pydantic_ai import Agent
from pydantic_ai.models import Model

from .config import Settings
from .core.deps import Deps
from .tools.fs import ALL_TOOLS

SYSTEM_PROMPT = """\
You are jimbo, a coding agent working inside a single workspace directory on {os}.
Use the provided tools to inspect the code before answering; do not guess file contents.
Paths are relative to the workspace root. Be concise. If a tool call is denied, do not retry
the same call; explain what you wanted to do and why instead.
"""


def build_agent(settings: Settings, model: Model) -> Agent[Deps, str]:
    return Agent(
        model,
        deps_type=Deps,
        instructions=SYSTEM_PROMPT.format(os=platform.system()),
        tools=ALL_TOOLS,
        retries=settings.tool_retries,
    )
