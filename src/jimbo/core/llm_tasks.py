"""One-shot, tool-less model calls for background tasks, each routed through the ModelRouter."""

import asyncio
from typing import TypeVar

from pydantic_ai import Agent

from .models import ModelRouter

T = TypeVar("T")


async def run_task(router: ModelRouter | None, task: str, prompt: str, *, instructions: str,
                   output_type: type[T] = str, timeout: float | None = None) -> T | None:
    """Run `prompt` on the model chosen for `task`. Returns None on any failure or timeout: these tasks
    are advisory, so callers carry on without them. Cancellation still propagates."""
    if router is None:
        return None
    try:
        model = await router.aresolve(task)
        agent = Agent(model, instructions=instructions, output_type=output_type, retries=1)
        return (await asyncio.wait_for(agent.run(prompt), timeout)).output
    except Exception:
        return None
