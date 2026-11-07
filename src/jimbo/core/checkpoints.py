"""What each turn's file tools changed, so /diff can show it and /undo can put it back. Shell commands aren't tracked."""

import difflib
from pathlib import Path

MAX_DIFF_LINES = 400


class Checkpoints:
    def __init__(self) -> None:
        self._turns: list[dict[Path, bytes | None]] = []  # newest last; path -> content before the turn (None: new file)

    def begin(self) -> None:
        """Start a new turn; one that changed nothing is reused."""
        if not self._turns or self._turns[-1]:
            self._turns.append({})

    def record(self, path: Path) -> None:
        """Call before writing `path`. The first write of a turn wins, so undo restores the turn's starting state."""
        if not self._turns:
            self.begin()
        self._turns[-1].setdefault(path, path.read_bytes() if path.is_file() else None)

    def _latest(self) -> dict[Path, bytes | None] | None:
        return next((t for t in reversed(self._turns) if t), None)

    def diff(self, workspace: Path) -> str:
        """Unified diff of the latest turn that changed files, against what's on disk now."""
        lines: list[str] = []
        for path, before in (self._latest() or {}).items():
            old = (before or b"").decode("utf-8", "replace").splitlines()
            new = path.read_bytes().decode("utf-8", "replace").splitlines() if path.is_file() else []
            rel = path.relative_to(workspace).as_posix() if path.is_relative_to(workspace) else path.as_posix()  # ~/.jimbo/memory.md
            lines += difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", lineterm="")
        if len(lines) > MAX_DIFF_LINES:
            lines[MAX_DIFF_LINES:] = [f"[{len(lines) - MAX_DIFF_LINES} more lines]"]
        return "\n".join(lines)

    def changes(self) -> list[dict]:
        """The files the latest turn that changed files touched: {"path": absolute, "before": text or None}."""
        return [{"path": str(path), "before": None if before is None else before.decode("utf-8", "replace")}
                for path, before in (self._latest() or {}).items()]

    def undo(self) -> list[Path]:
        """Restore the latest turn that changed files and forget it. Returns the paths restored."""
        turn = self._latest()
        if turn is None:
            return []
        self._turns = self._turns[:self._turns.index(turn)]
        for path, before in turn.items():
            if before is None:
                path.unlink(missing_ok=True)
            else:  # the directory may be gone too: delete_file and move_file take whole directories
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before)
        return sorted(turn)
