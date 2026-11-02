import re

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied
from .todos import TodoItem

STEP = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+)")


def plan_steps(plan: str) -> list[TodoItem]:
    """Top-level list items of a markdown plan, as pending todos."""
    return [TodoItem(content=m.group(1).strip()) for line in plan.splitlines()
            if not line.startswith((" ", "\t")) and (m := STEP.fullmatch(line))]


async def exit_plan(ctx: RunContext[Deps], plan: str) -> str:
    """Present your finished plan (a markdown list of steps) for the user's approval and leave plan mode.

    Call this when you have investigated enough. If approved you can start executing; if rejected, revise.
    """
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "exit_plan", {"plan": plan}, "present plan", detail=plan)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.mode = "normal"
    steps = plan_steps(plan)
    if steps:
        ctx.deps.todos = steps
    return "Plan approved; plan mode is off. Execute it" + (" (todos created from the plan)." if steps else ".")


async def _only_in_plan_mode(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.mode == "plan" else None


PLAN_TOOLS = [Tool(exit_plan, prepare=_only_in_plan_mode)]
