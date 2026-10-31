"""Inline terminal UI: Rich for output (streaming markdown), prompt_toolkit for input."""

from pathlib import Path
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.events import Event

HISTORY_FILE = Path.home() / ".jimbo_history"
Ask = Callable[[str], Awaitable[str]]


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


def _history() -> History:
    try:
        return FileHistory(str(HISTORY_FILE))
    except OSError:
        return InMemoryHistory()


class TuiFrontend:
    def __init__(self, console: Console | None = None, ask: Ask | None = None) -> None:
        """`console` and `ask` (a one-line prompt function) can be injected for tests."""
        self.console = console or Console()
        kb = KeyBindings()

        @kb.add("escape", "enter")  # Alt+Enter inserts a newline; plain Enter submits
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        self._session: PromptSession | None = None
        self._kb = kb
        self.completer = SlashCompleter()
        self._ask = ask
        self._buf = ""
        self._live: Live | None = None
        self._usage: dict | None = None
        self._context: dict | None = None
        self._interrupted = False

    def _prompt_session(self) -> PromptSession:
        if self._session is None:
            self._session = PromptSession(history=_history(), key_bindings=self._kb,
                                          completer=self.completer, complete_while_typing=True)
        return self._session

    async def _prompt(self, message: str) -> str:
        if self._ask:
            return await self._ask(message)
        return await self._prompt_session().prompt_async(message)

    async def next_user_input(self) -> str | None:
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
            return text

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._buf = ""

    async def render(self, event: Event) -> None:
        d = event.data
        c = self.console
        match event.kind:
            case "assistant_text_delta":
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
            case "assistant_text":
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
