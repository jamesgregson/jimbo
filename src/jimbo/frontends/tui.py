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

from ..core.approval import ApprovalDecision, ApprovalRequest, parse_answer
from ..core.attachments import UserMessage, check_image
from ..core.events import Event
from ..core.agents import PROGRESS
from ..core.stats import AgentProgress, agent_prefix, format_stats, output_preview
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


class ModelCompleter(Completer):
    """Completes model values in /model, /models set and /models alias: aliases, `provider:`, and the provider's
    model names once it has answered. Names and providers come from the `config` event."""

    PATTERN = re.compile(r"/(model|models set \S+|models alias \S+)\s+(\S*)")

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}
        self.providers: list[str] = []
        self.lister: Callable[[str], Awaitable[list[str] | str]] | None = None
        self.on_update: Callable[[], None] = lambda: None
        self._models: dict[str, list[str] | str] = {}
        self._tasks: set[asyncio.Task] = set()

    def reset(self, aliases: dict[str, str], providers: list[str]) -> None:
        self.aliases, self.providers = aliases, providers
        self._models.clear()  # a provider may have changed

    def get_completions(self, document, complete_event):
        m = self.PATTERN.fullmatch(document.text_before_cursor)
        if not m:
            return
        word = m.group(2)
        provider, colon, _ = word.partition(":")
        if colon:
            found = self._models.get(provider)
            if found is None:
                self._fetch(provider)
            for name in found if isinstance(found, list) else []:
                if f"{provider}:{name}".startswith(word):
                    yield Completion(f"{provider}:{name}", start_position=-len(word))
            return
        if not m.group(1).startswith("models alias"):  # an alias names a model, not another alias
            for name, spec in self.aliases.items():
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word), display_meta=spec)
        for p in self.providers:
            if p.startswith(word):
                yield Completion(p + ":", start_position=-len(word))

    def _fetch(self, provider: str) -> None:
        if self.lister is None or provider not in self.providers or provider in self._models:
            return
        self._models[provider] = []  # asked; nothing to offer until it answers

        async def fetch() -> None:
            self._models[provider] = await self.lister(provider)
            self.on_update()

        try:
            task = asyncio.get_running_loop().create_task(fetch())
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class _SkippableHistory(History):
    """Input history that leaves out lines typed while `skip()` is true (API keys)."""

    def __init__(self, inner: History, skip: Callable[[], bool]) -> None:
        super().__init__()
        self.inner, self.skip = inner, skip

    def load_history_strings(self):
        return self.inner.load_history_strings()

    def store_string(self, string: str) -> None:
        self.inner.store_string(string)

    def append_string(self, string: str) -> None:
        if not self.skip():
            super().append_string(string)


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
        self._secret = False  # the prompt is taking a key: hidden, and not kept in history
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
        self.model_completer = ModelCompleter()
        self.model_completer.on_update = self._refresh_completions
        self._ask = ask
        self._buf = ""  # streamed assistant text not yet printed
        self._think = ""  # streamed reasoning not yet printed
        self._streamed = False
        self._usage: dict | None = None
        self._stats = ""  # latest context/usage line, shown in the prompt toolbar and the working spinner
        self._status: dict | None = None  # model and modes, shown in the prompt toolbar
        self._session_id = ""  # shown in the prompt toolbar; changes on /clear and /resume
        self._busy: str | None = None  # a slow command outside a run, e.g. "compacting"
        self._agent: AgentProgress | None = None  # a running agent, shown in the toolbar instead of its thinking
        self._interrupted = False

    def _prompt_session(self) -> PromptSession:
        if self._session is None:
            self._session = PromptSession(history=_SkippableHistory(_history(), lambda: self._secret),
                                          key_bindings=self._kb, is_password=Condition(lambda: self._secret),
                                          completer=merge_completers([self.completer, self.model_completer,
                                                                      self.file_completer]),
                                          complete_while_typing=True,
                                          bottom_toolbar=self._toolbar, refresh_interval=0.5)
        return self._session

    def bind_cancel(self, cancel: Callable[[], object]) -> None:
        """Called by run_frontend: Ctrl+C / Esc during a run stop it."""
        self._cancel = cancel

    def bind_model_list(self, lister: Callable[[str], Awaitable[list[str] | str]]) -> None:
        """Called by run_frontend: model names for completion."""
        self.model_completer.lister = lister

    def _refresh_completions(self) -> None:
        if self._session is not None and self._session.app.is_running:
            self._session.default_buffer.start_completion(select_first=False)

    def _state(self) -> str:
        d = self._status
        if not d:
            return ""
        flags = "".join(f" [{f}]" for f, on in (("PLAN", d["mode"] == "plan"), ("YOLO", d["yolo"]), ("AUTO", d.get("auto"))) if on)
        return f"{d['model']}{flags} · {self._session_id} · "

    def _toolbar(self) -> str | None:
        stats = self._state() + self._stats
        if self._running:
            doing = f"{self._agent.line()} · " if self._agent else ""
            return f"working… {doing}{stats}  (Esc stops · type to steer)"
        return (f"{self._busy}… {stats}" if self._busy else stats) or None

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

    def _flush_thinking(self, *, final: bool = False) -> None:
        """Print the complete reasoning lines buffered so far (all of it when `final`), dimmed."""
        if final:
            done, self._think = self._think, ""
        else:
            done, _, self._think = self._think.rpartition("\n")
        if done.strip():
            self.console.print(Text(done, style="dim italic"))

    def _stop_live(self) -> None:
        self._flush_thinking(final=True)
        self._flush_blocks(final=True)
        self._streamed = False

    async def render(self, event: Event) -> None:
        d = event.data
        c = self.console
        self._session_id = event.session_id
        if agent := d.get("agent"):
            if self._agent is None or self._agent.name != agent["name"]:
                self._agent = AgentProgress(agent["name"])
            self._agent.update(event.kind, d)
            self._invalidate()
            if event.kind in PROGRESS:  # a status only; the agent's thinking and text are in the trace
                return
        elif event.kind in ("tool_result", "run_finished"):  # the agent's run_agent call returned
            self._agent = None
        match event.kind:
            case "status":
                if self._status is None:
                    sandbox = "sandboxed" if d["sandboxed"] else "NOT sandboxed"
                    c.print(Text(f"jimbo · {d['model']} · {d['workspace']} · {sandbox} · session {event.session_id}",
                                 style="dim"))
                self._status = d
                self._invalidate()
            case "user_message":
                if d.get("steer"):
                    self._stop_live()
                    c.print(Text(f"↳ steering: {_clip(d['text'], 200)}", style="bold yellow"))
                else:
                    self._running = True
            case "assistant_thinking_delta":
                self._think += d["text"]
                self._flush_thinking()
            case "assistant_text_delta":
                self._flush_thinking(final=True)
                self._buf += d["text"]
                self._streamed = True
                self._flush_blocks()
            case "tool_call":
                self._stop_live()
                args = ", ".join(f"{k}={v!r}" for k, v in d["args"].items())
                c.print(Text(f"▸ {agent_prefix(d)}{d['tool']}({_clip(args)})", style="dim cyan"))
            case "tool_result":
                if d["ok"]:
                    first = d["content"].strip().splitlines()[:1]
                    failed = d.get("tool") == "shell" and first != ["exit code 0"]
                    c.print(Text(f"  ✓ {agent_prefix(d)}{_clip(first[0]) if first else ''}", style="yellow" if failed else "dim green"))
                    if d.get("tool") == "shell":
                        for line in output_preview(d["content"]):
                            c.print(Text(f"    {line}", style="dim"))
                else:
                    c.print(Text(f"  ✗ {agent_prefix(d)}{_clip(d['content'], 300)}", style="red"))
            case "assistant_text":
                streamed = self._streamed
                self._stop_live()
                if not streamed:
                    c.print(Markdown(d["text"]))
            case "approval_resolved" if d["decided_by"] != "user":
                c.print(Text(f"  approval: {d['tool']} → {d['action']} ({d['decided_by']})", style="dim"))
                if d.get("detail"):
                    c.print(Syntax(d["detail"], "diff", theme="ansi_dark"))
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
            case "config":
                self.model_completer.reset(d["aliases"], [p["name"] for p in d["providers"]])
            case "compaction":
                self._stop_live()
                c.print(Text(f"  {agent_prefix(d)}compacted history: {d['messages_before']} → {d['messages_after']} messages, ~{d['tokens_before']} → ~{d['tokens_after']} tokens",
                             style="dim"))
            case "wrap_up":
                self._stop_live()
                c.print(Text(f"  {agent_prefix(d)}wrap-up requested: {d['left']} request(s) left in this turn", style="dim"))
            case "busy":
                self._busy = d["label"] if d["on"] else None
                self._invalidate()
            case "command_output":
                c.print(Text(d["text"], style="" if d["ok"] else "red"))
            case "error":
                self._stop_live()
                c.print(Panel(f"{agent_prefix(d)}{d['type']}: {d['message']}", title="error", border_style="red"))
            case "run_finished":
                self._stop_live()
                self._running = False
                self._invalidate()
                if d.get("cancelled"):
                    c.print(Text("(interrupted)", style="dim yellow"))
                if self._usage:
                    u, self._usage = self._usage, None
                    c.print(Rule(self._stats or f"{u['requests']} req · {u['input_tokens']} in · {u['output_tokens']} out", style="dim", align="right"))

    async def confirm(self, message: str) -> bool:
        """A yes/no question; only an exact yes counts."""
        self._stop_live()
        self.console.print(Panel(Text(message, style="bold"), title="Confirm", border_style="red"))
        try:
            answer = await self._ask_line("[y]es / [n]o? ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")

    async def ask_secret(self, message: str) -> str | None:
        """A line that isn't echoed or kept in input history."""
        self._stop_live()
        self._secret = True
        try:
            return await self._ask_line(message)
        except (EOFError, KeyboardInterrupt):
            return None
        finally:
            self._secret = False

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
            else:
                body.append(Text("  fixed checks only; no model assessment", style="yellow"))
        if req.detail:
            is_diff = req.detail.lstrip().startswith(("---", "diff ", "@@"))
            body.append(Syntax(req.detail, "diff", theme="ansi_dark") if is_diff else Text(req.detail))
        self.console.print(Panel(Group(*body), title=f"Approve {req.tool}", border_style="yellow"))
        try:
            answer = parse_answer(await self._ask_line(f"[y]es / [n]o / [a]lways ({req.always})? "))
            if answer:
                return answer
            note = (await self._ask_line("Why? (optional, enter to skip) ")).strip()
        except (EOFError, KeyboardInterrupt):
            return ApprovalDecision(allow=False, note="no answer")
        return ApprovalDecision(allow=False, note=note or None)
