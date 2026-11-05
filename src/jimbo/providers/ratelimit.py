"""Client-side request limits per provider, for endpoints that refuse bursts (e.g. OpenRouter's new accounts)."""

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

WINDOW = 60.0  # seconds


class RequestLimiter:
    """At most `per_minute` requests start in any 60 seconds."""

    def __init__(self, per_minute: int, clock=time.monotonic, sleep=asyncio.sleep):
        self.per_minute = per_minute
        self._sent: deque[float] = deque()
        self._clock, self._sleep = clock, sleep

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= WINDOW:
                self._sent.popleft()
            if len(self._sent) < self.per_minute:  # no await between the check and the append
                self._sent.append(now)
                return
            await self._sleep(WINDOW - (now - self._sent[0]))


_limiters: dict[str, RequestLimiter] = {}


def limiter_for(provider: str, per_minute: int) -> RequestLimiter:
    """The limiter for `provider`, shared by every model built for it in this process."""
    current = _limiters.get(provider)
    if current is None or current.per_minute != per_minute:
        current = _limiters[provider] = RequestLimiter(per_minute)
    return current


class RateLimitedModel(WrapperModel):
    """`wrapped`, waiting for `limiter` before each request."""

    def __init__(self, wrapped: Model, limiter: RequestLimiter):
        super().__init__(wrapped)
        self.limiter = limiter

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        await self.limiter.acquire()
        return await super().request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                             model_request_parameters: ModelRequestParameters,
                             run_context: Any = None) -> AsyncGenerator[StreamedResponse]:
        await self.limiter.acquire()
        async with super().request_stream(messages, model_settings, model_request_parameters,
                                          run_context) as stream:
            yield stream
