"""Session persistence: the model's message history and todos, saved beside the JSONL event log."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .events import Event

# Events worth redrawing when a session is resumed; deltas are covered by assistant_text, and approval
# prompts or the command list would be stale.
REPLAY_KINDS = {"user_message", "assistant_text", "tool_call", "tool_result", "error", "command_output",
                "compaction", "todos"}


@dataclass
class SavedSession:
    id: str
    history: list[ModelMessage]
    todos: list[dict]


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _state(self, sid: str) -> Path:
        return self.directory / f"{sid}.messages.json"

    def _log(self, sid: str) -> Path:
        return self.directory / f"{sid}.jsonl"

    def save(self, sid: str, history: list[ModelMessage], todos: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"history": json.loads(ModelMessagesTypeAdapter.dump_json(history)), "todos": todos}
        tmp = self._state(sid).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        os.replace(tmp, self._state(sid))  # a crash mid-write never leaves a truncated state file

    def ids(self) -> list[str]:
        """Resumable session ids, most recently saved first."""
        files = sorted(self.directory.glob("*.messages.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.name.removesuffix(".messages.json") for p in files]

    def resolve(self, prefix: str) -> str:
        """A full id, or a unique prefix of one. Raises ValueError otherwise."""
        ids = self.ids()
        if prefix in ids:
            return prefix
        matches = [i for i in ids if i.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"no session matches {prefix!r}" if not matches
                             else f"{prefix!r} is ambiguous: {', '.join(matches)}")
        return matches[0]

    def load(self, sid: str) -> SavedSession:
        try:
            data = json.loads(self._state(sid).read_text("utf-8"))
            return SavedSession(sid, ModelMessagesTypeAdapter.validate_python(data["history"]),
                                data.get("todos", []))
        except (OSError, ValueError, KeyError) as e:
            raise ValueError(f"cannot load session {sid}: {e}") from None

    def events(self, sid: str) -> list[Event]:
        """The logged events of a session; unreadable lines (e.g. a torn last write) are skipped."""
        events: list[Event] = []
        try:
            lines = self._log(sid).read_text("utf-8").splitlines()
        except OSError:
            return events
        for line in lines:
            try:
                events.append(Event.model_validate_json(line))
            except ValueError:
                continue
        return events

    def summary(self, sid: str) -> str:
        """First user message of the session, for listings."""
        for ev in self.events(sid):
            if ev.kind == "user_message":
                return " ".join(str(ev.data.get("text", "")).split())[:70]
        return ""
