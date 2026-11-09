import difflib
import hashlib
import os
import shutil
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import BinaryContent

from ..core.attachments import IMAGE_SUFFIXES, check_image
from ..core.deps import Deps, ToolDenied
from .lsp import errors_after_edit
from .paths import PathEscape

MAX_READ_BYTES = 64_000
MAX_LIST_ENTRIES = 500
MAX_TREE_FILES = 1000  # most files delete_file and move_file keep for /undo
MAX_TREE_BYTES = 50_000_000
MAX_PROPOSED_BYTES = 1_000_000  # largest file an approval carries whole, for an editor's diff view


async def read_file(ctx: RunContext[Deps], path: str, offset: int = 1, limit: int = 500) -> str | list:
    """Read a text file in the workspace (or in a skill's directory, by absolute path). Returns numbered lines starting at line `offset` (1-based).

    An image (.png, .jpg, .jpeg, .gif, .webp) is returned as the image itself, so you can look at it."""
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "read_file", {"path": path}, f"read {path}", read_only=True)
        p = ctx.deps.readable(path)
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    if not p.is_file():
        raise ModelRetry(f"{path!r} is not a file")
    if media_type := IMAGE_SUFFIXES.get(p.suffix.lower()):
        if not ctx.deps.vision:
            raise ModelRetry(f"{path!r} is an image, and this model can't read images (vision = false)")
        data = p.read_bytes()
        if problem := check_image(media_type, data):
            raise ModelRetry(f"{path!r}: {problem}")
        return [f"The image {path}:", BinaryContent(data, media_type=media_type)]
    try:
        text = p.read_bytes()[:MAX_READ_BYTES * 4].decode("utf-8")
    except UnicodeDecodeError:
        raise ModelRetry(f"{path!r} is not valid UTF-8 text") from None
    _saw(ctx, p)
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
        await ctx.deps.authorize(ctx.tool_call_id, "list_dir", {"path": path}, f"list {path}", read_only=True)
        p = ctx.deps.readable(path)
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    if not p.is_dir():
        raise ModelRetry(f"{path!r} is not a directory")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    names = [e.name + ("/" if e.is_dir() else "") for e in entries[:MAX_LIST_ENTRIES]]
    if len(entries) > MAX_LIST_ENTRIES:
        names.append(f"[{len(entries) - MAX_LIST_ENTRIES} more]")
    return "\n".join(names) or "[empty]"


def _digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _saw(ctx: RunContext[Deps], p: Path) -> None:
    """Remember the file as the model now knows it."""
    if (h := _digest(p)) is not None:
        ctx.deps.seen_files[p] = h


def _check_fresh(ctx: RunContext[Deps], p: Path, path: str) -> None:
    """Refuse to change a file that changed (in an editor, or by a command) since the model last read or wrote it.
    A file the model never saw has nothing to compare with."""
    known = ctx.deps.seen_files.get(p)
    if known is not None and p.is_file() and _digest(p) != known:  # kept until a read_file updates it
        raise ModelRetry(f"{path} changed since you last read it (edited by the user or a command); read it again "
                         "before changing it")


def _proposed(p: Path, text: str) -> dict | None:
    """The file as it will be after a change, for frontends that show it in an editor; None if too large."""
    return {"path": str(p), "text": text} if len(text.encode("utf-8")) <= MAX_PROPOSED_BYTES else None


def _diff(path: str, old: str, new: str) -> str:
    d = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="")
    return "\n".join(list(d)[:200])


