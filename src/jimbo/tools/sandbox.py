"""Sandboxes for shell commands and language servers: macOS `sandbox-exec` (seatbelt), or a Docker or Podman
container.

A sandbox starts processes (`spawn`) and stops them with everything they started (`kill`). With no sandbox, the
module functions `spawn` and `kill` run processes directly."""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ..config import Settings

SandboxMode = Literal["auto", "on", "off", "docker", "podman"]
SHELL = shutil.which("bash") or "/bin/sh"

# Device files a normal command writes to; everything else outside `writable` is denied.
_DEVICES = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$") (subpath "/dev/fd")'


class SandboxError(Exception):
    """The sandbox can't run a process. The message is for the model or the user."""


class Sandbox(Protocol):
    kind: Literal["seatbelt", "docker", "podman"]
    container: bool  # commands run in a container, which has its own processes, files and programs
    network: bool
    shell: str  # the shell `shell` commands run with

    def describe(self) -> str: ...

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                    **kw) -> asyncio.subprocess.Process: ...

    async def kill(self, proc: asyncio.subprocess.Process) -> None: ...

    async def close(self) -> None: ...


def kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: `cmd | cmd`, background jobs, ...
    except (ProcessLookupError, PermissionError):
        pass


async def _exec(argv: list[str], cwd: Path | None, env: dict[str, str] | None, kw: dict) -> asyncio.subprocess.Process:
    # Own group: Ctrl+C goes to jimbo only, and kill_group takes the whole tree.
    return await asyncio.create_subprocess_exec(*argv, cwd=cwd, env={**os.environ, **env} if env else None,
                                                start_new_session=True, **kw)


async def spawn(sandbox: Sandbox | None, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                **kw) -> asyncio.subprocess.Process:
    if sandbox:
        return await sandbox.spawn(argv, cwd=cwd, env=env, **kw)
    return await _exec(argv, cwd, env, kw)


async def kill(sandbox: Sandbox | None, proc: asyncio.subprocess.Process) -> None:
    if sandbox:
        await sandbox.kill(proc)
    else:
        kill_group(proc)


def default_writable(workspace: Path) -> list[Path]:
    home = Path.home()
    return [workspace, Path("/private/tmp"), Path("/private/var/folders"),  # $TMPDIR lives under var/folders
            home / ".cache", home / "Library" / "Caches"]  # uv, pip, etc. keep caches here


class SeatbeltSandbox:
    kind: Literal["seatbelt"] = "seatbelt"
    container = False
    shell = SHELL

    def __init__(self, writable: list[Path], network: bool = True) -> None:
        # The kernel checks real paths (/tmp is /private/tmp), so resolve symlinks first.
        self.writable = list(dict.fromkeys(p.expanduser().resolve() for p in writable))
        self.network = network

    def profile(self) -> str:
        paths = " ".join(f'(subpath (param "W{i}"))' for i in range(len(self.writable)))
        rules = ["(version 1)", "(allow default)", "(deny file-write*)", f"(allow file-write* {_DEVICES} {paths})"]
        if not self.network:
            rules.append('(deny network-outbound (remote ip "*:*"))')
        return "\n".join(rules)

    def wrap(self, argv: list[str]) -> list[str]:
        params = [x for i, p in enumerate(self.writable) for x in ("-D", f"W{i}={p}")]
        return ["sandbox-exec", *params, "-p", self.profile(), *argv]

    def describe(self) -> str:
        net = "network allowed" if self.network else "no network"
        return f"sandboxed: writes limited to {', '.join(map(str, self.writable))}; {net}"

    async def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                    **kw) -> asyncio.subprocess.Process:
        return await _exec(self.wrap(argv), cwd, env, kw)

    async def kill(self, proc: asyncio.subprocess.Process) -> None:
        kill_group(proc)

    async def close(self) -> None:
        pass


@cache
def available() -> bool:
    """sandbox-exec exists and works; it fails when jimbo itself already runs inside a sandbox."""
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return False
    try:
        r = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def build_sandbox(settings: "Settings", workspace: Path) -> Sandbox | None:
    """The sandbox `settings.sandbox` asks for. Raises ValueError when it is required and can't be had."""
    mode = settings.sandbox
    if mode == "off":
        return None
    if mode in ("docker", "podman"):
        from . import container_sandbox
        return container_sandbox.build(settings, workspace, mode)
    if not available():
        if mode == "on":
            raise ValueError("sandbox='on' but sandbox-exec is unavailable (macOS only, and not nested in "
                             "another sandbox); use 'auto' or 'off'")
        return None
    return SeatbeltSandbox([*default_writable(workspace), *settings.sandbox_writable], settings.sandbox_network)
