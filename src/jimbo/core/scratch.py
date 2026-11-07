"""Scratch directories: one per session, under <user_dir>/scratch, for files that don't belong in the workspace."""

import os
import shutil
import time
from pathlib import Path


def last_change(d: Path) -> float:
    """The newest modification time of `d` and everything in it."""
    newest = d.stat().st_mtime
    for top, dirs, files in os.walk(d):
        for name in [*dirs, *files]:
            try:
                newest = max(newest, os.lstat(os.path.join(top, name)).st_mtime)
            except OSError:
                pass
    return newest


def prune(root: Path, keep_days: float) -> None:
    """Delete the session folders under `root` that haven't changed for `keep_days`."""
    cutoff = time.time() - keep_days * 86400
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return
    for d in folders:
        try:
            if last_change(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def remove_if_empty(d: Path) -> None:
    try:
        d.rmdir()
    except OSError:
        pass
