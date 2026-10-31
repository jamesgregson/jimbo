"""Read an image from the system clipboard (macOS: pngpaste, else osascript)."""

import asyncio
import shutil
import tempfile
from pathlib import Path


async def _run(*cmd: str) -> bytes | None:
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        return None
    return out if p.returncode == 0 and out else None


async def clipboard_png() -> bytes | None:
    """PNG bytes of the clipboard image, or None if there is none (or no way to read it)."""
    if shutil.which("pngpaste"):
        return await _run("pngpaste", "-")
    if shutil.which("osascript"):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "clip.png")
            script = ('try\n set d to the clipboard as «class PNGf»\n'
                      f' set fh to open for access POSIX file "{f}" with write permission\n'
                      ' write d to fh\n close access fh\nend try')
            await _run("osascript", "-e", script)
            return f.read_bytes() if f.exists() and f.stat().st_size else None
    return None
