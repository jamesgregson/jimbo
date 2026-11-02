import asyncio
import uuid
from dataclasses import replace

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserContent,
    UserPromptPart,
    repair_messages,
)
from pydantic_ai.usage import RunUsage

from .attachments import build_content, expand_mentions, image_meta
from .commands import CommandRegistry
from .compaction import compact as compact_history, render_transcript
from .deps import Deps
from .events import Event, EventKind, EventSink
from .store import REPLAY_KINDS, SessionStore


class AgentSession:
    """Headless conversation driver. Emits Events to sinks; knows nothing about any UI."""

    def __init__(self, agent: Agent[Deps, str], deps: Deps, sinks: list[EventSink],
                 *, request_limit: int = 50, session_id: str | None = None,
                 context_window: int | None = None, vision: bool = True,
                 compact_threshold: float = 0.8, compact_keep: int = 6) -> None:
        self.agent = agent
        self.deps = deps
        self.sinks = sinks
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.history: list[ModelMessage] = []
        self.commands = CommandRegistry()
        self.context_window = context_window
        self.vision = vision  # False: images are dropped with a warning instead of sent to the model
        self.compact_threshold = compact_threshold  # 0 disables auto-compaction
        self.compact_keep = compact_keep
        self.store: SessionStore | None = None  # set to persist history after each turn (enables /resume)
        self.replay_sink: EventSink | None = None  # where resume() redraws past events (the frontend only)
        self.deps.midrun_compact = self._compact_midrun
        self.deps.midrun_steer = self._inject_steers
        self.failed = False  # a run or command failed; one-shot mode turns this into the exit code
        self.last_output: str | None = None  # the latest shell result in full, for /output
        self._status_sent: dict | None = None
        self._steers: list[str | list[UserContent]] = []  # typed during a run, not yet seen by the model
        self.context_tokens = 0  # size of the last model request + response
        self.total_usage = RunUsage()  # whole session; updated live during a run
        self.turn_usage = RunUsage()  # the current (or most recent) prompt
        self._limits = UsageLimits(request_limit=request_limit)
        self._seq = 0
        self._todos_sent: list = []
        self._task: asyncio.Task | None = None

    async def emit(self, kind: EventKind, **data) -> None:
        if kind in ("run_finished", "command_output") and not data.get("ok", True):
            self.failed = True
        self._seq += 1
        ev = Event(seq=self._seq, session_id=self.session_id, kind=kind, data=data)
        for s in self.sinks:
            await s.emit(ev)

    async def emit_context(self) -> None:
        """Live stats: working-context size, plus token usage for the current prompt and the session."""
        def u(x: RunUsage) -> dict:
            return {"requests": x.requests, "input_tokens": x.input_tokens, "output_tokens": x.output_tokens}
        await self.emit("context", tokens=self.context_tokens, window=self.context_window,
                        turn=u(self.turn_usage), session=u(self.total_usage))

    async def emit_status(self, *, initial: bool = False) -> None:
        """Tell the UI what state it's in. After the initial call, only sent when something changed."""
        d = self.deps
        status = {"model": d.models.explain("main").spec if d.models else None, "workspace": str(d.workspace),
                  "mode": d.mode, "yolo": any(p.name == "yolo" for p in d.gate.policies),
                  "sandboxed": d.sandbox is not None}
        if initial or self._status_sent not in (None, status):
            self._status_sent = status
            await self.emit("status", **status)

    async def _emit_todos(self) -> None:
        if self.deps.todos != self._todos_sent:
            self._todos_sent = list(self.deps.todos)
            await self.emit("todos", items=[t.model_dump() for t in self.deps.todos])

    def persist(self) -> None:
        """Save history and todos so the session can be resumed. Best effort: a full disk must not kill a turn."""
        if self.store is None:
            return
        try:
            self.store.save(self.session_id, self.history, [t.model_dump() for t in self.deps.todos])
        except OSError:
            pass

    def new_session(self) -> None:
        """Later turns save under a new id, so the conversation so far stays resumable."""
        self.session_id = uuid.uuid4().hex[:12]
        self._seq = 0

    async def resume(self, prefix: str) -> str:
        """Load a saved session into this one: history, todos and id (so the log keeps appending to the
        same file), and redraw its events in the UI. Raises ValueError if it can't be found or read."""
        from ..tools.todos import TodoItem
        if self.store is None:
            raise ValueError("session saving is disabled (log_sessions = false)")
        saved = self.store.load(self.store.resolve(prefix))
        events = self.store.events(saved.id)
        self.history = saved.history
        self.session_id = saved.id
        self._seq = max((e.seq for e in events), default=0)
        self.deps.todos = [TodoItem.model_validate(t) for t in saved.todos]
        self._todos_sent = list(self.deps.todos)
        self.context_tokens = 0
        self.turn_usage, self.total_usage = RunUsage(), RunUsage()
        if self.replay_sink is not None:
            for ev in events:
                if ev.kind in REPLAY_KINDS:
                    await self.replay_sink.emit(ev)
        await self.emit_context()
        msg = f"Resumed session {saved.id} ({len(self.history)} messages)."
        started = self.store.workspace_of(saved.id)
        if started and self.store.workspace and started != str(self.store.workspace):
            msg += f" It started in {started}, but you are in {self.store.workspace}."
        return msg

    def cancel(self) -> bool:
        """Interrupt the in-flight run, if any. The partial turn is kept in history."""
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        return True

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def steer(self, content: str | list[UserContent]) -> None:
        """Queue a message typed during a run; the model sees it with its next request. One still queued
        when the run ends is returned by take_steers() for the caller to send as the next turn."""
        self._steers.append(content)

    def take_steers(self) -> list[str | list[UserContent]]:
        """Steers the run ended without delivering; the caller sends them as the next turn."""
        pending, self._steers = self._steers, []
        return pending

    async def _drop_steers(self) -> None:
        if pending := self.take_steers():
            await self.emit("command_output", command="steer", ok=False,
                            text=f"run did not finish; discarded {len(pending)} queued message(s)")

    async def _inject_steers(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if not self._steers or not messages or not isinstance(messages[-1], ModelRequest):
            return messages
        parts = []
        for content in self.take_steers():
            _, prompt = await self._compose(content, steer=True)
            parts.append(UserPromptPart(prompt))
        last = messages[-1]
        return [*messages[:-1], replace(last, parts=[*last.parts, *parts])]

    async def _compose(self, content: str | list[UserContent], *, steer: bool = False) -> tuple[str, str | list[UserContent]]:
        """Emit the user_message event for `content`; returns its text and what the model should see."""
        parts = [content] if isinstance(content, str) else list(content)
        text = "\n".join(p for p in parts if isinstance(p, str))
        images = [p for p in parts if isinstance(p, BinaryContent)]
        sent, files = expand_mentions(text, self.deps.workspace)
        warning = None
        if images and not self.vision:
            warning = f"the model is not marked vision-capable (vision = false); dropped {len(images)} image(s)"
            images = []
        attachments = [{"kind": "file", **f} for f in files] + image_meta(images)
        await self.emit("user_message", text=text, attachments=attachments, **({"steer": True} if steer else {}))
        if warning:
            await self.emit("command_output", command="attach", ok=False, text=warning)
        return text, build_content(sent, images)

    def _needs_compaction(self) -> bool:
        return bool(self.compact_threshold and self.context_window
                    and self.context_tokens / self.context_window > self.compact_threshold)

    async def _apply_compaction(self, messages: list[ModelMessage], *, focus: str = "",
                                allow_empty: bool = True) -> list[ModelMessage] | None:
        from ..tools.todos import format_todos
        todos = format_todos(self.deps.todos) if self.deps.todos else ""
        router = self.deps.models
        model = await router.aresolve("compaction") if router else self.agent.model
        new = await compact_history(model, messages, keep=self.compact_keep, focus=focus,
                                    todos=todos, allow_empty=allow_empty)
        if new is not None:
            before = self.context_tokens
            # rough estimate (~4 chars/token) until the next model request reports the real size
            self.context_tokens = len(render_transcript(new, return_limit=10**9)) // 4
            await self.emit("compaction", messages_before=len(messages), messages_after=len(new),
                            tokens_before=before, tokens_after=self.context_tokens)
            await self.emit_context()
        return new

    async def _compact_midrun(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if not self._needs_compaction():
            return messages
        try:
            return await self._apply_compaction(messages, allow_empty=False) or messages
        except Exception as e:  # compaction is best effort; the run continues with the full history
            await self.emit("error", type=type(e).__name__, message=f"auto-compaction failed: {e}")
            return messages

    async def compact(self, focus: str = "") -> str | None:
        """Summarize older history now. Returns a status line, or None if there was nothing to compact."""
        before = self.context_tokens
        new = await self._apply_compaction(self.history, focus=focus)
        if new is None:
            return None
        n = len(self.history)
        self.history = new
        self.persist()
        line = f"Compacted {n} messages into {len(new)}"
        if before:
            saved = max(before - self.context_tokens, 0)
            line += f"; context ~{before} → ~{self.context_tokens} tokens (saved ~{saved})"
        return line + "."

    async def send(self, content: str | list[UserContent]) -> str | None:
        """Run one turn. Returns the final text, or None if the run failed or was cancelled.

        `content` is text, or a list mixing text and `BinaryContent` images. `@path` mentions in the
        text are replaced by the file contents for the model; the user_message event keeps the original.
        """
        if self._needs_compaction():  # between turns: summarize before the new prompt is added
            try:
                await self.compact()
            except Exception as e:
                await self.emit("error", type=type(e).__name__, message=f"auto-compaction failed: {e}")
        self._task = asyncio.create_task(self._run(content))  # own task so cancel() can't hit the caller
        try:
            return await self._task
        finally:
            self._task = None
            if self.deps.models:
                self.deps.models.pop_scope()  # skill model choices last for one turn
            self.persist()

    async def _run(self, content: str | list[UserContent]) -> str | None:
        _, prompt = await self._compose(content)
        run = None
        self.deps.checkpoints.begin()
        base = self.total_usage
        self.turn_usage = RunUsage()
        try:
            async with self.agent.iter(prompt, deps=self.deps, message_history=self.history,
                                       usage_limits=self._limits) as run:
                async for node in run:
                    if Agent.is_model_request_node(node):
                        async with node.stream(run.ctx) as stream:
                            async for ev in stream:
                                if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                                    if ev.part.content:  # first chunk arrives here, not as a delta
                                        await self.emit("assistant_text_delta", text=ev.part.content)
                                elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                                    await self.emit("assistant_text_delta", text=ev.delta.content_delta)
                    elif Agent.is_call_tools_node(node):
                        u = node.model_response.usage
                        self.context_tokens = u.input_tokens + u.output_tokens
                        self.turn_usage = run.usage
                        self.total_usage = base + run.usage
                        await self.emit_context()
                        async with node.stream(run.ctx) as stream:
                            async for ev in stream:
                                if isinstance(ev, FunctionToolCallEvent):
                                    await self.emit("tool_call", id=ev.part.tool_call_id,
                                                    tool=ev.part.tool_name, args=ev.part.args_as_dict())
                                elif isinstance(ev, FunctionToolResultEvent):
                                    await self.emit("tool_result", id=ev.tool_call_id, tool=ev.part.tool_name,
                                                    ok=ev.part.part_kind == "tool-return",
                                                    content=str(ev.part.content))
                                    if ev.part.tool_name == "shell":
                                        self.last_output = str(ev.part.content)
                                    await self._emit_todos()
                                    await self.emit_status()
                result = run.result
            assert result is not None
        except asyncio.CancelledError:
            if run is not None:  # keep what happened so far; repair closes out unanswered tool calls
                self.history = repair_messages(run.all_messages())
                self.turn_usage, self.total_usage = run.usage, base + run.usage
            await self.emit_context()
            await self._drop_steers()
            await self.emit("run_finished", ok=False, cancelled=True)
            return None
        except Exception as e:  # surfaced to the UI/log; the session stays usable
            if run is not None:  # the work is done, so the model must remember it, as when cancelled
                self.history = repair_messages(run.all_messages())
                self.turn_usage, self.total_usage = run.usage, base + run.usage
            await self.emit("error", type=type(e).__name__, message=str(e))
            await self._drop_steers()
            await self.emit("run_finished", ok=False)
            return None
        self.history = result.all_messages()
        u = self.turn_usage = result.usage
        self.total_usage = base + u
        await self.emit("assistant_text", text=result.output)
        await self.emit("usage", requests=u.requests, input_tokens=u.input_tokens,
                        output_tokens=u.output_tokens)
        await self.emit_context()
        await self.emit("run_finished", ok=True)
        return result.output
