import asyncio
import os
import shutil
import signal

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

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


class BackgroundProcess:
    """A shell(background=true) process: its output is read as it arrives and kept until shell_output reads it."""

    def __init__(self, command: str, proc: asyncio.subprocess.Process) -> None:
        self.command, self.proc = command, proc
        self.unread = bytearray()
        self.dropped = 0  # unread bytes thrown away to keep the newest MAX_OUTPUT_BYTES
        self.killed = False
        self.pump = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self.proc.stdout is not None
        while chunk := await self.proc.stdout.read(65536):
            self.unread += chunk
            if len(self.unread) > MAX_OUTPUT_BYTES:
                self.dropped += len(self.unread) - MAX_OUTPUT_BYTES
                del self.unread[:-MAX_OUTPUT_BYTES]

    def status(self) -> str:
        if self.proc.returncode is None:
            return "running"
        return "killed" if self.killed else f"exited with code {self.proc.returncode}"

    async def read(self, wait: float = 0) -> str:
        """Status and the output since the last read, after waiting up to `wait` seconds for the process to exit."""
        if wait > 0 and self.proc.returncode is None:
            try:
                await asyncio.wait_for(asyncio.shield(self.proc.wait()), wait)
            except TimeoutError:
                pass
        if self.proc.returncode is not None:  # the last output may still be in the pipe
            await asyncio.wait({self.pump}, timeout=1)
        gap = f"[... {self.dropped} earlier bytes omitted ...]\n" if self.dropped else ""
        text = gap + bytes(self.unread).decode("utf-8", "replace")
        self.unread.clear()
        self.dropped = 0
        return f"{self.status()}\n{text}".rstrip()

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.killed = True
            _kill(self.proc)
            await self.proc.wait()


async def _start(d: Deps, command: str) -> asyncio.subprocess.Process:
    argv = [SHELL, "-c", command]
    if d.sandbox:
        argv = d.sandbox.wrap(argv)
    return await asyncio.create_subprocess_exec(
        *argv, cwd=d.workspace, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)  # own group: Ctrl+C goes to jimbo only


async def shell(ctx: RunContext[Deps], command: str, timeout: int | None = None, background: bool = False) -> str:
    """Run a bash command in the workspace root; returns the exit code and combined stdout/stderr.

    Non-interactive (stdin is empty). `timeout` is in seconds. Prefer grep/glob/read_file for searching
    and reading files.

    The call returns only when every process holding its output has exited. For a server, a watcher or anything
    that runs longer than the timeout, set `background` to true: the call returns an id after a second, and the
    command keeps running with no timeout. Read its output with shell_output and stop it with shell_kill.
    Background processes are stopped when jimbo exits.
    """
    d = ctx.deps
    t = max(1, min(timeout or d.shell_timeout, d.shell_timeout_max))
    where = d.sandbox.describe() if d.sandbox else "NOT sandboxed"
    verb, limit = ("run in background", "no timeout") if background else ("run", f"timeout {t}s")
    try:
        await d.authorize(ctx.tool_call_id, "shell", {"command": command}, f"{verb}: {' '.join(command.split())[:120]}",
                          detail=f"$ {command}\n\n({where}; {limit})")
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    proc = await _start(d, command)
    if background:
        pid = str(len(d.processes) + 1)
        bg = d.processes[pid] = BackgroundProcess(command, proc)
        first = await bg.read(wait=1)  # a command that fails at once says so now
        return f"background process {pid}: {first}"
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


def _process(ctx: RunContext[Deps], id: str) -> BackgroundProcess:
    if (bg := ctx.deps.processes.get(str(id).strip())) is None:
        raise ModelRetry(f"no background process {id!r}; started: {', '.join(ctx.deps.processes) or 'none'}")
    return bg


async def shell_output(ctx: RunContext[Deps], id: str, wait: int = 0) -> str:
    """Return a background process's status and the output it wrote since you last read it.

    `wait` is how many seconds (up to the shell timeout) to wait for the process to exit first.
    """
    bg = _process(ctx, id)
    try:  # read-only: it only reads what a process already approved wrote
        await ctx.deps.authorize(ctx.tool_call_id, "shell_output", {"id": id}, f"read output of process {id}",
                                 read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    return await bg.read(wait=max(0, min(wait, ctx.deps.shell_timeout_max)))


async def shell_kill(ctx: RunContext[Deps], id: str) -> str:
    """Stop a background process (and the processes it started). Returns its last output."""
    bg = _process(ctx, id)
    try:  # read-only: it only stops a process this session started
        await ctx.deps.authorize(ctx.tool_call_id, "shell_kill", {"id": id}, f"stop process {id}: {bg.command[:80]}",
                                 read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    await bg.kill()
    return await bg.read()


async def kill_background(deps: Deps) -> None:
    """Stop every background process, as jimbo exits."""
    for bg in deps.processes.values():
        await bg.kill()


async def _only_with_processes(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.processes else None


SHELL_TOOLS = [shell, Tool(shell_output, prepare=_only_with_processes), Tool(shell_kill, prepare=_only_with_processes)]
