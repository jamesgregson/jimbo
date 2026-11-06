from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied

MAX_REPORT_CHARS = 20_000
PARENT_RESERVE = 5  # requests kept back so the main model can finish the turn after an agent
# Tools that change nothing. An agent with only these runs alongside others; any other agent runs alone.
READ_ONLY_TOOLS = {"read_file", "list_dir", "glob", "grep", "load_skill", "todo_write", "web_fetch", "shell_output",
                   "lsp_definition", "lsp_references", "lsp_symbols", "lsp_diagnostics"}


def _siblings(ctx: RunContext[Deps]) -> int:
    """How many run_agent calls the model made in the response that made this one: they share the requests left."""
    last = ctx.messages[-1] if ctx.messages else None
    if not isinstance(last, ModelResponse):
        return 1
    return max(1, sum(isinstance(p, ToolCallPart) and p.tool_name == "run_agent" for p in last.parts))


async def run_agent(ctx: RunContext[Deps], agent: str, task: str) -> str:
    """Run `agent` (see the agent list in your instructions) on `task` and return its report.

    The agent starts with no conversation history, so put everything it needs in `task`. Read-only agents
    called in the same response run at the same time; other agents run one at a time.
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
    share = _siblings(ctx)
    if ctx.deps.mode == "plan" or (spec.tools is not None and set(spec.tools) <= READ_ONLY_TOOLS):
        result = await ctx.deps.spawn_agent(spec, task, ctx.usage, reserve=PARENT_RESERVE, share=share)
    else:
        async with ctx.deps.agent_lock:
            result = await ctx.deps.spawn_agent(spec, task, ctx.usage, reserve=PARENT_RESERVE, share=share)
    if len(result.text) > MAX_REPORT_CHARS:  # /output has all of it
        return f"{result.text[:MAX_REPORT_CHARS]}\n[truncated]"
    return result.text


async def _only_with_agents(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if any(a.description for a in ctx.deps.agents.values()) else None


AGENT_TOOLS = [Tool(run_agent, prepare=_only_with_agents)]
