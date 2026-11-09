"""Git and GitHub rules for shell commands: git is read-only unless `git_write` is on, and remote changes are always
denied. This reads the command text, so a script or `python -c` that runs git gets past it; the sandbox is what
stops those (it makes .git read-only)."""

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

from .approval import ToolCallInfo, Verdict
from .shellwords import segments, strip_prefix

Kind = Literal["read", "write", "remote", "unknown"]

# Subcommands that never change the repository or a remote.
READ = {"status", "diff", "log", "show", "blame", "annotate", "grep", "ls-files", "ls-tree", "cat-file", "rev-parse",
        "rev-list", "describe", "shortlog", "help", "version", "name-rev", "merge-base", "show-ref", "for-each-ref",
        "check-ignore", "check-attr", "check-ref-format", "var", "count-objects", "diff-tree", "diff-files",
        "diff-index", "whatchanged", "cherry", "range-diff", "show-branch", "verify-commit", "verify-tag", "archive",
        "format-patch", "request-pull", "fsck", "get-tar-commit-id", "column", "hash-object"}
# Change the local repository, or read a remote (`fetch` writes refs). Allowed only with git_write, and asked each time.
WRITE = {"add", "am", "apply", "bisect", "branch", "bundle", "checkout", "cherry-pick", "clean", "clone", "commit",
         "config", "fetch", "filter-branch", "filter-repo", "gc", "init", "ls-remote", "maintenance", "merge", "mv",
         "notes", "prune", "pull", "rebase", "reflog", "remote", "repack", "replace", "reset", "restore", "revert",
         "rm", "sparse-checkout", "stash", "submodule", "switch", "symbolic-ref", "tag", "update-index", "update-ref",
         "worktree", "lfs", "svn", "p4", "rerere", "pack-refs", "commit-tree", "read-tree", "write-tree", "mktree",
         "mktag", "unpack-objects", "fast-import", "checkout-index", "merge-file", "merge-tree"}
# Change a remote. Always denied.
REMOTE = {"push", "send-email", "imap-send"}
_REMOTE_EDITS = {"add", "set-url", "rename", "remove", "rm"}

# Global options before the subcommand that take a value as the next word.
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env",
                      "--super-prefix", "--list-cmds", "--attr-source"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}

_BRANCH_LIST = {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current",
                "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--color", "--no-color",
                "--column", "--no-column", "-i", "--ignore-case", "--omit-empty"}
_BRANCH_LIST_PREFIX = ("--sort", "--format", "--abbrev", "--color=", "--column=", "--contains=", "--merged=",
                       "--no-merged=", "--points-at=", "--no-contains=")
_TAG_LIST = {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
             "--color", "--column", "--no-column", "-i", "--ignore-case", "-v", "--verify", "--omit-empty"}
_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin",
                "--show-scope", "--name-only", "--null", "-z", "--global", "--system", "--local", "--worktree",
                "--includes", "--no-includes", "--type", "--bool", "--int", "--path", "--default"}

# gh: subcommands that only read. Everything else changes GitHub, the local repository or credentials.
_GH_READ_ACTIONS = {"view", "list", "status", "diff", "checks", "search", "browse", "download", "watch"}
_GH_READ_TOP = {"help", "version", "--version", "--help", "search", "browse", "status"}


