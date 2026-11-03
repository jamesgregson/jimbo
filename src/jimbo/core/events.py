"""The single event vocabulary shared by frontends and loggers. Must stay JSON-serializable."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1

EventKind = Literal[
    "user_message",
    "assistant_text_delta",
    "assistant_text",
    "assistant_thinking_delta",
    "tool_call",
    "tool_result",
    "approval_requested",
    "approval_resolved",
    "usage",
    "context",
    "todos",
    "error",
    "run_finished",
    "commands",
    "command_output",
    "compaction",
    "status",
    "busy",
    "config",
]


class Event(BaseModel):
    seq: int
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str
    kind: EventKind
    data: dict[str, Any] = Field(default_factory=dict)


class EventSink:
    """Anything that consumes events: a frontend renderer, a JSONL logger, ..."""

    async def emit(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError
