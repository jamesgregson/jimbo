"""macOS `sandbox-exec` wrapper: shell commands may read anything but write only to allowed paths."""

import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Literal

SandboxMode = Literal["auto", "on", "off"]

# Device files a normal command writes to; everything else outside `writable` is denied.
_DEVICES = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$") (subpath "/dev/fd")'


def default_writable(workspace: Path) -> list[Path]:
    home = Path.home()
    return [workspace, Path("/private/tmp"), Path("/private/var/folders"),  # $TMPDIR lives under var/folders
            home / ".cache", home / "Library" / "Caches"]  # uv, pip, etc. keep caches here


class Sandbox:
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


def build_sandbox(mode: SandboxMode, workspace: Path, extra_writable: list[Path], network: bool) -> Sandbox | None:
    if mode == "off":
        return None
    if not available():
        if mode == "on":
            raise ValueError("sandbox='on' but sandbox-exec is unavailable (macOS only, and not nested in "
                             "another sandbox); use 'auto' or 'off'")
        return None
    return Sandbox([*default_writable(workspace), *extra_writable], network)
