import asyncio
import os
import shutil
import signal

from pydantic_ai import ModelRetry, RunContext

from ..core.deps import Deps, ToolDenied

MAX_OUTPUT_BYTES = 30_000
SHELL = shutil.which("bash") or "/bin/sh"


class _Capped:
    """Keeps the head and tail of a stream; the middle of long output is rarely what matters."""

    def __init__(self, limit: int) -> None:
        self.half = limit // 2
        self.head, self.tail = bytearray(), bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        if len(self.tail) > self.half:
            self.dropped += len(self.tail) - self.half
            del self.tail[:-self.half]

    def text(self) -> str:
        gap = f"\n[... {self.dropped} bytes omitted ...]\n" if self.dropped else ""
        return (bytes(self.head).decode("utf-8", "replace") + gap
                + bytes(self.tail).decode("utf-8", "replace"))


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: `cmd | cmd`, background jobs, ...
    except ProcessLookupError:
        pass


async def shell(ctx: RunContext[Deps], command: str, timeout: int | None = None) -> str:
    """Run a bash command in the workspace root; returns the exit code and combined stdout/stderr.

    Non-interactive (stdin is empty). `timeout` is in seconds. Prefer grep/glob/read_file for searching
    and reading files.
    """
    d = ctx.deps
    t = max(1, min(timeout or d.shell_timeout, d.shell_timeout_max))
    where = d.sandbox.describe() if d.sandbox else "NOT sandboxed"
    try:
        await d.authorize("shell", {"command": command}, f"run: {' '.join(command.split())[:120]}",
                          detail=f"$ {command}\n\n({where}; timeout {t}s)")
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    argv = [SHELL, "-c", command]
    if d.sandbox:
        argv = d.sandbox.wrap(argv)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=d.workspace, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)  # own group: Ctrl+C goes to jimbo only
    out = _Capped(MAX_OUTPUT_BYTES)

    async def pump() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(65536):
            out.add(chunk)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), t)
        status = f"exit code {proc.returncode}"
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        status = f"timed out after {t}s (killed)"
    except asyncio.CancelledError:  # run interrupted: don't leave the command running
        _kill(proc)
        raise
    return f"{status}\n{out.text()}".rstrip()
