"""Agents: helpers defined by a markdown file, with their own prompt, tools and model. Each run starts with an
empty conversation, does one task and returns one report."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .commands import NAME_RE, parse_frontmatter
from .events import Event
from .models import MAIN

if TYPE_CHECKING:
    from .session import AgentSession

RESERVED_PREFIX = "jimbo-"
FORWARDED = {"tool_call", "tool_result", "compaction", "wrap_up", "instructions", "error"}
# Also passed on, so an agent's progress can be shown and logged. Frontends show these as a status line, not as
# the parent's own text or context.
PROGRESS = {"assistant_thinking_delta", "assistant_text_delta", "context"}


@dataclass
class AgentSpec:
    name: str
    description: str
    body: str
    path: Path | None = None  # None: built in
    model: str | None = None  # alias or "main"
    tools: list[str] | None = None  # None: every tool an agent may have
    skills: tuple[str, ...] = ()  # loaded when the agent starts


@dataclass
class AgentResult:
    ok: bool
    text: str  # the report, or what is known about a failure
    cancelled: bool = False


BUILTIN_AGENTS = {
    "explore": AgentSpec("explore", "Searches and reads code to answer a question. Read-only.",
                         "Search and read the code to answer the task. Report what you found, with file paths. "
                         "Don't change anything.",
                         tools=["read_file", "list_dir", "glob", "grep", "load_skill"]),
    "general": AgentSpec("general", "Does multi-step work in its own context, including edits and commands.",
                         "Complete the task, then report what you did and what you found."),
}


def _load_agent(path: Path, tool_names: set[str]) -> AgentSpec:
    """Raises ValueError with the reason a file can't be used."""
    name = path.stem
    if not NAME_RE.fullmatch(name) or name == MAIN or name.startswith(RESERVED_PREFIX):
        raise ValueError(f"name {name!r} is invalid or reserved")
    try:
        meta, body = parse_frontmatter(path.read_text("utf-8"), strict=True)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"can't read it: {e}") from None
    tools, skills = meta.get("tools"), meta.get("skills", [])
    if tools is not None and not isinstance(tools, list):
        raise ValueError("`tools` must be a list")
    if not isinstance(skills, list):
        raise ValueError("`skills` must be a list")
    if tools is not None and (unknown := sorted({str(t) for t in tools} - tool_names)):
        raise ValueError(f"tools not available to agents: {', '.join(unknown)}")
    model = meta.get("model")
    return AgentSpec(name, " ".join(str(meta.get("description") or "").split()), body, path,
                     str(model) if model else None, [str(t) for t in tools] if tools is not None else None,
                     tuple(str(s) for s in skills))


def load_agents(dirs: list[Path], tool_names: set[str]) -> tuple[dict[str, AgentSpec], list[str]]:
    """Built-in agents, then `<dir>/*.md`; later dirs override earlier ones. Returns the agents and a warning
    for each file that was skipped."""
    agents, problems = dict(BUILTIN_AGENTS), []
    for d in dirs:
        for path in sorted(d.glob("*.md")) if d.is_dir() else []:
            try:
                spec = _load_agent(path, tool_names)
            except ValueError as e:
                problems.append(f"skipped agent {path}: {e}")
            else:
                agents[spec.name] = spec
    return agents, problems


def format_agent_index(agents: dict[str, AgentSpec]) -> str:
    return "\n".join(f"- {a.name}: {a.description}" for a in agents.values() if a.description)


class ForwardSink:
    """Passes an agent's tool activity to its parent session, tagged with who did it, and keeps what the agent
    said last, in case it fails before reporting."""

    def __init__(self, parent: "AgentSession", name: str, model: str) -> None:
        self._parent = parent
        self._tag = {"name": name, "model": model}
        self._new_text = True
        self.text = ""
        self.reason: str | None = None
        self.cancelled = False

    async def emit(self, event: Event) -> None:
        d = event.data
        if event.kind == "assistant_text_delta":
            self.text = d["text"] if self._new_text else self.text + d["text"]
            self._new_text = False
        elif event.kind == "run_finished":
            self.cancelled = bool(d.get("cancelled"))
        elif event.kind in FORWARDED:
            self._new_text = True
            if event.kind == "error":
                self.reason = d.get("message")
            if event.kind == "tool_result" and d.get("tool") == "shell":
                self._parent.last_output = d["content"]
            if "id" in d:  # agents and their parent number tool calls separately
                d = {**d, "id": f"{self._tag['name']}.{d['id']}"}
            await self._parent.emit(event.kind, **d, agent=self._tag)
        if event.kind in PROGRESS:
            await self._parent.emit(event.kind, **d, agent=self._tag)
