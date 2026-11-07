"""Line-based frontend using only print/input. Works in pipes and CI; the reference Frontend."""

import asyncio
import getpass
import sys
import threading
from typing import Awaitable, Callable

from ..core.agents import PROGRESS
from ..core.approval import ApprovalDecision, ApprovalRequest, mode_switch, parse_answer
from ..core.events import Event
from ..core.questions import Answers, Question, ask_by_line
from ..core.stats import OUTPUT_TOOLS, agent_prefix, format_stats, output_preview, sandbox_label


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
        self._thinking = False  # reasoning is mid-line on stderr
        self._stats = ""
        self._stdin = _Stdin()
        self._set_mode: Callable[[str], Awaitable[None]] | None = None

    def bind_approval_mode(self, set_mode: Callable[[str], Awaitable[None]]) -> None:
        """Called by run_frontend: `/approve <mode>` typed at an approval prompt switches modes."""
        self._set_mode = set_mode

    async def next_user_input(self) -> str | None:
        try:
            return await self._stdin.line(f"\n[{self._stats}]\n> " if self._stats else "\n> ")
        except EOFError:
            return None

    async def render(self, event: Event) -> None:
        d = event.data
        if d.get("agent") and event.kind in PROGRESS:  # an agent's thinking and text: only in the trace
            return
        match event.kind:
            case "status":
                sandbox = sandbox_label(d)
                flags = "".join(f" · {f}" for f, on in (("plan mode", d["mode"] == "plan"), ("YOLO", d["yolo"]), ("AUTO", d.get("auto"))) if on)
                print(f"[jimbo: {d['model']} · {d['workspace']} · {sandbox}{flags} · session {event.session_id}]",
                      file=sys.stderr)
            case "warning":
                print(f"[warning] {d['text']}", file=sys.stderr)
            case "user_message":
                print("[working…]", file=sys.stderr)
            case "busy" if d["on"]:
                print(f"[{d['label']}…]", file=sys.stderr)
            case "assistant_thinking_delta":
                self._thinking = True
                print(d["text"], end="", flush=True, file=sys.stderr)
            case "assistant_text_delta":
                if self._thinking:
                    print(file=sys.stderr)
                    self._thinking = False
                self._streamed = True
                print(d["text"], end="", flush=True)
            case "assistant_text":
                print() if self._streamed else print(d["text"])
                self._streamed = False
            case "tool_call":
                print(f"\n[tool] {agent_prefix(d)}{d['tool']}({d['args']})", file=sys.stderr)
            case "tool_result" if not d["ok"]:
                print(f"[tool error] {agent_prefix(d)}{d['content']}", file=sys.stderr)
            case "tool_result" if d.get("tool") in OUTPUT_TOOLS:
                print(agent_prefix(d) + d["content"].strip().splitlines()[0], *output_preview(d["content"]),
                      sep="\n  ", file=sys.stderr)
            case "error":
                print(f"\n[error] {agent_prefix(d)}{d['type']}: {d['message']}", file=sys.stderr)
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
                print(f"[{agent_prefix(d)}compacted history: {d['messages_before']} → {d['messages_after']} messages, ~{d['tokens_before']} → ~{d['tokens_after']} tokens]", file=sys.stderr)
            case "wrap_up":
                print(f"[{agent_prefix(d)}wrap-up requested: {d['left']} request(s) left in this turn]", file=sys.stderr)
            case "command_output":
                print(d["text"], file=sys.stdout if d["ok"] else sys.stderr)
            case "run_finished" if d.get("cancelled"):
                self._streamed = False
                print("\n[interrupted]", file=sys.stderr)

    async def confirm(self, message: str) -> bool:
        """A yes/no question; only y or yes counts, and a closed stdin is no."""
        print(f"\n{message}", file=sys.stderr)
        try:
            return (await self._stdin.line("[y]es / [n]o? ")).strip().lower() in ("y", "yes")
        except EOFError:
            return False

    async def ask_secret(self, message: str) -> str | None:
        """A line read without echo (from the terminal, not stdin, when there is one)."""
        try:
            return await asyncio.to_thread(getpass.getpass, message)
        except (EOFError, KeyboardInterrupt):
            return None

    async def ask_questions(self, questions: list[Question]) -> Answers:
        def show(i: int, q: Question) -> None:
            print(f"\nQuestion {i + 1} of {len(questions)}: {q.question}", file=sys.stderr)
            for n, o in enumerate(q.options, 1):
                print(f"  {n}. {o}", file=sys.stderr)

        return await ask_by_line(questions, show, self._stdin.line)

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        print(f"\n{req.title}: {req.summary}", file=sys.stderr)
        if a := req.assessment:
            print(f"[risk: {a['risk'].upper()}] {a['summary']}", file=sys.stderr)
            if a["reasons"]:
                print("  " + "; ".join(a["reasons"]), file=sys.stderr)
            if not a.get("model"):
                print("  fixed checks only; no model assessment", file=sys.stderr)
        if req.detail:
            print(req.detail, file=sys.stderr)
        try:
            # `/approve <mode>` switches modes for later calls; this one is still asked about
            while (mode := mode_switch(line := await self._stdin.line(req.keys()))) is not None and self._set_mode:
                await self._set_mode(mode)
            answer = parse_answer(line)
            if answer:
                return answer
            note = (await self._stdin.line(f"{req.note_prompt} (optional, enter to skip) ")).strip()
        except EOFError:
            return ApprovalDecision(allow=False, note="not run: stdin closed, so nobody can approve it")
        return ApprovalDecision(allow=False, note=note or None)
