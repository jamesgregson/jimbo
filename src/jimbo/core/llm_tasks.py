"""One-shot, tool-less model calls for background tasks, each routed through the ModelRouter."""

import asyncio
from collections.abc import Callable
from typing import TypeVar

from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from .models import ModelRouter

T = TypeVar("T")


async def run_task(router: ModelRouter | None, task: str, prompt: str, *, instructions: str,
                   output_type: type[T] = str, timeout: float | None = None,
                   on_failure: Callable[[str], None] | None = None) -> T | None:
    """Run `prompt` on the model chosen for `task`. Returns None on any failure or timeout: these tasks
    are advisory, so callers carry on without them. `on_failure` gets a short reason. Cancellation still
    propagates."""
    if router is None:
        return None
    try:
        model = await router.aresolve(task)
        agent = Agent(model, instructions=instructions, output_type=output_type, retries=1)
        return (await asyncio.wait_for(agent.run(prompt), timeout)).output
    except Exception as e:
        if on_failure is not None:
            if isinstance(e, TimeoutError):
                why = f"timed out after {timeout:g}s"
            elif isinstance(e, UnexpectedModelBehavior):  # e.g. not the requested format, even after a retry
                why = f"unusable answer: {e.message}"
            else:
                why = f"{type(e).__name__}: {e}"
            on_failure(why[:200])
        return None
