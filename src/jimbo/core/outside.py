"""Paths a shell command names outside the workspace, and notes for commands the sandbox stopped.

"Outside" means not under the workspace, the scratch directory or another allowed root, and not a system folder
that holds programs, libraries or headers."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .shellwords import segments, strip_prefix

# Reading these is normal work: programs, libraries, headers, devices.
SYSTEM = [Path(p) for p in ("/usr", "/bin", "/sbin", "/opt/homebrew", "/opt/local", "/Library/Developer",
                            "/Applications/Xcode.app", "/System", "/nix", "/etc/ssl", "/proc/self", "/dev")]
WRITERS = {"rm", "rmdir", "mv", "cp", "touch", "mkdir", "tee", "ln", "chmod", "chown", "install", "rsync",
           "truncate", "dd", "unlink", "shred"}
_HOME_VARS = ("$HOME", "${HOME}")


@dataclass(frozen=True)
class Reach:
    path: str  # as written
    write: bool

    def __str__(self) -> str:
        return f"{self.path} ({'write' if self.write else 'read'})"


def _expand(word: str) -> str | None:
    """`word` as an absolute path if it names one (/x, ~, ~/x, $HOME/x), else None."""
    for v in _HOME_VARS:
        if word == v or word.startswith(v + "/"):
            word = "~" + word[len(v):]
    if word == "~" or word.startswith("~/"):
        return os.path.expanduser(word)
    return word if word.startswith("/") else None


def _inside(p: Path, roots: list[Path]) -> bool:
    return any(p == r or p.is_relative_to(r) for r in roots)


def outside_paths(command: str, workspace: Path, roots: list[Path] = ()) -> list[Reach]:
    """Paths the command names that are outside the workspace and `roots`. A path is a write when a redirect
    targets it or the program changes files (rm, cp, touch ...)."""
    ws = workspace.resolve()
    allowed = [ws, *(r.resolve() for r in roots)]
    found: dict[str, Reach] = {}

    def check(word: str, write: bool, cwd: Path) -> None:
        if word.startswith("-") and "=" in word:  # --prefix=/opt/x
            word = word.split("=", 1)[1]
        text = _expand(word)
        if text is None and ".." in Path(word).parts:
            text = str(cwd / word)
        if text is None or any(c in text for c in "*?$"):
            return
        p = Path(os.path.normpath(text))
        try:
            p = p.resolve()
        except OSError:
            pass
        if _inside(p, allowed) or (not write and _inside(p, SYSTEM)) or str(p).startswith("/dev/"):
            return
        prev = found.get(word)
        found[word] = Reach(word, write or bool(prev and prev.write))

    cwd = ws
    for seg in segments(command):
        argv = strip_prefix(seg.argv)
        for target in seg.redirects:
            check(target, True, cwd)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        write = prog in WRITERS or (prog == "sed" and any(a.startswith("-i") for a in argv[1:]))
        for word in argv[1:]:
            check(word, write, cwd)
        if prog == "cd" and len(argv) > 1 and (t := _expand(argv[1]) or (str(cwd / argv[1]))):
            cwd = Path(os.path.normpath(t))
    return list(found.values())


_PHRASE = r"(?:Operation not permitted|Permission denied|Read-only file system)"
# "cat: /x: Operation not permitted", or Python's "... Operation not permitted: '/x'"
_BEFORE = re.compile(rf"([^\s:'\"]+)['\"]?: {_PHRASE}")
_AFTER = re.compile(rf"{_PHRASE}: ['\"]([^'\"]+)['\"]")


def blocked_notes(output: str, workspace: Path, scratch: Path | None, readable: list[Path] = (),
                  writes: set[Path] = frozenset()) -> list[str]:
    """Notes for the model about paths the sandbox stopped a command from using, read from its output. A path is
    taken as a write if it is in `writes` (what the command writes), or is readable but was blocked anyway."""
    ws = workspace.resolve()
    seen, notes = set(), []
    for line in output.splitlines():
        if not (m := _BEFORE.search(line) or _AFTER.search(line)):
            continue
        p = Path(os.path.normpath(ws / os.path.expanduser(m.group(1))))
        if p in seen:
            continue
        seen.add(p)
        rel = p.resolve().relative_to(ws) if p.resolve().is_relative_to(ws) else None
        if rel is not None and ".git" in rel.parts:
            notes.append(f"[jimbo: the sandbox blocks changing {p}: git is read-only. Leave the changes uncommitted "
                         "and tell the user.]")
        elif rel is not None:
            continue  # a permission problem in the project itself, not the sandbox
        elif p in writes or _inside(p.resolve(), [r.resolve() for r in readable]) or p.is_relative_to("/tmp") or \
                p.is_relative_to("/private/tmp"):
            where = f"the scratch directory {scratch}" if scratch else "the workspace"
            notes.append(f"[jimbo: the sandbox blocks writing {p}. Write temporary files to {where}.]")
        else:
            notes.append(f"[jimbo: the sandbox blocks reading {p}. Only the workspace, the scratch directory and tool "
                         "folders are readable. Ask the user if you need this file.]")
    return notes[:3]


STAY_INSIDE = ("Stay inside the workspace and the scratch directory. Don't look in the home folder or other projects "
               "unless the user asks. Ask the user if you need something outside.")


def dependency_dirs(workspace: Path, env: dict[str, str] | None = None) -> list[Path]:
    """Folders outside the workspace that hold the source of the project's libraries: the active virtualenv's
    site-packages, Cargo's registry sources and Go's module cache, when they exist."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    found: list[Path] = []
    if venv := env.get("VIRTUAL_ENV"):
        found += sorted(Path(venv).glob("lib/python*/site-packages")) + [Path(venv) / "Lib" / "site-packages"]
    found.append(Path(env.get("CARGO_HOME") or home / ".cargo") / "registry" / "src")
    found.append(Path(env.get("GOMODCACHE") or Path(env.get("GOPATH") or home / "go") / "pkg" / "mod"))
    ws = workspace.resolve()
    out = []
    for p in found:
        if p.is_dir() and not (r := p.resolve()).is_relative_to(ws) and r not in out:
            out.append(r)
    return out
