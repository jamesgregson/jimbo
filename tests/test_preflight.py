import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from helpers import settings
from jimbo.config import ProviderConfig
from jimbo.providers.preflight import OLLAMA_DEFAULT_CONTEXT, inspect_model


async def serve(routes):
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    server = TestServer(app)
    await server.start_server()
    return server


def replying(status=200, **body):
    async def handler(request):
        return web.json_response(body, status=status)
    return handler


async def ollama(tmp_path, show, ps=None, model="ollama:m:8b"):
    server = await serve([("POST", "/api/show", show), ("GET", "/api/ps", ps or replying(models=[]))])
    try:
        return await inspect_model(model, settings(tmp_path, ollama_base_url=str(server.make_url("/v1"))))
    finally:
        await server.close()


async def hosted(tmp_path, models, spec="work:m"):
    server = await serve([("GET", "/v1/models", models)])
    try:
        st = settings(tmp_path, providers={"work": ProviderConfig(base_url=str(server.make_url("/v1")))})
        return await inspect_model(spec, st)
    finally:
        await server.close()


async def test_ollama_missing_model_says_how_to_get_it(tmp_path):
    facts = await ollama(tmp_path, replying(404, error="not found"))
    assert "ollama pull m:8b" in facts.problem


async def test_ollama_model_without_tools_is_refused(tmp_path):
    facts = await ollama(tmp_path, replying(capabilities=["completion"]))
    assert "tool calling" in facts.problem


async def test_ollama_context_comes_from_the_modelfile(tmp_path):
    facts = await ollama(tmp_path, replying(capabilities=["completion", "tools", "vision"],
                                            parameters='stop "x"\nnum_ctx 32768\n'))
    assert facts.problem is None and facts.context == 32768 and facts.vision is True and not facts.notes


async def test_ollama_loaded_model_reports_its_real_context(tmp_path):
    facts = await ollama(tmp_path, replying(capabilities=["tools"], parameters="num_ctx 32768"),
                         replying(models=[{"name": "m:8b", "context_length": 8192}]))
    assert facts.context == 8192 and facts.vision is False


async def test_ollama_without_num_ctx_warns(tmp_path):
    facts = await ollama(tmp_path, replying(capabilities=["tools"]))
    assert facts.context == OLLAMA_DEFAULT_CONTEXT and "Give Ollama more context" in facts.notes[0]


async def test_unreachable_server_is_a_problem(tmp_path):
    st = settings(tmp_path, ollama_base_url="http://127.0.0.1:9/v1",
                  providers={"work": ProviderConfig(base_url="http://127.0.0.1:9/v1")})
    assert "is it running" in (await inspect_model("ollama:m", st)).problem
    assert "can't reach work" in (await inspect_model("work:m", st)).problem


async def test_rejected_key_is_a_problem(tmp_path):
    assert "rejected the API key" in (await hosted(tmp_path, replying(401))).problem


async def test_hosted_context_and_unlisted_model(tmp_path):
    listing = replying(data=[{"id": "m", "context_length": 200000,
                              "pricing": {"prompt": "0.00000015", "completion": "0.00000047", "discount": 0}}])
    facts = await hosted(tmp_path, listing)
    assert facts.context == 200000
    assert facts.prices == {"prompt": 1.5e-7, "completion": 4.7e-7, "discount": 0.0}
    facts = await hosted(tmp_path, listing, "work:typo")
    assert facts.problem is None and "doesn't list" in facts.notes[0]


@pytest.mark.parametrize("handler", [replying(404), replying(500), replying(data="nonsense")])
async def test_inconclusive_answers_let_jimbo_start(tmp_path, handler):
    facts = await hosted(tmp_path, handler)
    assert facts.problem is None and not facts.notes
