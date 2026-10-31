"""Line-based frontend using only print/input. Works in pipes and CI; the reference Frontend."""

import asyncio
import sys

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.events import Event
from ..core.stats import format_stats


class PlainFrontend:
    def __init__(self) -> None:
        self._streamed = False
        self._stats = ""

    async def next_user_input(self) -> str | None:
        try:
            return await asyncio.to_thread(input, f"\n[{self._stats}]\n> " if self._stats else "\n> ")
        except EOFError:
            return None

    async def render(self, event: Event) -> None:
        d = event.data
        match event.kind:
            case "user_message":
                print("[working…]", file=sys.stderr)
            case "assistant_text_delta":
                self._streamed = True
                print(d["text"], end="", flush=True)
            case "assistant_text":
                print() if self._streamed else print(d["text"])
                self._streamed = False
            case "tool_call":
                print(f"\n[tool] {d['tool']}({d['args']})", file=sys.stderr)
            case "tool_result" if not d["ok"]:
                print(f"[tool error] {d['content']}", file=sys.stderr)
            case "error":
                print(f"\n[error] {d['type']}: {d['message']}", file=sys.stderr)
            case "approval_resolved":
                print(f"[approval] {d['tool']}: {d['action']} by {d['decided_by']}", file=sys.stderr)
            case "context":
                self._stats = format_stats(d)
            case "run_finished" if not d.get("cancelled") and self._stats:
                print(f"[{self._stats}]", file=sys.stderr)
            case "todos":
                marks = {"done": "[x]", "in_progress": "[~]"}
                print("[todos]", *(f"{marks.get(t['status'], '[ ]')} {t['content']}" for t in d["items"]),
                      sep="\n  ", file=sys.stderr)
            case "compaction":
                print(f"[compacted history: {d['messages_before']} → {d['messages_after']} messages]", file=sys.stderr)
            case "command_output":
                print(d["text"], file=sys.stdout if d["ok"] else sys.stderr)
            case "run_finished" if d.get("cancelled"):
                self._streamed = False
                print("\n[interrupted]", file=sys.stderr)

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        print(f"\nApprove {req.tool}: {req.summary}", file=sys.stderr)
        if req.detail:
            print(req.detail, file=sys.stderr)
        ans = (await asyncio.to_thread(input, "[y]es / [n]o / [a]lways this session? ")).strip().lower()
        if ans.startswith("a"):
            return ApprovalDecision(allow=True, remember="session")
        if ans.startswith("y"):
            return ApprovalDecision(allow=True)
        note = (await asyncio.to_thread(input, "Why? (optional, enter to skip) ")).strip()
        return ApprovalDecision(allow=False, note=note or None)