@dataclass
class Found:
    kind: Kind
    text: str  # the git or gh command as written, for messages
    cwd: Path | None = None  # where it runs, when known


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _classify_git(args: list[str]) -> tuple[Kind, str]:
    """What `git <args>` does, and the subcommand, after global options."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("--version", "--help", "-h"):
            return "read", args[i]
        i += 2 if args[i] in _GLOBAL_WITH_VALUE else 1
    if i >= len(args):
        return "read", ""
    sub, rest = args[i], args[i + 1:]
    pos = _positional(rest)
    if sub in REMOTE:
        return "remote", sub
    if sub == "remote":
        if pos and pos[0] in _REMOTE_EDITS:
            return "remote", sub
        return ("read", sub) if not pos or pos[0] == "get-url" else ("write", sub)
    if sub == "lfs":
        if pos and pos[0] == "push":
            return "remote", sub
        return ("read", sub) if pos and pos[0] in ("ls-files", "status", "env", "version") else ("write", sub)
    if sub in ("svn", "p4"):
        return ("remote", sub) if pos and pos[0] in ("dcommit", "submit", "set-tree") else ("write", sub)
    if sub in READ:
        return "read", sub
    if sub == "branch":
        flags = [a for a in rest if a.startswith("-")]
        listing = all(f in _BRANCH_LIST or f.startswith(_BRANCH_LIST_PREFIX) for f in flags)
        has_list = any(f in ("-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at")
                       or f.startswith(("--contains=", "--merged=", "--no-merged=", "--points-at=")) for f in flags)
        return ("read" if listing and (not pos or has_list) else "write"), sub
    if sub == "tag":
        flags = [a for a in rest if a.startswith("-")]
        listing = all(f in _TAG_LIST or f.startswith(("--sort", "--format", "--color=", "-n")) for f in flags)
        return ("read" if listing and (not pos or flags) else "write"), sub
    if sub == "stash":
        return ("read" if pos and pos[0] in ("list", "show") else "write"), sub
    if sub == "reflog":
        return ("read" if not pos or pos[0] == "show" else "write"), sub
    if sub in ("notes", "worktree"):
        return ("read" if pos and pos[0] in ("list", "show") else "write"), sub
    if sub == "submodule":
        return ("read" if pos and pos[0] in ("status", "summary") else "write"), sub
    if sub == "bundle":
        return ("read" if pos and pos[0] in ("verify", "list-heads") else "write"), sub
    if sub == "symbolic-ref":
        return ("read" if len(pos) <= 1 and not {"-d", "--delete"} & set(rest) else "write"), sub
    if sub == "config":
        flags = [a for a in rest if a.startswith("-")]
        if pos and pos[0] in ("get", "list"):
            return "read", sub
        if all(f in _CONFIG_READ or f.startswith("--type=") or f.startswith("--default=") for f in flags) and (
                any(f in ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l") for f in flags)
                or len(pos) == 1):
            return "read", sub
        return "write", sub
    if sub in WRITE:
        return "write", sub
    return "unknown", sub


def _classify_gh(args: list[str]) -> Kind:
    pos = _positional(args)
    if not pos or pos[0] in _GH_READ_TOP or args[0] in _GH_READ_TOP:
        return "read"
    group = pos[0]
    if group == "api":
        method = None
        for j, a in enumerate(args):
            if a in ("-X", "--method") and j + 1 < len(args):
                method = args[j + 1].upper()
            elif a.startswith(("--method=", "-X")) and len(a) > 2 and a not in ("-X",):
                method = a.split("=", 1)[-1].removeprefix("-X").upper()
            elif a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=",
                                                                                         "--input=")):
                method = method or "POST"
        return "read" if (method or "GET") == "GET" else "remote"
    if group == "auth":
        return "read" if len(pos) > 1 and pos[1] == "status" else "remote"  # `gh auth token` prints a credential
    if group in ("pr", "repo") and len(pos) > 1 and pos[1] in ("checkout", "clone"):
        return "write"
    if len(pos) > 1 and pos[1] in _GH_READ_ACTIONS:
        return "read"
    return "remote"


def _resolve(cwd: Path, path: str) -> Path:
    return (cwd / os.path.expanduser(path)).resolve()


def find(command: str, workspace: Path, depth: int = 0) -> list[Found]:
    """The git and gh commands in a shell command, including inside `bash -c "..."` and `eval`."""
    out: list[Found] = []
    cwd = workspace
    for seg in segments(command):
        argv = strip_prefix(seg.argv)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        if prog == "cd" and len(argv) > 1 and not any(c in argv[1] for c in "$*?"):
            cwd = _resolve(cwd, argv[1])
        elif prog in _SHELLS and depth < 3:
            for j, a in enumerate(argv[1:], 1):
                if a.startswith("-") and "c" in a.lstrip("-") and j + 1 < len(argv):
                    out += find(argv[j + 1], cwd, depth + 1)
                    break
        elif prog == "eval" and depth < 3:
            out += find(" ".join(argv[1:]), cwd, depth + 1)
        elif prog == "git":
            kind, _ = _classify_git(argv[1:])
            where = cwd
            for j, a in enumerate(argv[1:-1], 1):
                if a == "-C":
                    where = _resolve(where, argv[j + 1])
                elif a == "-c" and "git " in argv[j + 1] and depth < 3:  # e.g. a pager or alias that runs git
                    out += find(argv[j + 1].split("=", 1)[-1], cwd, depth + 1)
            for a in argv[1:]:
                if a.startswith("--git-dir="):
                    where = _resolve(cwd, a.split("=", 1)[1])
            out.append(Found(kind, shlex.join(argv), where))
        elif prog == "gh":
            out.append(Found(_classify_gh(argv[1:]), shlex.join(argv), cwd))
    return out


REMOTE_MESSAGE = ("jimbo never changes a remote: git push, gh pr create and other commands that change a remote "
                  "repository or GitHub are always denied. Leave the changes local and tell the user what to run.")
UNKNOWN_MESSAGE = ("jimbo only runs git subcommands it knows; {sub!r} isn't one (aliases aren't allowed). Use the "
                   "full git command.")
READ_ONLY_MESSAGE = ("git is read-only in this session (git_write = false): only commands such as status, diff, log, "
                     "show and blame work. Leave the changes uncommitted and tell the user. Run the other commands "
                     "without the git part.")


MAX_SCAN_BYTES = 5_000_000


def _git(workspace: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def scan_commit(workspace: Path, secret_files: list[str], everything: bool) -> list[str]:
    """Secrets in what a commit would add: `path (secret file)` for names matching `secret_files`, and
    `path:line (kind)` for secret formats in added lines. `everything`: the command also stages changes (git add,
    commit -a), so look at all changes and untracked files, not only what is staged now."""
    from fnmatch import fnmatchcase
    from .redact import found
    if everything:
        diff = _git(workspace, "diff", "HEAD", "--no-color", "-U0") or _git(workspace, "diff", "--no-color", "-U0")
        names = [*_git(workspace, "diff", "HEAD", "--name-only").splitlines(),
                 *_git(workspace, "ls-files", "--others", "--exclude-standard").splitlines()]
        untracked = _git(workspace, "ls-files", "--others", "--exclude-standard").splitlines()
    else:
        diff = _git(workspace, "diff", "--cached", "--no-color", "-U0")
        names, untracked = _git(workspace, "diff", "--cached", "--name-only").splitlines(), []
    hits = [f"{n} (secret file)" for n in dict.fromkeys(names) if any(fnmatchcase(Path(n).name, g) for g in secret_files)]
    added: list[tuple[str, int, str]] = []
    path, line = "", 0
    for row in diff[:MAX_SCAN_BYTES].splitlines():
        if row.startswith("+++ b/"):
            path = row[6:]
        elif row.startswith("@@"):
            m = re.search(r"\+(\d+)", row)
            line = int(m.group(1)) if m else 0
        elif row.startswith("+") and not row.startswith("+++"):
            added.append((path, line, row[1:]))
            line += 1
    for name in untracked:
        try:
            text = (workspace / name).read_bytes()[:MAX_SCAN_BYTES].decode("utf-8", "replace")
        except OSError:
            continue
        added += [(name, i, t) for i, t in enumerate(text.splitlines(), 1)]
    for p, n, text in added:
        hits += [f"{p}:{n} ({kind})" for kind in found(text)]
    return list(dict.fromkeys(hits))


def _short(text: str, n: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


class GitRules:
    """Head of every approval chain, after plan mode, for `shell` calls. Remote changes and unknown git subcommands
    are denied. Other git changes are denied unless `git_write` is on; then the user is asked, with no "always", and
    no other policy (auto, yolo, remembered) can allow them. Changes to a repository under `scratch` are not
    limited, except remote changes."""

    name = "git"

    def __init__(self, git_write: bool, scratch: Callable[[], Path | None],
                 ask: Callable[[ToolCallInfo], Awaitable[Verdict]], secret_files: list[str] = ()) -> None:
        self.git_write = git_write
        self._scratch = scratch
        self._ask = ask
        self.secret_files = list(secret_files)

    def _in_scratch(self, f: Found, workspace: Path) -> bool:
        s = self._scratch()
        if f.cwd is None or s is None:
            return False
        ws, s = workspace.resolve(), s.resolve()
        return f.cwd.is_relative_to(s) and not f.cwd.is_relative_to(ws)

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        if call.tool != "shell":
            return Verdict(action="ask", decided_by=self.name)
        found = find(str(call.args.get("command", "")), call.workspace)
        if remote := [f for f in found if f.kind == "remote"]:
            return Verdict(action="deny", decided_by=self.name,
                           reason=f"{REMOTE_MESSAGE} (denied: {_short(remote[0].text)})")
        if unknown := [f for f in found if f.kind == "unknown"]:
            sub = _classify_git(shlex.split(unknown[0].text)[1:])[1] if unknown[0].text.startswith("git") else "?"
            return Verdict(action="deny", decided_by=self.name, reason=UNKNOWN_MESSAGE.format(sub=sub))
        writes = [f for f in found if f.kind == "write" and not self._in_scratch(f, call.workspace)]
        if not writes:
            return Verdict(action="ask", decided_by=self.name)
        if not self.git_write:
            return Verdict(action="deny", decided_by=self.name,
                           reason=f"{READ_ONLY_MESSAGE} (denied: {_short(writes[0].text)})")
        names = ", ".join(_short(f.text, 60) for f in writes)
        head = f"This command changes git: {names}"
        subs = [shlex.split(f.text) for f in writes if f.text.startswith("git")]
        commits = [a for a in subs if _classify_git(a[1:])[1] == "commit"]
        if commits:
            everything = any(_classify_git(a[1:])[1] == "add" for a in subs) or any(
                x in ("-a", "--all") or (x.startswith("-") and not x.startswith("--") and "a" in x) for c in commits
                for x in c[2:])
            if hits := scan_commit(call.workspace, self.secret_files, everything):
                head = f"Staged changes contain a secret: {', '.join(hits[:10])}\n{head}"
        call.detail = f"{head}\n\n{call.detail or ''}".rstrip()
        call.wording = {**call.wording, "always": None}
        return await self._ask(call)
