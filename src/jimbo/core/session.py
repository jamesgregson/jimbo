import uuid

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from .deps import Deps
from .events import Event, EventKind, EventSink


class AgentSession:
    """Headless conversation driver. Emits Events to sinks; knows nothing about any UI."""

    def __init__(self, agent: Agent[Deps, str], deps: Deps, sinks: list[EventSink],
                 *, request_limit: int = 50, session_id: str | None = None) -> None:
        self.agent = agent
        self.deps = deps
        self.sinks = sinks
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.history: list[ModelMessage] = []
        self._limits = UsageLimits(request_limit=request_limit)
        self._seq = 0

    async def emit(self, kind: EventKind, **data) -> None:
        self._seq += 1
        ev = Event(seq=self._seq, session_id=self.session_id, kind=kind, data=data)
        for s in self.sinks:
            await s.emit(ev)

    async def send(self, text: str) -> str | None:
        """Run one turn. Returns the final text, or None if the run failed (error event emitted)."""
        await self.emit("user_message", text=text)
        try:
            async with self.agent.iter(text, deps=self.deps, message_history=self.history,
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
                        async with node.stream(run.ctx) as stream:
                            async for ev in stream:
                                if isinstance(ev, FunctionToolCallEvent):
                                    await self.emit("tool_call", id=ev.part.tool_call_id,
                                                    tool=ev.part.tool_name, args=ev.part.args_as_dict())
                                elif isinstance(ev, FunctionToolResultEvent):
                                    await self.emit("tool_result", id=ev.tool_call_id,
                                                    ok=ev.part.part_kind == "tool-return",
                                                    content=str(ev.part.content))
                result = run.result
            assert result is not None
        except Exception as e:  # surfaced to the UI/log; the session stays usable
            await self.emit("error", type=type(e).__name__, message=str(e))
            await self.emit("run_finished", ok=False)
            return None
        self.history = result.all_messages()
        u = result.usage
        await self.emit("assistant_text", text=result.output)
        await self.emit("usage", requests=u.requests, input_tokens=u.input_tokens,
                        output_tokens=u.output_tokens)
        await self.emit("run_finished", ok=True)
        return result.output
