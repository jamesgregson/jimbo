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
