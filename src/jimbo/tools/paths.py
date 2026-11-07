from pathlib import Path


class PathEscape(ValueError):
    pass


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    """Resolve `path` (relative to workspace) following symlinks; reject anything outside it."""
    root = workspace.resolve()
    p = (root / path).resolve()
    if p != root and root not in p.parents:
        raise PathEscape(f"{path!r} is outside the workspace")
    return p


def resolve_in_roots(workspace: Path, path: str, extra_roots: list[Path]) -> Path:
    """Like resolve_in_workspace, but `path` may also fall under `extra_roots` (the scratch directory, and skill
    directories for reads)."""
    try:
        return resolve_in_workspace(workspace, path)
    except PathEscape:
        p = (workspace.resolve() / path).resolve()
        if any(r == p or r in p.parents for r in (x.resolve() for x in extra_roots)):
            return p
        raise
