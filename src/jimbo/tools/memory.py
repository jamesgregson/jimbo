from typing import Literal

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied
from ..core.skills import load_project_instructions, memory_paths
from .fs import _diff


async def remember(ctx: RunContext[Deps], note: str, scope: Literal["user", "project"], replaces: str = "") -> str:
    """Save a note that jimbo reads at the start of every session. Follow the jot-that-down skill.

    `scope` "user" saves to ~/.jimbo/memory.md, for every project: the user's preferences and habits. "project"
    saves to .jimbo/memory.md in this project: its conventions, decisions and facts. `replaces` is the text of an
    existing note to replace, when the new note corrects or updates it; leave it empty to add a note.
    """
    note = " ".join(note.split()).removeprefix("- ")
    if not note:
        raise ModelRetry("the note is empty")
    path = memory_paths(ctx.deps.user_dir, ctx.deps.workspace)[scope]
    old = path.read_text("utf-8") if path.is_file() else ""
    lines = old.splitlines()
    if replaces := " ".join(replaces.split()).removeprefix("- "):
        hits = [i for i, line in enumerate(lines) if line.strip().removeprefix("- ") == replaces]
        if len(hits) != 1:
            raise ModelRetry(f"`replaces` must match exactly one note in {path}; it matches {len(hits)}")
        lines[hits[0]] = f"- {note}"
    elif any(line.strip().removeprefix("- ") == note for line in lines):
        return f"Already in {path}."
    else:
        lines.append(f"- {note}")
    new = "\n".join(lines) + "\n"
    shown = "~/.jimbo/memory.md" if scope == "user" else ".jimbo/memory.md"
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "remember", {"path": str(path)}, f"remember in {shown}",
                                 detail=_diff(shown, old, new))
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    ctx.deps.checkpoints.record(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, "utf-8")
    ctx.deps.project_instructions = load_project_instructions(ctx.deps.user_dir, ctx.deps.workspace)  # applies now
    return f"Saved to {shown}."


async def _only_with_a_user_dir(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.user_dir is not None else None


MEMORY_TOOLS = [Tool(remember, prepare=_only_with_a_user_dir)]  # main session only
