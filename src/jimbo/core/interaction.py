from typing import Protocol

from .approval import ApprovalDecision, ApprovalRequest
from .attachments import UserMessage
from .events import Event


class Frontend(Protocol):
    """What a UI implements. Core never imports a UI; UIs only depend on this and on Event."""

    async def next_user_input(self) -> str | UserMessage | None:
        """Return the next user message (a UserMessage if it carries images), or None to quit."""

    async def render(self, event: Event) -> None:
        """Display an event. Must not block the run."""

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision: ...
