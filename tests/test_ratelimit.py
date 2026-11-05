import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from jimbo.config import ProviderConfig, Settings
from jimbo.providers.ratelimit import RateLimitedModel, RequestLimiter
from jimbo.providers.registry import build_model_from_spec


class FakeTime:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


async def test_limiter_waits_for_the_oldest_request_to_leave_the_window():
    t = FakeTime()
    limiter = RequestLimiter(2, clock=t.clock, sleep=t.sleep)
    await limiter.acquire()
    t.now = 10.0
    await limiter.acquire()
    assert t.slept == []
    await limiter.acquire()  # third in the minute: waits until the first is 60s old
    assert t.slept == [50.0] and t.now == 60.0
    await limiter.acquire()  # the second request leaves the window at 70s
    assert t.slept == [50.0, 10.0] and t.now == 70.0


async def test_wrapped_model_waits_before_each_request():
    t = FakeTime()
    model = RateLimitedModel(TestModel(), RequestLimiter(1, clock=t.clock, sleep=t.sleep))
    await Agent(model).run("one")
    await Agent(model).run("two")
    assert t.slept == [60.0]
    async with Agent(model).run_stream("three") as run:
        await run.get_output()
    assert t.slept == [60.0, 60.0]


def test_models_of_one_provider_share_a_limiter():
    st = Settings(providers={"work": ProviderConfig(base_url="http://x/v1", requests_per_minute=20)})
    a, b = build_model_from_spec("work:a", st), build_model_from_spec("work:b", st)
    assert isinstance(a, RateLimitedModel) and a.limiter is b.limiter and a.limiter.per_minute == 20
    assert not isinstance(build_model_from_spec("ollama:m", st), RateLimitedModel)


def test_limit_must_be_positive():
    with pytest.raises(ValidationError):
        ProviderConfig(requests_per_minute=0)
