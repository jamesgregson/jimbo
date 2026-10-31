"""Inline terminal UI: Rich for output (streaming markdown), prompt_toolkit for input."""

import asyncio
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.completion import Completer, Completion, merge_completers
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.keys import Keys
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from pydantic_ai.messages import BinaryContent

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.attachments import UserMessage, check_image
from ..core.events import Event
from ..core.stats import format_stats
from ..tools.search import list_files
from .clipboard import clipboard_png

HISTORY_FILE = Path.home() / ".jimbo_history"
Ask = Callable[[str], Awaitable[str]]
# Terminals that report modified keys (kitty/CSI-u, xterm modifyOtherKeys) send Shift+Enter as a distinct
# sequence; treat it like Alt+Enter. Terminals that send a bare Enter for Shift+Enter can't be helped.
for _seq in ("\x1b[13;2u", "\x1b[27;2;13~"):
    ANSI_SEQUENCES[_seq] = (Keys.Escape, Keys.ControlM)

PASTE_COLLAPSE_LINES = 10  # pastes longer than this become a [pasted #n: m lines] placeholder
PASTE_RE = re.compile(r"\[pasted #(\d+): \d+ lines\]")
IMAGE_RE = re.compile(r"\[image #(\d+)\]")


def _clip(s: str, n: int = 100) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class SlashCompleter(Completer):
    """Completes /command names (filled from the `commands` event) at the start of the input."""

    def __init__(self) -> None:
        self.commands: dict[str, str] = {}

    def get_completions(self, document, complete_event):
        t = document.text_before_cursor
        if not t.startswith("/") or " " in t:
            return
        for name, help in self.commands.items():
            if name.startswith(t[1:]):
                yield Completion("/" + name, start_position=-len(t), display_meta=help)


