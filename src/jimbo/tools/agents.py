from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied

MAX_REPORT_CHARS = 20_000
PARENT_RESERVE = 5  # requests kept back so the main model can finish the turn after an agent


async def run_agent(ctx: RunContext[Deps], agent: str, task: str) -> str:
    """Run `agent` (see the agent list in your instructions) on `task` and return its report.

    The agent starts with no conversation history, so put everything it needs in `task`.
    """
    spec = ctx.deps.agents.get(agent)
    if spec is None or not spec.description:
        names = [a.name for a in ctx.deps.agents.values() if a.description]
        raise ModelRetry(f"unknown agent {agent!r}; available: {', '.join(names) or 'none'}")
    try:  # the agent's own calls are each approved, so starting it is not a risk in itself
        await ctx.deps.authorize(ctx.tool_call_id, "run_agent", {"agent": agent, "task": task},
                                 f"run agent {agent}", read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    result = await ctx.deps.spawn_agent(spec, task, ctx.usage, reserve=PARENT_RESERVE)
    if len(result.text) > MAX_REPORT_CHARS:  # /output has all of it
        return f"{result.text[:MAX_REPORT_CHARS]}\n[truncated]"
    return result.text


async def _only_with_agents(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if any(a.description for a in ctx.deps.agents.values()) else None


AGENT_TOOLS = [Tool(run_agent, prepare=_only_with_agents, sequential=True)]  # one agent at a time
