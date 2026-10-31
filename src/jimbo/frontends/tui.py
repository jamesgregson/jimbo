"""Inline terminal UI: Rich for output (streaming markdown), prompt_toolkit for input."""

import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, merge_completers
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.keys import Keys
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from pydantic_ai.messages import BinaryContent

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.attachments import UserMessage, check_image
from ..core.events import Event
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


class TuiFrontend:
    def __init__(self, console: Console | None = None, ask: Ask | None = None,
                 workspace: Path | None = None) -> None:
        """`console` and `ask` (a one-line prompt function) can be injected for tests."""
        self.console = console or Console()
        kb = KeyBindings()
        self._pastes: dict[int, str] = {}
        self._images: dict[int, BinaryContent] = {}

        @kb.add("escape", "enter")  # Shift+Enter / Alt+Enter insert a newline; plain Enter submits
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.BracketedPaste)  # newlines in a paste must not submit; long pastes collapse
        def _(event) -> None:
            event.current_buffer.insert_text(self.collapse_paste(event.data))

        @kb.add("c-v")  # image from the clipboard (Cmd+V pastes text via the terminal)
        def _(event) -> None:
            event.app.create_background_task(self._paste_image(event.current_buffer))

        self._session: PromptSession | None = None
        self._kb = kb
        self.completer = SlashCompleter()
        self.file_completer = FileCompleter(workspace or Path.cwd())
        self._ask = ask
        self._buf = ""
        self._live: Live | None = None
        self._spinner = None
        self._usage: dict | None = None
        self._context: dict | None = None
        self._interrupted = False

    def _prompt_session(self) -> PromptSession:
        if self._session is None:
            self._session = PromptSession(history=_history(), key_bindings=self._kb,
                                          completer=merge_completers([self.completer, self.file_completer]),
                                          complete_while_typing=True)
        return self._session

    async def _prompt(self, message: str) -> str:
        if self._ask:
            return await self._ask(message)
        return await self._prompt_session().prompt_async(message)

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
            try:
                text = await self._prompt("\n> ")
            except EOFError:
                return None
            except KeyboardInterrupt:
                if self._interrupted:
                    return None
                self._interrupted = True
                self.console.print("[dim](press Ctrl+C again or Ctrl+D to quit)[/dim]")
                continue
            self._interrupted = False
            return self.expand(text)

    def _start_spinner(self) -> None:
        """Show a spinner while the agent is working but nothing is streaming (model or tool wait)."""
        if self._spinner is None and self._live is None and self.console.is_terminal:
            self._spinner = self.console.status("[dim]working…[/dim]", spinner="dots")
            self._spinner.start()

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def _stop_live(self) -> None:
        self._stop_spinner()
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._buf = ""

    async def render(self, event: Event) -> None:
        d = event.data
        c = self.console
        match event.kind:
            case "user_message":
                self._start_spinner()
            case "assistant_text_delta":
                self._stop_spinner()
                self._buf += d["text"]
                if self._live is None:
                    self._live = Live(console=c, auto_refresh=False, vertical_overflow="visible")
                    self._live.start()
                self._live.update(Markdown(self._buf), refresh=True)
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
                self._start_spinner()  # the model is thinking again (or another tool is running)
            case "assistant_text":
                self._stop_spinner()
                if self._live is not None:
                    self._stop_live()
                else:
                    c.print(Markdown(d["text"]))
            case "approval_resolved" if d["decided_by"] != "user":
                c.print(Text(f"  approval: {d['tool']} → {d['action']} ({d['decided_by']})", style="dim"))
            case "usage":
                self._usage = d
            case "context":
                self._context = d
            case "todos":
                self._stop_live()
                for t in d["items"]:
                    mark, style = {"done": ("✓", "dim green"), "in_progress": ("▸", "bold yellow")}.get(
                        t["status"], ("○", "dim"))
                    c.print(Text(f"  {mark} {t['content']}", style=style))
            case "commands":
                self.completer.commands = {c["name"]: c["help"] for c in d["commands"]}
            case "command_output":
                c.print(Text(d["text"], style="" if d["ok"] else "red"))
            case "error":
                self._stop_live()
                c.print(Panel(f"{d['type']}: {d['message']}", title="error", border_style="red"))
            case "run_finished":
                self._stop_live()
                if d.get("cancelled"):
                    c.print(Text("(interrupted)", style="dim yellow"))
                if self._usage:
                    u, self._usage = self._usage, None
                    line = f"{u['requests']} req · {u['input_tokens']} in · {u['output_tokens']} out"
                    if (ctx := self._context) and ctx.get("window"):
                        line += f" · ctx {100 * ctx['tokens'] / ctx['window']:.0f}%"
                    c.print(Rule(line, style="dim", align="right"))

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        self._stop_live()
        body: list = [Text(req.summary, style="bold")]
        if req.detail:
            is_diff = req.detail.lstrip().startswith(("---", "diff ", "@@"))
            body.append(Syntax(req.detail, "diff", theme="ansi_dark") if is_diff else Text(req.detail))
        self.console.print(Panel(Group(*body), title=f"Approve {req.tool}", border_style="yellow"))
        try:
            ans = (await self._prompt("[y]es / [n]o / [a]lways this session? ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ApprovalDecision(allow=False, note="no answer")
        if ans.startswith("a"):
            return ApprovalDecision(allow=True, remember="session")
        return ApprovalDecision(allow=ans.startswith("y"))
