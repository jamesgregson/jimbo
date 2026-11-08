"""`jimbo vscode install`: install the VS Code extension that ships in the jimbo package."""
import shutil
import subprocess

from .config import BUNDLED_DIR

VSIX = BUNDLED_DIR / "jimbo.vsix"  # built by the release workflow; not in git


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
                         "`npm install && npm run bundle` in vscode/ first")
    exe = shutil.which(cli)
    if exe is None:
        raise ValueError(f"{cli!r} is not on PATH; in VS Code, run the command "
                         "\"Shell Command: Install 'code' command in PATH\"")
    r = subprocess.run([exe, "--install-extension", str(VSIX), "--force"])
    if r.returncode:
        raise SystemExit(r.returncode)
