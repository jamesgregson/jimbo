from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from jimbo.config import Settings
from jimbo.core.llm_tasks import REASON_LIMIT, run_task, shorten
from jimbo.core.models import ModelRouter


class Verdict(BaseModel):
    passed: bool


def router(run):
    return ModelRouter(Settings(model_aliases={"main": "t:main"}), lambda spec, s: FunctionModel(run))


async def test_structured_output_is_asked_for_as_text():
    seen = []

    async def run(messages, info):
        seen.append((info.allow_text_output, info.output_tools))
        return ModelResponse(parts=[TextPart('{"passed": true}')])

    out = await run_task(router(run), "evals-judge", "grade", instructions="", output_type=Verdict)
    assert out == Verdict(passed=True)
    assert seen == [(True, [])]  # no output tool for the provider to force


async def test_http_failure_keeps_the_provider_message():
    async def run(messages, info):
        raise ModelHTTPError(400, "m", {"code": 400, "metadata": {"raw": "x" * 600 + " tool_choice not allowed"}})

    failed = []
    out = await run_task(router(run), "evals-judge", "grade", instructions="", output_type=Verdict,
                         on_failure=failed.append)
    assert out is None
    assert failed[0].startswith("ModelHTTPError: HTTP 400:")
    assert "tool_choice not allowed" in failed[0] and len(failed[0]) <= REASON_LIMIT


def test_shorten_keeps_both_ends():
    assert shorten("short") == "short"
    text = "a" * 100 + "b" * 1000 + "c" * 100
    cut = shorten(text, 100)
    assert len(cut) == 100 and cut.startswith("a" * 25) and cut.endswith("c" * 72)
