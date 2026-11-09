"""A rough reading of a shell command: its simple commands, their words and their redirect targets. Not a shell
parser: it is for checks that look for git calls or paths, and the sandbox is what actually stops a command."""

import os
import shlex
from dataclasses import dataclass, field

_SEPARATORS = set(";&|()\n`")
# Words that run the command after them.
WRAPPERS = {"command", "exec", "env", "nohup", "time", "nice", "sudo", "doas", "xargs", "builtin", "timeout",
            "caffeinate"}


@dataclass
class Segment:
    argv: list[str]  # the words, without redirects
    redirects: list[str] = field(default_factory=list)  # targets of >, >>, <, 2> ...


def _tokens(command: str) -> list[str] | None:
    """Words and separators. None when the quoting can't be parsed."""
    lex = shlex.shlex(command.replace("`", " ` "), posix=True, punctuation_chars=";&|()<>`\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _is_separator(tok: str) -> bool:
    return bool(tok) and all(c in _SEPARATORS for c in tok)


def segments(command: str) -> list[Segment]:
    toks = _tokens(command)
    if toks is None:  # unbalanced quotes: split on whitespace and separators, so commands are still seen
        rough = command.replace("`", " ` ")
        for c in ";&|()\n<>":
            rough = rough.replace(c, f" {c} ")
        toks = [t.strip("'\"") for t in rough.split()]
    segs: list[Segment] = []
    cur, redirect = Segment([]), None
    for t in toks:
        if _is_separator(t):
            if cur.argv or cur.redirects:
                segs.append(cur)
            cur, redirect = Segment([]), None
        elif t and set(t) <= set("<>&"):
            redirect = t  # the next word is its target
            if cur.argv and cur.argv[-1].isdigit():  # the 2 in 2>&1, split off by the tokenizer
                cur.argv.pop()
        elif redirect is not None:
            if not t.isdigit():  # not the 1 in 2>&1
                cur.redirects.append(t)
            redirect = None
        else:
            cur.argv.append(t)
    if cur.argv or cur.redirects:
        segs.append(cur)
    return segs


def strip_prefix(argv: list[str]) -> list[str]:
    """argv without leading VAR=value assignments and wrappers such as `env` or `sudo`."""
    i = 0
    while i < len(argv):
        w = argv[i]
        if "=" in w and not w.startswith("-") and w.split("=", 1)[0].replace("_", "a").isalnum():
            i += 1
        elif (name := os.path.basename(w)) in WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):  # wrapper options, e.g. env -i, nice -n
                i += 1
            if name in ("timeout", "nice") and i < len(argv) and argv[i][:1].isdigit():
                i += 1  # timeout's duration, nice's level
        else:
            break
    return argv[i:]
