import difflib

from pydantic_ai import ModelRetry, RunContext

from ..core.deps import Deps, ToolDenied
from .paths import PathEscape, resolve_in_workspace

MAX_READ_BYTES = 64_000
MAX_LIST_ENTRIES = 500


async def read_file(ctx: RunContext[Deps], path: str, offset: int = 1, limit: int = 500) -> str:
    """Read a text file in the workspace. Returns numbered lines starting at line `offset` (1-based)."""
    try:
        await ctx.deps.authorize("read_file", {"path": path}, f"read {path}", read_only=True)
        p = resolve_in_workspace(ctx.deps.workspace, path)
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    if not p.is_file():
        raise ModelRetry(f"{path!r} is not a file")
    try:
        text = p.read_bytes()[:MAX_READ_BYTES * 4].decode("utf-8")
    except UnicodeDecodeError:
        raise ModelRetry(f"{path!r} is not valid UTF-8 text") from None
    lines = text.splitlines()
    start = max(offset, 1) - 1
    chunk = lines[start:start + limit]
    out = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk))
    if len(out) > MAX_READ_BYTES:
        out = out[:MAX_READ_BYTES] + "\n[truncated]"
    elif start + limit < len(lines):
        out += f"\n[{len(lines) - start - limit} more lines; use offset={start + limit + 1}]"
    return out or "[empty]"


async def list_dir(ctx: RunContext[Deps], path: str = ".") -> str:
    """List entries of a workspace directory. Directories end with '/'."""
    try:
        await ctx.deps.authorize("list_dir", {"path": path}, f"list {path}", read_only=True)
        p = resolve_in_workspace(ctx.deps.workspace, path)
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    if not p.is_dir():
        raise ModelRetry(f"{path!r} is not a directory")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    names = [e.name + ("/" if e.is_dir() else "") for e in entries[:MAX_LIST_ENTRIES]]
    if len(entries) > MAX_LIST_ENTRIES:
        names.append(f"[{len(entries) - MAX_LIST_ENTRIES} more]")
    return "\n".join(names) or "[empty]"


def _diff(path: str, old: str, new: str) -> str:
    d = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="")
    return "\n".join(list(d)[:200])


async def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
    """Create or overwrite a text file in the workspace (parent directories are created)."""
    try:
        p = resolve_in_workspace(ctx.deps.workspace, path)
        old = p.read_text("utf-8") if p.is_file() else ""
        verb = "overwrite" if p.is_file() else "create"
        await ctx.deps.authorize("write_file", {"path": path}, f"{verb} {path}",
                                 detail=_diff(path, old, content))
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    except UnicodeDecodeError:
        raise ModelRetry(f"{path!r} is not valid UTF-8 text") from None
    if p.is_dir():
        raise ModelRetry(f"{path!r} is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")
    return f"wrote {len(content)} chars to {path}"


async def edit_file(ctx: RunContext[Deps], path: str, old: str, new: str, replace_all: bool = False) -> str:
    """Replace text in an existing file. `old` must match exactly once unless replace_all is true."""
    try:
        p = resolve_in_workspace(ctx.deps.workspace, path)
        if not p.is_file():
            raise ModelRetry(f"{path!r} is not a file")
        text = p.read_text("utf-8")
        n = text.count(old) if old else 0
        if n == 0:
            raise ModelRetry("`old` not found in file (it must match exactly, including whitespace)")
        if n > 1 and not replace_all:
            raise ModelRetry(f"`old` matches {n} times; add context to make it unique or set replace_all")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        await ctx.deps.authorize("edit_file", {"path": path}, f"edit {path}",
                                 detail=_diff(path, text, updated))
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    except UnicodeDecodeError:
        raise ModelRetry(f"{path!r} is not valid UTF-8 text") from None
    p.write_text(updated, "utf-8")
    return f"edited {path} ({n if replace_all else 1} replacement(s))"


ALL_TOOLS = [read_file, list_dir, write_file, edit_file]
