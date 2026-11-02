import asyncio
import os
import re
import subprocess
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from ..core.deps import Deps, ToolDenied
from .paths import PathEscape, resolve_in_workspace

MAX_GLOB_RESULTS = 500
MAX_GREP_MATCHES = 200
MAX_GREP_FILE_BYTES = 2_000_000
MAX_LINE_CHARS = 300
# Pruned when walking a directory that isn't a git repo (git already applies .gitignore).
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".tox"}


def glob_to_regex(pattern: str) -> re.Pattern:
    """Glob with `**` (any depth), `*` and `?` (within one path segment). No '/' means match basenames."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    prefix = "" if "/" in pattern else "(?:.*/)?"
    return re.compile(prefix + "".join(out))


def _git_files(root: Path, base: Path) -> list[str] | None:
    try:
        r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", str(base)],
                           cwd=root, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk_files(root: Path, base: Path) -> list[str]:
    files = []
    for d, dirs, names in os.walk(base):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        files += [Path(d, n).relative_to(root).as_posix() for n in names]
    return files


def list_files(root: Path, base: Path) -> list[str]:
    """Workspace-relative files under `base`, honouring .gitignore in git repos. Skips links leaving the workspace."""
    if base.is_file():
        return [base.relative_to(root).as_posix()]
    found = _git_files(root, base)
    if found is None:
        found = _walk_files(root, base)
    out = []
    for rel in found:
        try:
            p = resolve_in_workspace(root, rel)
        except PathEscape:
            continue
        if p.is_file():
            out.append(rel)
    return sorted(out)


async def _authorize_path(ctx: RunContext[Deps], tool: str, args: dict, summary: str, path: str) -> Path:
    try:
        await ctx.deps.authorize(ctx.tool_call_id, tool, args, summary, read_only=True)
        p = resolve_in_workspace(ctx.deps.workspace, path)
    except (PathEscape, ToolDenied) as e:
        raise ModelRetry(str(e)) from e
    if not p.exists():
        raise ModelRetry(f"{path!r} does not exist")
    return p


async def glob(ctx: RunContext[Deps], pattern: str, path: str = ".") -> str:
    """Find files by name, e.g. `**/*.py` or `test_*.py` (no '/' matches at any depth). Ignores git-ignored files."""
    base = await _authorize_path(ctx, "glob", {"pattern": pattern, "path": path}, f"glob {pattern} in {path}", path)
    rx = glob_to_regex(pattern)
    root = ctx.deps.workspace
    hits = [f for f in await asyncio.to_thread(list_files, root, base) if rx.fullmatch(f)]
    out = hits[:MAX_GLOB_RESULTS]
    if len(hits) > MAX_GLOB_RESULTS:
        out.append(f"[{len(hits) - MAX_GLOB_RESULTS} more; narrow the pattern or path]")
    return "\n".join(out) or "[no matches]"


def _grep(root: Path, files: list[str], rx: re.Pattern) -> tuple[list[str], int]:
    out, total = [], 0
    for rel in files:
        p = root / rel
        try:
            if p.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:  # binary
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(line):
                total += 1
                if len(out) < MAX_GREP_MATCHES:
                    out.append(f"{rel}:{n}:{line[:MAX_LINE_CHARS]}")
    return out, total


async def grep(ctx: RunContext[Deps], pattern: str, path: str = ".", glob: str | None = None,
               ignore_case: bool = False) -> str:
    """Search file contents with a Python regex. Returns `path:line:text`. `glob` filters files, e.g. `*.py`."""
    base = await _authorize_path(ctx, "grep", {"pattern": pattern, "path": path}, f"grep {pattern!r} in {path}", path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ModelRetry(f"invalid regex: {e}") from None
    root = ctx.deps.workspace
    files = [f for f in await asyncio.to_thread(list_files, root, base) if not ctx.deps.is_secret(f)]
    if glob:
        frx = glob_to_regex(glob)
        files = [f for f in files if frx.fullmatch(f)]
    out, total = await asyncio.to_thread(_grep, root, files, rx)
    if total > len(out):
        out.append(f"[{total - len(out)} more matches; narrow the pattern, path or glob]")
    return "\n".join(out) or "[no matches]"


SEARCH_TOOLS = [glob, grep]
