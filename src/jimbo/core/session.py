import asyncio
import uuid

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserContent,
    repair_messages,
)
from pydantic_ai.usage import RunUsage

from .attachments import build_content, expand_mentions, image_meta
from .commands import CommandRegistry
from .deps import Deps
from .events import Event, EventKind, EventSink


class AgentSession:
    """Headless conversation driver. Emits Events to sinks; knows nothing about any UI."""

    def __init__(self, agent: Agent[Deps, str], deps: Deps, sinks: list[EventSink],
                 *, request_limit: int = 50, session_id: str | None = None,
                 context_window: int | None = None, vision: bool = True) -> None:
        self.agent = agent
        self.deps = deps
        self.sinks = sinks
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.history: list[ModelMessage] = []
        self.commands = CommandRegistry()
        self.context_window = context_window
        self.vision = vision  # False: images are dropped with a warning instead of sent to the model
        self.context_tokens = 0  # size of the last model request + response
        self.total_usage = RunUsage()
        self._limits = UsageLimits(request_limit=request_limit)
        self._seq = 0
        self._todos_sent: list = []
        self._task: asyncio.Task | None = None

    async def emit(self, kind: EventKind, **data) -> None:
        self._seq += 1
        ev = Event(seq=self._seq, session_id=self.session_id, kind=kind, data=data)
        for s in self.sinks:
            await s.emit(ev)

    async def _emit_todos(self) -> None:
        if self.deps.todos != self._todos_sent:
            self._todos_sent = list(self.deps.todos)
            await self.emit("todos", items=[t.model_dump() for t in self.deps.todos])

    def cancel(self) -> bool:
        """Interrupt the in-flight run, if any. The partial turn is kept in history."""
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        return True

    async def send(self, content: str | list[UserContent]) -> str | None:
        """Run one turn. Returns the final text, or None if the run failed or was cancelled.

        `content` is text, or a list mixing text and `BinaryContent` images. `@path` mentions in the
        text are replaced by the file contents for the model; the user_message event keeps the original.
        """
        self._task = asyncio.create_task(self._run(content))  # own task so cancel() can't hit the caller
        try:
            return await self._task
        finally:
            self._task = None

    async def _run(self, content: str | list[UserContent]) -> str | None:
        parts = [content] if isinstance(content, str) else list(content)
        text = "\n".join(p for p in parts if isinstance(p, str))
        images = [p for p in parts if isinstance(p, BinaryContent)]
        sent, files = expand_mentions(text, self.deps.workspace)
        warning = None
        if images and not self.vision:
            warning = f"the model is not marked vision-capable (vision = false); dropped {len(images)} image(s)"
            images = []
        attachments = [{"kind": "file", **f} for f in files] + image_meta(images)
        await self.emit("user_message", text=text, attachments=attachments)
        if warning:
            await self.emit("command_output", command="attach", ok=False, text=warning)
        prompt = build_content(sent, images)
        run = None
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
                        await self.emit("context", tokens=self.context_tokens, window=self.context_window)
                        async with node.stream(run.ctx) as stream:
                            async for ev in stream:
                                if isinstance(ev, FunctionToolCallEvent):
                                    await self.emit("tool_call", id=ev.part.tool_call_id,
                                                    tool=ev.part.tool_name, args=ev.part.args_as_dict())
                                elif isinstance(ev, FunctionToolResultEvent):
                                    await self.emit("tool_result", id=ev.tool_call_id,
                                                    ok=ev.part.part_kind == "tool-return",
                                                    content=str(ev.part.content))
                                    await self._emit_todos()
                result = run.result
            assert result is not None
        except asyncio.CancelledError:
            if run is not None:  # keep what happened so far; repair closes out unanswered tool calls
                self.history = repair_messages(run.all_messages())
                self.total_usage = self.total_usage + run.usage
            await self.emit("run_finished", ok=False, cancelled=True)
            return None
        except Exception as e:  # surfaced to the UI/log; the session stays usable
            await self.emit("error", type=type(e).__name__, message=str(e))
            await self.emit("run_finished", ok=False)
            return None
        self.history = result.all_messages()
        u = result.usage
        self.total_usage = self.total_usage + u
        await self.emit("assistant_text", text=result.output)
        await self.emit("usage", requests=u.requests, input_tokens=u.input_tokens,
                        output_tokens=u.output_tokens)
        await self.emit("run_finished", ok=True)
        return result.output
