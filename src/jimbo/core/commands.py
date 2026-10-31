"""Slash commands, handled before input reaches the model. Core owns the registry; handlers are wired outside."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import yaml

if TYPE_CHECKING:
    from .session import AgentSession

NAME_RE = re.compile(r"[A-Za-z][\w-]*")
# "/name args"; the name must not contain "/" so pasted paths like "/Users/x/y" still go to the model.
COMMAND_RE = re.compile(rf"/({NAME_RE.pattern})(?:\s+(.*))?", re.S)

Handler = Callable[["AgentSession", str], Awaitable[str | None]]


class CommandError(Exception):
    """Raised by a handler for a user-facing failure (bad arguments, ...)."""


class QuitSession(Exception):
    """Raised by a handler to end the interactive loop."""


@dataclass
class Command:
    name: str
    help: str
    handler: Handler


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def add(self, name: str, help: str, handler: Handler) -> None:
        self._commands[name] = Command(name, help, handler)

    def __contains__(self, name: str) -> bool:
        return name in self._commands

    def describe(self) -> list[dict]:
        return [{"name": c.name, "help": c.help} for c in sorted(self._commands.values(), key=lambda c: c.name)]

    async def dispatch(self, session: "AgentSession", text: str) -> bool:
        """Run `text` if it is a command. Returns False if it is ordinary input for the model."""
        m = COMMAND_RE.fullmatch(text.strip())
        if not m:
            return False
        name, args = m.group(1), (m.group(2) or "").strip()
        cmd = self._commands.get(name)
        if cmd is None:
            await session.emit("command_output", command=name, ok=False,
                               text=f"unknown command /{name}; try /help")
            return True
        try:
            out = await cmd.handler(session, args)
        except CommandError as e:
            await session.emit("command_output", command=name, ok=False, text=str(e))
        except QuitSession:
            raise
        except Exception as e:
            await session.emit("error", type=type(e).__name__, message=str(e))
        else:
            if out is not None:
                await session.emit("command_output", command=name, ok=True, text=out)
        return True


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter (between `---` lines) from a markdown body.

    Invalid YAML falls back to flat `key: value` lines so a stray colon doesn't lose the whole header."""
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text
    try:
        meta = yaml.safe_load(head)
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict):
        meta = {}
        for line in head.splitlines():
            key, colon, value = line.partition(":")
            if colon:
                meta[key.strip()] = value.strip()
    return {str(k): v for k, v in meta.items()}, body.lstrip("\n")


def load_markdown_commands(registry: CommandRegistry, dirs: list[Path]) -> None:
    """Each `<dir>/<name>.md` becomes /<name>: its body is sent as a prompt, `$ARGUMENTS` substituted.

    Later dirs override earlier ones; built-in commands are never overridden.
    """
    builtin = {c["name"] for c in registry.describe()}
    for d in dirs:
        for path in sorted(d.glob("*.md")) if d.is_dir() else []:
            name = path.stem
            if name in builtin or not NAME_RE.fullmatch(name):
                continue
            meta, body = parse_frontmatter(path.read_text("utf-8"))

            async def run(session: "AgentSession", args: str, body: str = body) -> None:
                if "$ARGUMENTS" in body:
                    prompt = body.replace("$ARGUMENTS", args)
                else:
                    prompt = f"{body.rstrip()}\n\n{args}" if args else body
                await session.send(prompt)

            registry.add(name, str(meta.get("description") or f"prompt from {path}"), run)
