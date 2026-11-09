"""`jimbo vscode build` and `jimbo vscode install`: build the VS Code extension from a clone, and install the one
that ships in the jimbo package."""
import shutil
import subprocess
from pathlib import Path

from .config import BUNDLED_DIR

VSIX = BUNDLED_DIR / "jimbo.vsix"  # built by `jimbo vscode build` or the release workflow; not in git
VSCODE_DIR = Path(__file__).resolve().parents[2] / "vscode"  # the vscode/ folder of a clone; absent in an install


def build(argv: list[str]) -> None:
    """Build the extension from the clone's vscode/ folder into VSIX. Installs its npm dependencies first when they
    are missing. Raises ValueError when the folder or npm is missing."""
    if argv:
        raise ValueError("usage: jimbo vscode build")
    if not (VSCODE_DIR / "package.json").is_file():
        raise ValueError(f"no vscode/ folder at {VSCODE_DIR}; `jimbo vscode build` works in a clone of jimbo")
    npm = shutil.which("npm")
    if npm is None:
        raise ValueError("'npm' is not on PATH; install Node.js 20 or later")
    if not (VSCODE_DIR / "node_modules").is_dir():
        r = subprocess.run([npm, "install"], cwd=VSCODE_DIR)
        if r.returncode:
            raise SystemExit(r.returncode)
    r = subprocess.run([npm, "run", "bundle"], cwd=VSCODE_DIR)
    if r.returncode:
        raise SystemExit(r.returncode)
    print(f"built {VSIX}")


def install(argv: list[str]) -> None:
    """Install the bundled extension with the editor CLI named in argv (default `code`). Raises ValueError when
    the CLI or the extension is missing."""
    match argv:
        case []:
            cli = "code"
        case [cli]:
            pass
        case _:
            raise ValueError("usage: jimbo vscode install [code|code-insiders|cursor|codium]")
    if not VSIX.is_file():
        raise ValueError(f"this jimbo has no VS Code extension ({VSIX} is missing); in a clone, run "
                         "`jimbo vscode build` first")
    exe = shutil.which(cli)
    if exe is None:
        raise ValueError(f"{cli!r} is not on PATH; in VS Code, run the command "
                         "\"Shell Command: Install 'code' command in PATH\"")
    r = subprocess.run([exe, "--install-extension", str(VSIX), "--force"])
    if r.returncode:
        raise SystemExit(r.returncode)
