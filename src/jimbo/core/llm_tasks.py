"""One-shot, tool-less model calls for background tasks, each routed through the ModelRouter."""

import asyncio
from collections.abc import Callable
from typing import TypeVar

from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from .models import ModelRouter

T = TypeVar("T")

REASON_LIMIT = 500  # characters of a failure reason; provider errors put the useful part at the end


async def run_task(router: ModelRouter | None, task: str, prompt: str, *, instructions: str,
                   output_type: type[T] = str, timeout: float | None = None,
                   on_failure: Callable[[str], None] | None = None) -> T | None:
    """Run `prompt` on the model chosen for `task`. Returns None on any failure or timeout: these tasks
    are advisory, so callers carry on without them. `on_failure` gets a short reason. Cancellation still
    propagates.

    Structured output is asked for as JSON in the text, not as a forced tool call: some providers reject
    a forced tool_choice, for example Qwen in thinking mode."""
    if router is None:
        return None
    try:
        model = await router.aresolve(task)
        output = output_type if output_type is str else PromptedOutput(output_type)
        agent = Agent(model, instructions=instructions, output_type=output, retries=1)
        return (await asyncio.wait_for(agent.run(prompt), timeout)).output
    except Exception as e:
        if on_failure is not None:
            on_failure(shorten(failure_reason(e, timeout)))
        return None


def failure_reason(e: Exception, timeout: float | None) -> str:
    if isinstance(e, TimeoutError):
        return f"timed out after {timeout:g}s"
    if isinstance(e, UnexpectedModelBehavior):  # e.g. not the requested format, even after a retry
        return f"unusable answer: {e.message}"
    if isinstance(e, ModelHTTPError):  # the body holds the provider's own message
        return f"{type(e).__name__}: HTTP {e.status_code}: {e.body}"
    return f"{type(e).__name__}: {e}"


def shorten(text: str, limit: int = REASON_LIMIT) -> str:
    """Cut `text` to `limit` characters, keeping its start and its end."""
    if len(text) <= limit:
        return text
    head = limit // 4
    return f"{text[:head]} … {text[-(limit - head - 3):]}"
