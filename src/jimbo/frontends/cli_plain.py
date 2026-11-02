"""Line-based frontend using only print/input. Works in pipes and CI; the reference Frontend."""

import asyncio
import sys
import threading

from ..core.approval import ApprovalDecision, ApprovalRequest, parse_answer
from ..core.events import Event
from ..core.stats import format_stats, output_preview


class _Stdin:
    """input() on a daemon thread. Unlike asyncio.to_thread it can't hold up exit while waiting for a line,
    and a read that Ctrl+C abandoned is picked up by the next caller, so that caller gets the next line."""

    def __init__(self) -> None:
        self._read: asyncio.Future[str] | None = None

    async def line(self, prompt: str) -> str:
        print(prompt, end="", flush=True)
        loop = asyncio.get_running_loop()
        if self._read is None:
            self._read = read = loop.create_future()

            def finish(line: str | None, error: BaseException | None) -> None:
                if not read.done():
                    read.set_exception(error) if error else read.set_result(line)

            def work() -> None:
                line, error = None, None
                try:
                    line = input()
                except (EOFError, OSError) as e:
                    error = EOFError(str(e))
                try:
                    loop.call_soon_threadsafe(finish, line, error)
                except RuntimeError:  # the loop closed: jimbo is exiting
                    pass

            threading.Thread(target=work, daemon=True).start()
        try:
            return await asyncio.shield(self._read)
        finally:
            if self._read is not None and self._read.done():
                self._read = None


class PlainFrontend:
    def __init__(self) -> None:
        self._streamed = False
        self._stats = ""
        self._stdin = _Stdin()

    async def next_user_input(self) -> str | None:
        try:
            return await self._stdin.line(f"\n[{self._stats}]\n> " if self._stats else "\n> ")
        except EOFError:
            return None

    async def render(self, event: Event) -> None:
        d = event.data
        match event.kind:
            case "status":
                sandbox = "sandboxed" if d["sandboxed"] else "NOT sandboxed"
                flags = "".join(f" · {f}" for f, on in (("plan mode", d["mode"] == "plan"), ("YOLO", d["yolo"])) if on)
                print(f"[jimbo: {d['model']} · {d['workspace']} · {sandbox}{flags}]", file=sys.stderr)
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
            case "tool_result" if d.get("tool") == "shell":
                print(d["content"].strip().splitlines()[0], *output_preview(d["content"]), sep="\n  ", file=sys.stderr)
            case "error":
                print(f"\n[error] {d['type']}: {d['message']}", file=sys.stderr)
            case "approval_resolved":
                print(f"[approval] {d['tool']}: {d['action']} by {d['decided_by']}", file=sys.stderr)
                if d.get("detail"):
                    print(d["detail"], file=sys.stderr)
            case "context":
                self._stats = format_stats(d)
            case "run_finished" if not d.get("cancelled") and self._stats:
                print(f"[{self._stats}]", file=sys.stderr)
            case "todos":
                marks = {"done": "[x]", "in_progress": "[~]"}
                print("[todos]", *(f"{marks.get(t['status'], '[ ]')} {t['content']}" for t in d["items"]),
                      sep="\n  ", file=sys.stderr)
            case "compaction":
                print(f"[compacted history: {d['messages_before']} → {d['messages_after']} messages, ~{d['tokens_before']} → ~{d['tokens_after']} tokens]", file=sys.stderr)
            case "command_output":
                print(d["text"], file=sys.stdout if d["ok"] else sys.stderr)
            case "run_finished" if d.get("cancelled"):
                self._streamed = False
                print("\n[interrupted]", file=sys.stderr)

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        print(f"\nApprove {req.tool}: {req.summary}", file=sys.stderr)
        if a := req.assessment:
            print(f"[risk: {a['risk'].upper()}] {a['summary']}", file=sys.stderr)
            if a["reasons"]:
                print("  " + "; ".join(a["reasons"]), file=sys.stderr)
        if req.detail:
            print(req.detail, file=sys.stderr)
        try:
            answer = parse_answer(await self._stdin.line(f"[y]es / [n]o / [a]lways ({req.always})? "))
            if answer:
                return answer
            note = (await self._stdin.line("Why? (optional, enter to skip) ")).strip()
        except EOFError:
            return ApprovalDecision(allow=False, note="not run: stdin closed, so nobody can approve it")
        return ApprovalDecision(allow=False, note=note or None)
