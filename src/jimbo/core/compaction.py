"""Context compaction: replace old history with a model-written summary, keep the recent tail verbatim."""

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model

SUMMARY_INSTRUCTIONS = """\
You compact the transcript of a coding-agent session so work can continue from your summary alone.
Write a dense summary with these sections: Goal (what the user wants), Decisions and findings,
Files read or changed (with paths), Current state and open todos, Next steps.
Keep exact paths, identifiers, commands and error messages that matter. Do not invent anything."""

SUMMARY_PREFIX = "Summary of the earlier conversation (compacted to save context):\n\n"
ACK = "Understood. I'll continue from that summary."
RETURN_LIMIT = 500  # chars of each tool result kept in the transcript given to the summarizer


def _is_boundary(msg: ModelMessage) -> bool:
    """A safe place to start the kept tail: a request that answers no tool call."""
    return isinstance(msg, ModelRequest) and not any(
        isinstance(p, ToolReturnPart) or (isinstance(p, RetryPromptPart) and p.tool_name) for p in msg.parts)


def split_point(history: list[ModelMessage], keep: int, *, allow_empty: bool = True) -> int | None:
    """Index where the verbatim tail starts, never between a tool call and its result.

    Prefers keeping at least `keep` messages. Returns None when nothing precedes the tail (nothing to
    summarize). With `allow_empty=False` the tail is never empty (needed mid-run, where the last
    message is the pending request).
    """
    if len(history) < 2:
        return None
    start = max(len(history) - keep, 1)
    for i in range(start, 0, -1):
        if _is_boundary(history[i]):
            return i
    for i in range(start + 1, len(history)):
        if _is_boundary(history[i]):
            return i
    if allow_empty and len(history) > 1:
        return len(history)
    return None


def render_transcript(messages: list[ModelMessage], return_limit: int = RETURN_LIMIT) -> str:
    """Plain-text transcript; large old tool results are truncated (the cheap first stage)."""
    lines: list[str] = []
    for m in messages:
        for p in m.parts:
            if isinstance(p, UserPromptPart):
                content = p.content if isinstance(p.content, str) else " ".join(
                    c if isinstance(c, str) else "[attachment]" for c in p.content)
                lines.append(f"USER: {content}")
            elif isinstance(p, TextPart):
                lines.append(f"ASSISTANT: {p.content}")
            elif isinstance(p, ToolCallPart):
                lines.append(f"TOOL CALL {p.tool_name}({p.args_as_json_str()[:300]})")
            elif isinstance(p, (ToolReturnPart, RetryPromptPart)):
                text = str(p.content)
                if len(text) > return_limit:
                    text = f"{text[:return_limit]}… [{len(text) - return_limit} chars truncated]"
                lines.append(f"TOOL RESULT: {text}")
    return "\n".join(lines)


async def compact(model: Model, history: list[ModelMessage], *, keep: int = 6, focus: str = "",
                  todos: str = "", allow_empty: bool = True) -> list[ModelMessage] | None:
    """Return a shorter history: [summary, ack, *recent tail], or None if there is nothing to compact."""
    cut = split_point(history, keep, allow_empty=allow_empty)
    if cut is None:
        return None
    prompt = "Transcript to summarize:\n\n" + render_transcript(history[:cut])
    if todos:
        prompt += f"\n\nCurrent todo list:\n{todos}"
    if focus:
        prompt += f"\n\nPay particular attention to: {focus}"
    summary = (await Agent(model, instructions=SUMMARY_INSTRUCTIONS).run(prompt)).output
    head: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(SUMMARY_PREFIX + summary)]),
                                ModelResponse(parts=[TextPart(ACK)])]
    return head + history[cut:]
