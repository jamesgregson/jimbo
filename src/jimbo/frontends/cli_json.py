"""Headless frontend for scripts: every event as a line of JSON on stdout. Nobody is there to approve, so
calls that need a human are denied; allow them with approval_allow, or run with --yolo."""

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.events import Event


class JsonFrontend:
    async def next_user_input(self) -> None:
        return None

    async def render(self, event: Event) -> None:
        print(event.model_dump_json(), flush=True)

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(allow=False, note="not run: nobody is available to approve it")