async def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
    """Create or overwrite a text file in the workspace or the scratch directory (parent directories are created)."""
    try:
        p = ctx.deps.writable(path)
        _check_fresh(ctx, p, path)
        old = p.read_text("utf-8") if p.is_file() else ""
        verb = "overwrite" if p.is_file() else "create"
        await ctx.deps.authorize(ctx.tool_call_id, "write_file", {"path": path}, f"{verb} {path}",
                                 detail=_diff(path, old, content), proposed=_proposed(p, content))
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    except UnicodeDecodeError:
        raise ModelRetry(f"{path!r} is not valid UTF-8 text") from None
    if p.is_dir():
        raise ModelRetry(f"{path!r} is a directory")
    existed = p.is_file()
    _check_fresh(ctx, p, path)  # again: the file may have changed while the user was being asked
    ctx.deps.checkpoints.record(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")
    _saw(ctx, p)
    return f"wrote {len(content)} chars to {path}" + await errors_after_edit(ctx, [(p, old if existed else None)])


class Edit(BaseModel):
    old: str
    new: str
    replace_all: bool = False


def _replace(text: str, e: Edit, label: str = "`old`") -> tuple[str, int]:
    n = text.count(e.old) if e.old else 0
    if n == 0:
        raise ModelRetry(f"{label} not found in file (it must match exactly, including whitespace)")
    if n > 1 and not e.replace_all:
        raise ModelRetry(f"{label} matches {n} times; add context to make it unique or set replace_all")
    return (text.replace(e.old, e.new) if e.replace_all else text.replace(e.old, e.new, 1)), (n if e.replace_all else 1)


async def _edit(ctx: RunContext[Deps], path: str, edits: list[Edit]) -> tuple[Path, str, int]:
    """Apply `edits` in order to the file's text, then ask once and write once. Returns the path, the text
    before, and the number of replacements."""
    try:
        p = ctx.deps.writable(path)
        if not p.is_file():
            raise ModelRetry(f"{path!r} is not a file")
        _check_fresh(ctx, p, path)
        text = updated = p.read_text("utf-8")
        total = 0
        for i, e in enumerate(edits, 1):
            updated, n = _replace(updated, e, "`old`" if len(edits) == 1 else f"edit {i}: `old`")
            total += n
        # multi_edit is approved as edit_file, so rules and "always" for edit_file cover it
        await ctx.deps.authorize(ctx.tool_call_id, "edit_file", {"path": path}, f"edit {path}",
                                 detail=_diff(path, text, updated), proposed=_proposed(p, updated))
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    except UnicodeDecodeError:
        raise ModelRetry(f"{path!r} is not valid UTF-8 text") from None
    _check_fresh(ctx, p, path)  # again: the file may have changed while the user was being asked
    ctx.deps.checkpoints.record(p)
    p.write_text(updated, "utf-8")
    _saw(ctx, p)
    return p, text, total


async def edit_file(ctx: RunContext[Deps], path: str, old: str, new: str, replace_all: bool = False) -> str:
    """Replace text in an existing file. `old` must match exactly once unless replace_all is true."""
    p, text, n = await _edit(ctx, path, [Edit(old=old, new=new, replace_all=replace_all)])
    return f"edited {path} ({n} replacement(s))" + await errors_after_edit(ctx, [(p, text)])


async def multi_edit(ctx: RunContext[Deps], path: str, edits: list[Edit]) -> str:
    """Make several replacements in one file in one step. Prefer it to several edit_file calls on one file.

    The edits apply in order, each to the result of the one before, with the same rules as edit_file. If any
    edit fails, none are made.
    """
    if not edits:
        raise ModelRetry("`edits` is empty")
    p, text, n = await _edit(ctx, path, edits)
    return f"edited {path} ({n} replacement(s))" + await errors_after_edit(ctx, [(p, text)])


def _confined(ctx: RunContext[Deps], path: str) -> Path:
    """`path` in the workspace or the scratch directory, not followed if it is a link: deleting or moving a link
    must not touch its target."""
    lexical = Path(os.path.normpath(ctx.deps.workspace / path))
    if lexical.is_symlink():
        raise ModelRetry(f"{path!r} is a symbolic link; use shell for links")
    p = ctx.deps.writable(path)
    if p == ctx.deps.workspace.resolve():
        raise PathEscape(f"{path!r} is the workspace itself")
    if p in (r.resolve() for r in ctx.deps.write_roots()):
        raise PathEscape(f"{path!r} is the scratch directory itself")
    return p


def _files_under(p: Path, path: str) -> list[Path]:
    """`p` itself, or every file in the directory `p`. Refuses links and trees too big to keep for /undo."""
    if p.is_file():
        return [p]
    files, size = [], 0
    for d, dirs, names in os.walk(p):
        for n in [*dirs, *names]:
            if Path(d, n).is_symlink():
                raise ModelRetry(f"{path!r} contains a symbolic link ({Path(d, n).relative_to(p)}); use shell")
        files += [Path(d, n) for n in names]
        size += sum(Path(d, n).stat().st_size for n in names)
        if len(files) > MAX_TREE_FILES or size > MAX_TREE_BYTES:
            raise ModelRetry(f"{path!r} has more than {MAX_TREE_FILES} files or {MAX_TREE_BYTES // 1_000_000} MB, "
                             "too much to keep for /undo; use shell")
    return files


async def delete_file(ctx: RunContext[Deps], path: str) -> str:
    """Delete a file, or a directory and everything in it, in the workspace or the scratch directory. /undo can
    restore it."""
    try:
        p = _confined(ctx, path)
        if not p.exists():
            raise ModelRetry(f"{path!r} does not exist")
        files = _files_under(p, path)
        what = f"directory {path} ({len(files)} files)" if p.is_dir() else path
        await ctx.deps.authorize(ctx.tool_call_id, "delete_file", {"path": path}, f"delete {what}")
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    for f in files:
        ctx.deps.checkpoints.record(f)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return f"deleted {what}"


async def move_file(ctx: RunContext[Deps], path: str, destination: str) -> str:
    """Move or rename a file or directory in the workspace or the scratch directory. `destination` must not exist;
    its parent directories are created. /undo can move it back. Imports and other references are not updated."""
    try:
        src = _confined(ctx, path)
        if not src.exists():
            raise ModelRetry(f"{path!r} does not exist")
        dst = ctx.deps.writable(destination)
        if dst.exists():
            raise ModelRetry(f"{destination!r} already exists")
        if dst.is_relative_to(src):
            raise ModelRetry(f"can't move {path!r} into itself")
        files = _files_under(src, path)
        await ctx.deps.authorize(ctx.tool_call_id, "move_file", {"path": path, "destination": destination},
                                 f"move {path} to {destination}")
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    for f in files:
        ctx.deps.checkpoints.record(f)
        ctx.deps.checkpoints.record(dst / f.relative_to(src) if src.is_dir() else dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)  # not rename: the scratch directory can be on another file system
    return f"moved {path} to {destination}"


FS_TOOLS = [read_file, list_dir, write_file, edit_file, multi_edit, delete_file, move_file]
