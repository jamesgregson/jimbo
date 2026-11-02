from typing import Literal

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext

from ..core.deps import Deps, ToolDenied

MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


class TodoItem(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "done"] = "pending"


def format_todos(items: list[TodoItem]) -> str:
    return "\n".join(f"{MARKS[t.status]} {t.content}" for t in items) or "No todos."


async def todo_write(ctx: RunContext[Deps], items: list[TodoItem]) -> str:
    """Replace the task list with `items` (send the complete list every time, not just changes).

    Use it for work with 3 or more steps: write the plan first, keep at most one item `in_progress`,
    and mark each item `done` as soon as it is finished. Skip it for trivial one-step requests.
    """
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "todo_write", {"count": len(items)}, f"update todos ({len(items)} items)",
                                 read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    if sum(t.status == "in_progress" for t in items) > 1:
        raise ModelRetry("at most one todo may be in_progress")
    ctx.deps.todos = list(items)
    return format_todos(ctx.deps.todos)


TODO_TOOLS = [todo_write]
