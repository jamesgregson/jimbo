from pydantic_ai import ModelRetry, RunContext

from ..core.deps import Deps, ToolDenied


async def compact(ctx: RunContext[Deps], focus: str = "") -> str:
    """Summarize the older conversation to free context, like the user's /compact command.

    Call it when the user asks you to compact, summarize or shrink the conversation or context. `focus` is what
    the summary should keep in detail, if the user said. The recent messages are kept as they are.
    """
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "compact", {"focus": focus}, "compact the conversation",
                                 read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.compact_requested = focus.strip()
    return "The older conversation will be summarized before your next step. Then tell the user it is done."


COMPACT_TOOLS = [compact]
