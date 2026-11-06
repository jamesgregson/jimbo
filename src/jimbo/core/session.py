import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
    repair_messages,
)
from pydantic_ai.usage import RunUsage

from .agents import AgentResult
from .approval import ApprovalModes
from .attachments import MAX_IMAGES, build_content, content_text, expand_mentions, image_meta, mentioned_images
from .commands import CommandRegistry
from .compaction import compact as compact_history, render_transcript
from .cost import Spend, estimate_cost
from .stats import OUTPUT_TOOLS, thinking_tokens
from .deps import Deps
from .events import Event, EventKind, EventSink
from .store import REPLAY_KINDS, SessionStore


class AgentSession:
    """Headless conversation driver. Emits Events to sinks; knows nothing about any UI."""

    def __init__(self, agent: Agent[Deps, str], deps: Deps, sinks: list[EventSink],
                 *, request_limit: int = 50, session_id: str | None = None,
                 context_window: int | None = None, vision: bool = True,
                 compact_threshold: float = 0.8, compact_keep: int = 6, nested: bool = False) -> None:
        self.agent = agent
        self.nested = nested  # an agent's run: its file changes belong to the turn that started it
        self.deps = deps
        self.sinks = sinks
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.history: list[ModelMessage] = []
        self.commands = CommandRegistry()
        self.context_window = context_window
        self.deps.vision = vision  # False: images are dropped with a warning instead of sent to the model
        self.compact_threshold = compact_threshold  # 0 disables auto-compaction
        self.compact_keep = compact_keep
        self.store: SessionStore | None = None  # set to persist history after each turn (enables /resume)
        self.replay_sink: EventSink | None = None  # where resume() redraws past events (the frontend only)
        self.approval_modes: ApprovalModes | None = None  # set by wiring; switched by /approve or the frontend
        self.confirm: Callable[[str], Awaitable[bool]] | None = None  # the frontend's yes/no question, if any
        self.ask_secret: Callable[[str], Awaitable[str | None]] | None = None  # the frontend's hidden question
        self.config: Any = None  # set by wiring: config_edit.ConfigControl, which saves model and provider changes
        self.deps.midrun_compact = self._compact_midrun
        self.deps.midrun_steer = self._inject_steers
        self.failed = False  # a run or command failed; one-shot mode turns this into the exit code
        self.last_output: str | None = None  # the latest shell result in full, for /output
        self._status_sent: dict | None = None
        self._steers: list[str | list[UserContent]] = []  # typed during a run, not yet seen by the model
        self.context_tokens = 0  # size of the last model request + response
        self.total_usage = RunUsage()  # whole session; updated live during a run
        self.turn_usage = RunUsage()  # the current (or most recent) prompt
        self.prices: dict[str, float] | None = None  # the main model's listed prices, when the provider gives them
        self.spend = Spend()  # this session id, across resumes; saved with its history
        self.run_spend = Spend()  # this jimbo process, across /clear and /resume
        self.request_limit = request_limit
        self._limits = UsageLimits(request_limit=request_limit)
        self.deps.request_limit = request_limit  # so the prompt can warn the model before it runs out
        self._seq = 0
        self._todos_sent: list = []
        self._instructions_sent: str | None = None  # logged again whenever they change, and in each new log
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
            return {"requests": x.requests, "input_tokens": x.input_tokens, "output_tokens": x.output_tokens,
                    "thinking_tokens": thinking_tokens(x)}
        # `turns` numbers the session's turns; turn["requests"] counts this turn's cycles, up to `limit`
        await self.emit("context", tokens=self.context_tokens, window=self.context_window,
                        turn=u(self.turn_usage), session=u(self.total_usage), turns=self.spend.turns,
                        limit=self.request_limit)

    async def emit_status(self, *, initial: bool = False) -> None:
        """Tell the UI what state it's in. After the initial call, only sent when something changed."""
        d = self.deps
        status = {"model": d.models.explain("main").spec if d.models else None, "workspace": str(d.workspace),
                  "mode": d.mode, "yolo": any(p.name == "yolo" for p in d.gate.policies),
                  "auto": any(p.name == "auto" for p in d.gate.policies),
                  "sandboxed": d.sandbox is not None, "sandbox": d.sandbox.kind if d.sandbox else None}
        if initial or self._status_sent not in (None, status):
            self._status_sent = status
            await self.emit("status", **status)

    async def _emit_instructions(self, text: str | None) -> None:
        """Log what the model is told besides the conversation, when it differs from the last request's."""
        if text != self._instructions_sent:
            self._instructions_sent = text
            await self.emit("instructions", text=text or "")

    async def _emit_todos(self) -> None:
        if self.deps.todos != self._todos_sent:
            self._todos_sent = list(self.deps.todos)
            await self.emit("todos", items=[t.model_dump() for t in self.deps.todos])

    def persist(self) -> None:
        """Save history and todos so the session can be resumed. Best effort: a full disk must not kill a turn."""
        if self.store is None:
            return
        try:
            self.store.save(self.session_id, self.history, [t.model_dump() for t in self.deps.todos],
                            self.spend.to_dict())
        except OSError:
            pass

    def new_session(self) -> None:
        """Later turns save under a new id, so the conversation so far stays resumable."""
        self.session_id = uuid.uuid4().hex[:12]
        self._seq = 0
        self.spend = Spend()
        self._instructions_sent = None

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
        self._instructions_sent = None
        self.deps.todos = [TodoItem.model_validate(t) for t in saved.todos]
        self._todos_sent = list(self.deps.todos)
        self.context_tokens = 0
        self.turn_usage, self.total_usage = RunUsage(), RunUsage()
        self.spend = Spend.from_dict(saved.spend)
        if self.replay_sink is not None:
            replayed = [ev for ev in events if ev.kind in REPLAY_KINDS]
            for ev in replayed:
                await self.replay_sink.emit(ev)
            last = next((ev for ev in reversed(replayed) if ev.kind in ("user_message", "run_finished")), None)
            if last is not None and last.kind == "user_message":
                # the log stops mid-run (jimbo was killed): close it in the UI only, the log stays as it was
                await self.replay_sink.emit(Event(seq=self._seq, session_id=self.session_id, kind="run_finished",
                                                  data={"ok": False}))
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
        found, found_meta, warnings = mentioned_images(text, self.deps.workspace)
        meta = image_meta(images) + found_meta
        images += found
        if len(images) > MAX_IMAGES:
            warnings.append(f"at most {MAX_IMAGES} images per message; dropped {len(images) - MAX_IMAGES}")
            images, meta = images[:MAX_IMAGES], meta[:MAX_IMAGES]
        if images and not self.vision:
            warnings.append(f"the model is not marked vision-capable (vision = false); dropped {len(images)} image(s)")
            images, meta = [], []
        if notes := [f"[The image {m['path']} is attached to this message.]" for m in meta if "path" in m]:
            sent += "\n\n" + "\n".join(notes)  # otherwise the model may go looking for the file with read_file
        attachments = [{"kind": "file", **f} for f in files] + meta
        await self.emit("user_message", text=text, attachments=attachments, **({"steer": True} if steer else {}))
        for warning in warnings:
            await self.emit("command_output", command="attach", ok=False, text=warning)
        return text, build_content(sent, images)

    @property
    def vision(self) -> bool:
        return self.deps.vision

    @vision.setter
    def vision(self, on: bool) -> None:
        self.deps.vision = on

    def _needs_compaction(self) -> bool:
        return bool(self.compact_threshold and self.context_window
                    and self.context_tokens / self.context_window > self.compact_threshold)

    async def _apply_compaction(self, messages: list[ModelMessage], *, focus: str = "",
                                allow_empty: bool = True) -> list[ModelMessage] | None:
        from ..tools.todos import format_todos
        todos = format_todos(self.deps.todos) if self.deps.todos else ""
        router = self.deps.models
        model = await router.aresolve("jimbo-compaction") if router else self.agent.model
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
        focus, self.deps.compact_requested = self.deps.compact_requested, None  # the model called compact
        if focus is None and not self._needs_compaction():
            return messages
        try:
            return await self._apply_compaction(messages, focus=focus or "", allow_empty=False) or messages
        except Exception as e:  # compaction is best effort; the run continues with the full history
            await self.emit("error", type=type(e).__name__, message=f"auto-compaction failed: {e}")
            return messages

    @asynccontextmanager
    async def busy(self, label: str) -> AsyncIterator[None]:
        """Show the UI as busy with `label` (e.g. "compacting") while a slow command runs. Runs have their own
        indicator (user_message .. run_finished); this is for work outside them."""
        await self.emit("busy", label=label, on=True)
        try:
            yield
        finally:
            await self.emit("busy", label=label, on=False)

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
            self.deps.no_questions = None  # so does a skill's `interactive: false`
            self.persist()

    async def delegate(self, name: str, task: str, *, model: str | None = None) -> None:
        """Run agent `name` directly on the user's behalf, without the main model. The exchange joins the
        conversation, so the user can follow up on the report."""
        self._task = asyncio.create_task(self._delegate(name, task, model))  # own task, as in send()
        try:
            await self._task
        finally:
            self._task = None
            self.persist()

    async def _delegate(self, name: str, task: str, model: str | None) -> None:
        self._count_turn()
        text = f"@{name} {task}"
        await self.emit("user_message", text=text, attachments=[])
        self.deps.checkpoints.begin()
        base = self.total_usage
        self.turn_usage = RunUsage()
        try:
            result = await self.deps.spawn_agent(self.deps.agents[name], task, self.turn_usage, model=model)
        except asyncio.CancelledError:  # stopped before the agent itself could report it
            result = AgentResult(False, "", cancelled=True)
        self.total_usage = base + self.turn_usage
        if result.text:
            self.history = [*self.history, ModelRequest(parts=[UserPromptPart(text)]),
                            ModelResponse(parts=[TextPart(result.text)])]
        if result.ok:
            await self.emit("assistant_text", text=result.text)
        elif not result.cancelled:
            await self.emit("error", type="AgentError", message=result.text)
        await self._emit_usage()
        await self.emit_context()
        await self.emit("run_finished", ok=result.ok, **({"cancelled": True} if result.cancelled else {}))

    async def _emit_usage(self) -> None:
        """Price the turn that just ended and add it to the totals. Prices are the main model's at this
        point, so a later model switch doesn't reprice earlier turns."""
        u = self.turn_usage
        cost = estimate_cost(u, self.prices)
        self.spend.add(u, cost)
        self.run_spend.add(u, cost)
        await self.emit("usage", requests=u.requests, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                        thinking_tokens=thinking_tokens(u), cost=cost)

    async def close(self) -> None:
        """Log what the session cost, as jimbo exits."""
        await self.emit("session_end", session=self.spend.to_dict(), run=self.run_spend.to_dict())

    async def refresh_files(self) -> None:
        """Pick up changes to agent and skill files, so a new or edited one works from the next input."""
        if self.deps.reload_skills:  # first: agents' `skills:` are checked against them
            await self.deps.reload_skills()
        if self.deps.reload_agents:
            for warning in self.deps.reload_agents() or []:
                await self.emit("command_output", command="agents", ok=True, text=warning)

    def _count_turn(self) -> None:
        self.spend.turns += 1
        self.run_spend.turns += 1

    async def _run(self, content: str | list[UserContent]) -> str | None:
        if not self.nested:  # an agent's run is part of its parent's turn
            self._count_turn()
        _, prompt = await self._compose(content)
        run = None
        if not self.nested:
            self.deps.checkpoints.begin()
        base = self.total_usage
        self.turn_usage = RunUsage()
        try:
            async with self.agent.iter(prompt, deps=self.deps, message_history=self.history,
                                       usage_limits=self._limits) as run:
                async for node in run:
                    if Agent.is_model_request_node(node):
                        self.deps.wrap_up_left = None
                        async with node.stream(run.ctx) as stream:
                            await self._emit_instructions(node.request.instructions)  # built by now
                            if (left := self.deps.wrap_up_left) is not None:
                                await self.emit("wrap_up", left=left)
                            async for ev in stream:
                                if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                                    if ev.part.content:  # first chunk arrives here, not as a delta
                                        await self.emit("assistant_text_delta", text=ev.part.content)
                                elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                                    await self.emit("assistant_text_delta", text=ev.delta.content_delta)
                                elif isinstance(ev, PartStartEvent) and isinstance(ev.part, ThinkingPart):
                                    if ev.part.content:
                                        await self.emit("assistant_thinking_delta", text=ev.part.content)
                                elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, ThinkingPartDelta):
                                    if ev.delta.content_delta:  # signature-only deltas carry no text
                                        await self.emit("assistant_thinking_delta", text=ev.delta.content_delta)
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
                                                    ok=isinstance(ev.part, ToolReturnPart)
                                                    and ev.part.outcome == "success",
                                                    content=content_text(ev.part.content))
                                    if ev.part.tool_name in OUTPUT_TOOLS:
                                        self.last_output = str(ev.part.content)
                                    await self._emit_todos()
                                    await self.emit_status()
                result = run.result
            assert result is not None
        except asyncio.CancelledError:
            if run is not None:  # keep what happened so far; repair closes out unanswered tool calls
                self.history = repair_messages(run.all_messages())
                self.turn_usage, self.total_usage = run.usage, base + run.usage
                await self._emit_usage()
            await self.emit_context()
            await self._drop_steers()
            await self.emit("run_finished", ok=False, cancelled=True)
            return None
        except Exception as e:  # surfaced to the UI/log; the session stays usable
            if run is not None:  # the work is done, so the model must remember it, as when cancelled
                self.history = repair_messages(run.all_messages())
                self.turn_usage, self.total_usage = run.usage, base + run.usage
                await self._emit_usage()
            await self.emit("error", type=type(e).__name__, message=str(e))
            await self._drop_steers()
            await self.emit("run_finished", ok=False)
            return None
        self.history = result.all_messages()
        u = self.turn_usage = result.usage
        self.total_usage = base + u
        await self.emit("assistant_text", text=result.output)
        await self._emit_usage()
        await self.emit_context()
        await self.emit("run_finished", ok=True)
        return result.output