class FileCompleter(Completer):
    """Completes `@path` mentions with workspace files (the core inlines them on send)."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._files: list[str] = []
        self._loaded = 0.0

    def _list(self) -> list[str]:
        if time.monotonic() - self._loaded > 5:
            try:
                self._files = list_files(self.workspace, self.workspace)
            except OSError:
                self._files = []
            self._loaded = time.monotonic()
        return self._files

    def get_completions(self, document, complete_event):
        m = re.search(r"(?<![\w@])@([^\s@]*)$", document.text_before_cursor)
        if not m:
            return
        want = m.group(1).lower()
        hits = [f for f in self._list() if want in f.lower()]
        hits.sort(key=lambda f: (not f.lower().startswith(want), len(f)))
        for f in hits[:50]:
            yield Completion("@" + f, start_position=-len(m.group(0)))


def _history() -> History:
    try:
        return FileHistory(str(HISTORY_FILE))
    except OSError:
        return InMemoryHistory()


def _block_end(buf: str) -> int | None:
    """Offset of the first blank line that ends a complete markdown block (outside code fences), if any."""
    pos, fence, seen = 0, False, False
    while (nl := buf.find("\n", pos)) != -1:
        line = buf[pos:nl]
        if line.lstrip().startswith("```"):
            fence, seen = not fence, True
        elif not fence:
            if not line.strip():
                if seen:
                    return pos
            else:
                seen = True
        pos = nl + 1
    return None


class TuiFrontend:
    supports_steering = True  # the prompt stays active during a run; what's typed there steers it

    def __init__(self, console: Console | None = None, ask: Ask | None = None,
                 workspace: Path | None = None) -> None:
        """`console` and `ask` (a one-line prompt function) can be injected for tests."""
        self.console = console or Console()
        kb = KeyBindings()
        self._cancel: Callable[[], object] | None = None
        self._running = False
        self._prompting = False  # next_user_input is waiting on the main prompt
        self._question: tuple[str, asyncio.Future[str]] | None = None  # asked through the main prompt
        self._pastes: dict[int, str] = {}
        self._images: dict[int, BinaryContent] = {}

        @kb.add("escape", "enter")  # Shift+Enter / Alt+Enter insert a newline; plain Enter submits
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.BracketedPaste)  # newlines in a paste must not submit; long pastes collapse
        def _(event) -> None:
            event.current_buffer.insert_text(self.collapse_paste(event.data))

        @kb.add("c-c")  # during a run: stop it; otherwise: the usual interrupt
        def _(event) -> None:
            if self._running and self._cancel:
                self._cancel()
            else:
                event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        @kb.add("escape", filter=Condition(lambda: self._running))
        def _(event) -> None:
            if self._cancel:
                self._cancel()

        @kb.add("c-v")  # image from the clipboard (Cmd+V pastes text via the terminal)
        def _(event) -> None:
            event.app.create_background_task(self._paste_image(event.current_buffer))

        self._session: PromptSession | None = None
        self._kb = kb
        self.completer = SlashCompleter()
        self.file_completer = FileCompleter(workspace or Path.cwd())
        self._ask = ask
        self._buf = ""  # streamed assistant text not yet printed
        self._streamed = False
        self._usage: dict | None = None
        self._stats = ""  # latest context/usage line, shown in the prompt toolbar and the working spinner
        self._interrupted = False

    def _prompt_session(self) -> PromptSession:
        if self._session is None:
            self._session = PromptSession(history=_history(), key_bindings=self._kb,
                                          completer=merge_completers([self.completer, self.file_completer]),
                                          complete_while_typing=True,
                                          bottom_toolbar=self._toolbar, refresh_interval=0.5)
        return self._session

    def bind_cancel(self, cancel: Callable[[], object]) -> None:
        """Called by run_frontend: Ctrl+C / Esc during a run stop it."""
        self._cancel = cancel

    def _toolbar(self) -> str | None:
        return (f"working… {self._stats}  (Esc stops · type to steer)" if self._running else self._stats) or None

    def _message(self) -> str:
        if self._question:
            return self._question[0]
        return "steer> " if self._running else "\n> "

    def _invalidate(self) -> None:
        if self._session is not None:
            self._session.app.invalidate()

    async def _prompt(self, message) -> str:
        if self._ask:
            return await self._ask(message() if callable(message) else message)
        return await self._prompt_session().prompt_async(message)

    async def _ask_line(self, message: str) -> str:
        """Ask a one-line question. While the main prompt is up (a run is in flight) it takes the answer;
        a second prompt on the same terminal would fight it for the keyboard."""
        if not self._prompting:
            return await self._prompt(message)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._question = (message, fut)
        self._invalidate()
        try:
            return await fut
        finally:
            self._question = None
            self._invalidate()

    def collapse_paste(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        n = text.count("\n") + 1
        if n <= PASTE_COLLAPSE_LINES:
            return text
        i = max(self._pastes, default=0) + 1
        self._pastes[i] = text
        return f"[pasted #{i}: {n} lines]"

    async def _paste_image(self, buffer) -> None:
        data = await clipboard_png()
        if data is None:
            self.console.print(Text("no image on the clipboard", style="dim yellow"))
            return
        if err := check_image("image/png", data):
            self.console.print(Text(err, style="red"))
            return
        i = max(self._images, default=0) + 1
        self._images[i] = BinaryContent(data, media_type="image/png")
        buffer.insert_text(f"[image #{i}]")

    def expand(self, text: str) -> str | UserMessage:
        """Replace paste placeholders with their text and lift image chips out as attachments."""
        text = PASTE_RE.sub(lambda m: self._pastes.get(int(m.group(1)), m.group(0)), text)
        images = [self._images[i] for m in IMAGE_RE.finditer(text) if (i := int(m.group(1))) in self._images]
        self._pastes.clear()
        if not images:
            self._images.clear()
            return text
        text = IMAGE_RE.sub("", text).strip()
        self._images.clear()
        return UserMessage(text, images)

    async def next_user_input(self) -> str | UserMessage | None:
        while True:
            self._prompting = True
            try:
                if self._ask:
                    text = await self._prompt(self._message)
                else:
                    with patch_stdout(raw=True):  # run output prints above the prompt, not through it
                        text = await self._prompt(self._message)
            except EOFError:
                return None
            except KeyboardInterrupt:
                if self._interrupted:
                    return None
                self._interrupted = True
                self.console.print("[dim](press Ctrl+C again or Ctrl+D to quit)[/dim]")
                continue
            finally:
                self._prompting = False
            if self._question:  # the line answers a question (approval), it isn't a message
                if not self._question[1].done():
                    self._question[1].set_result(text)
                continue
            self._interrupted = False
            return self.expand(text)

    def _flush_blocks(self, *, final: bool = False) -> None:
        """Print the complete markdown blocks buffered so far (all of it when `final`)."""
        while (i := _block_end(self._buf)) is not None:
            if self._buf[:i].strip():
                self.console.print(Markdown(self._buf[:i]))
            self._buf = self._buf[i:].lstrip("\n")
        if final:
            if self._buf.strip():
                self.console.print(Markdown(self._buf))
            self._buf = ""

    def _stop_live(self) -> None:
        self._flush_blocks(final=True)
        self._streamed = False

    async def render(self, event: Event) -> None:
        d = event.data
        c = self.console
        match event.kind:
            case "user_message":
                if d.get("steer"):
                    self._stop_live()
                    c.print(Text(f"↳ steering: {_clip(d['text'], 200)}", style="bold yellow"))
                else:
                    self._running = True
            case "assistant_text_delta":
                self._buf += d["text"]
                self._streamed = True
                self._flush_blocks()
            case "tool_call":
                self._stop_live()
                args = ", ".join(f"{k}={v!r}" for k, v in d["args"].items())
                c.print(Text(f"▸ {d['tool']}({_clip(args)})", style="dim cyan"))
            case "tool_result":
                if d["ok"]:
                    first = d["content"].strip().splitlines()[:1]
                    c.print(Text(f"  ✓ {_clip(first[0]) if first else ''}", style="dim green"))
                else:
                    c.print(Text(f"  ✗ {_clip(d['content'], 300)}", style="red"))
            case "assistant_text":
                streamed = self._streamed
                self._stop_live()
                if not streamed:
                    c.print(Markdown(d["text"]))
            case "approval_resolved" if d["decided_by"] != "user":
                c.print(Text(f"  approval: {d['tool']} → {d['action']} ({d['decided_by']})", style="dim"))
            case "usage":
                self._usage = d
            case "context":
                self._stats = format_stats(d)
            case "todos":
                self._stop_live()
                for t in d["items"]:
                    mark, style = {"done": ("✓", "dim green"), "in_progress": ("▸", "bold yellow")}.get(
                        t["status"], ("○", "dim"))
                    c.print(Text(f"  {mark} {t['content']}", style=style))
            case "commands":
                self.completer.commands = {c["name"]: c["help"] for c in d["commands"]}
            case "compaction":
                self._stop_live()
                c.print(Text(f"  compacted history: {d['messages_before']} → {d['messages_after']} messages",
                             style="dim"))
            case "command_output":
                c.print(Text(d["text"], style="" if d["ok"] else "red"))
            case "error":
                self._stop_live()
                c.print(Panel(f"{d['type']}: {d['message']}", title="error", border_style="red"))
            case "run_finished":
                self._stop_live()
                self._running = False
                self._invalidate()
                if d.get("cancelled"):
                    c.print(Text("(interrupted)", style="dim yellow"))
                if self._usage:
                    u, self._usage = self._usage, None
                    c.print(Rule(self._stats or f"{u['requests']} req · {u['input_tokens']} in · {u['output_tokens']} out", style="dim", align="right"))

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        self._stop_live()
        body: list = [Text(req.summary, style="bold")]
        if a := req.assessment:
            style = {"low": "green", "medium": "yellow", "high": "red", "critical": "bold white on red"}[a["risk"]]
            line = Text.assemble((f" {a['risk'].upper()} ", style), f"  {a['summary']}")
            body.append(line)
            if a["reasons"]:
                body.append(Text("  · " + "; ".join(a["reasons"]), style="dim"))
            if a.get("model"):
                body.append(Text(f"  assessed by {a['model']}", style="dim"))
        if req.detail:
            is_diff = req.detail.lstrip().startswith(("---", "diff ", "@@"))
            body.append(Syntax(req.detail, "diff", theme="ansi_dark") if is_diff else Text(req.detail))
        self.console.print(Panel(Group(*body), title=f"Approve {req.tool}", border_style="yellow"))
        try:
            ans = (await self._ask_line("[y]es / [n]o / [a]lways this session? ")).strip().lower()
            if ans.startswith("a"):
                return ApprovalDecision(allow=True, remember="session")
            if ans.startswith("y"):
                return ApprovalDecision(allow=True)
            note = (await self._ask_line("Why? (optional, enter to skip) ")).strip()
        except (EOFError, KeyboardInterrupt):
            return ApprovalDecision(allow=False, note="no answer")
        return ApprovalDecision(allow=False, note=note or None)
