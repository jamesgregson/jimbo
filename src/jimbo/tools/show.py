from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.attachments import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, check_image
from ..core.deps import Deps, ToolDenied
from .paths import PathEscape

SHOWABLE = {**IMAGE_SUFFIXES, ".svg": "image/svg+xml"}


async def show_image(ctx: RunContext[Deps], path: str, caption: str = "") -> str:
    """Show an image file to the user, e.g. a chart, diagram or screenshot you made with a shell command.

    Save the image in the workspace or the scratch directory first (.png, .jpg, .jpeg, .gif, .webp or .svg, up
    to 5 MB). Only the user sees it; to look at an image yourself, use read_file.
    """
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "show_image", {"path": path}, f"show {path}", read_only=True)
        p = ctx.deps.readable(path)
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    if not p.is_file():
        raise ModelRetry(f"{path!r} is not a file")
    if ctx.deps.is_secret(path):
        raise ModelRetry(f"{path!r} matches secret_files")
    media_type = SHOWABLE.get(p.suffix.lower())
    if media_type is None:
        raise ModelRetry(f"{path!r} is not a .png, .jpg, .jpeg, .gif, .webp or .svg file")
    data = p.read_bytes()
    if problem := (check_image(media_type, data) if media_type != "image/svg+xml"
                   else "larger than 5 MB" if len(data) > MAX_IMAGE_BYTES else None):
        raise ModelRetry(f"{path!r}: {problem}")
    assert ctx.deps.show_image is not None  # the tool is hidden otherwise
    await ctx.deps.show_image(p, path, media_type, caption.strip())
    return f"Showed {path} to the user."


async def _only_when_shown(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    """Only frontends that can display an image offer the tool (the web UI)."""
    return tool_def if ctx.deps.show_image is not None and ctx.deps.agent is None else None


SHOW_TOOLS = [Tool(show_image, prepare=_only_when_shown)]  # main session only
