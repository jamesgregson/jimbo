import asyncio
import os
import tomllib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.config import ProviderConfig
from jimbo.config_edit import ConfigFile
from jimbo.core.runner import config_request
from jimbo.providers import credentials
from jimbo.providers.preflight import ModelFacts, list_models
from jimbo.wiring import build_session

SECRET = "sk-sentinel-456"


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """Keys go to files under the test's user_dir, never the developer's Keychain."""
    monkeypatch.setattr(credentials, "use_keychain", lambda: False)
    credentials.clear_cache()


def make(tmp_path, *, inspector=None, config: str | None = None, **kw):
    user = tmp_path / "user"
    if config is not None:
        user.mkdir(exist_ok=True)
        (user / "config.toml").write_text(config)
    kw.setdefault("model_aliases", {"main": "ollama:big"})
    fe = ScriptedFrontend()
    built: list[str] = []

    def builder(spec, st):
        if spec.startswith("nope:"):
            raise ValueError("unknown provider 'nope'")
        built.append(spec)
        return TestModel()

    s = build_session(settings(tmp_path, **kw), fe, model=TestModel(), model_builder=builder)
    s.config.inspector = inspector

    async def lister(provider, st):
        return ["a", "b"] if provider != "down" else "can't reach down"

    s.config.lister = lister
    s.built = built
    return s, fe


def saved(s) -> dict:
    return tomllib.loads(s.config.file.path.read_text())


def last(fe):
    return next(e for e in reversed(fe.events) if e.kind == "command_output").data


async def run(s, text):
    assert await s.commands.dispatch(s, text)


def answers(s, *values):
    queue = list(values)

    async def ask(message):
        return queue.pop(0)

    s.ask_secret = ask


# -- R1: writing the file ----------------------------------------------------------------------------

CONFIG = """\
# my settings
temperature = 0.2  # warm

[model_aliases]
main = "ollama:big"  # the one I talk to
"""


async def test_comments_and_order_survive_a_save(tmp_path):
    s, _ = make(tmp_path, config=CONFIG)
    await run(s, "/models alias fast ollama:small")
    text = s.config.file.path.read_text()
    assert text.startswith(CONFIG) and 'fast = "ollama:small"' in text


async def test_edits_made_since_startup_are_kept(tmp_path):
    s, _ = make(tmp_path, config=CONFIG)
    s.config.file.path.write_text(CONFIG + 'smart = "ollama:huge"\n')  # edited in an editor
    await run(s, "/models alias fast ollama:small")
    assert saved(s)["model_aliases"] == {"main": "ollama:big", "smart": "ollama:huge", "fast": "ollama:small"}


def test_an_invalid_result_is_not_written(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    f = ConfigFile(path)
    with pytest.raises(ValueError, match="not saved: providers.x: set at most one"):
        f.edit(lambda doc: doc.update({"providers": {"x": {"api_key_env": "A", "api_key_file": "/k"}}}))
    assert path.read_text() == CONFIG


def test_new_file_is_0600_and_mode_and_symlink_are_kept(tmp_path):
    f = ConfigFile(tmp_path / "new" / "config.toml")
    f.edit(lambda doc: doc.update({"models": {"x": "main"}}))
    assert f.path.stat().st_mode & 0o777 == 0o600
    real = tmp_path / "dotfiles.toml"
    real.write_text(CONFIG)
    real.chmod(0o640)
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    ConfigFile(link).edit(lambda doc: doc.update({"models": {"x": "main"}}))
    assert link.is_symlink() and "[models]" in real.read_text() and real.stat().st_mode & 0o777 == 0o640


# -- R2, R5: models ---------------------------------------------------------------------------------

async def test_model_save_checks_then_saves(tmp_path):
    async def inspector(spec, st):
        return ModelFacts("no such model") if spec == "ollama:bad" else ModelFacts(context=32000, vision=False)

    s, fe = make(tmp_path, inspector=inspector, model_aliases={"main": "ollama:big", "fast": "ollama:small"})
    await run(s, "/model ollama:bad --save")
    assert last(fe) == {"command": "model", "ok": False, "text": "no such model"}
    assert s.deps.models.explain("main").spec == "ollama:big" and not s.config.file.path.exists()
    await run(s, "/model fast --save")
    assert last(fe)["ok"] and saved(s)["model_aliases"]["main"] == "ollama:small"
    assert s.context_window == 32000 and s.vision is False
    assert any(e.kind == "config" and e.data["aliases"]["main"] == "ollama:small" for e in fe.events)


async def test_model_without_save_is_session_only_but_checked(tmp_path):
    async def inspector(spec, st):
        return ModelFacts(context=8000)

    s, fe = make(tmp_path, inspector=inspector)
    await run(s, "/model ollama:other")
    assert s.deps.models.explain("main").spec == "ollama:other" and s.context_window == 8000
    assert not s.config.file.path.exists()
    await run(s, "/model --save")  # saves the session's main model
    assert saved(s)["model_aliases"]["main"] == "ollama:other"


async def test_alias_add_and_remove(tmp_path):
    s, fe = make(tmp_path, config=CONFIG)
    await run(s, "/models alias fast fast")
    assert not last(fe)["ok"] and "provider" in last(fe)["text"]  # an alias can't point at an alias
    await run(s, "/models alias fast nope:x")
    assert not last(fe)["ok"] and "fast" not in saved(s)["model_aliases"]
    await run(s, "/models alias fast ollama:small")
    assert s.deps.models.explain_value("fast") == "ollama:small"
    await run(s, "/models set jimbo-compaction fast --save")
    await run(s, "/models alias fast --rm")
    assert not last(fe)["ok"] and "jimbo-compaction" in last(fe)["text"]
    await run(s, "/models reset jimbo-compaction --save")
    await run(s, "/models alias fast --rm")
    assert last(fe)["ok"] and "fast" not in saved(s)["model_aliases"] and "fast" not in s.config.settings.model_aliases
    await run(s, "/models alias main --rm")
    assert not last(fe)["ok"]


async def test_task_set_and_reset_save(tmp_path):
    s, fe = make(tmp_path, model_aliases={"main": "ollama:big", "fast": "ollama:small"})
    await run(s, "/models set jimbo-compaction fast --save")
    assert last(fe)["text"] == "jimbo-compaction: ollama:small [fast] (saved)"
    assert saved(s)["models"] == {"jimbo-compaction": "fast"}
    assert s.deps.models.explain("jimbo-compaction").source == "config"
    await run(s, "/models reset jimbo-compaction --save")
    assert "models" not in saved(s) and s.deps.models.explain("jimbo-compaction").source == "main"


# -- R2, R3, R5: providers ------------------------------------------------------------------------

async def test_add_provider_with_pasted_key(tmp_path):
    s, fe = make(tmp_path)
    answers(s, SECRET)
    await run(s, "/providers add lm http://localhost:1234/v1")
    assert last(fe)["text"] == "lm: saved\nlm: reachable, 2 models"
    key_path = tmp_path / "user" / "keys" / "lm"
    assert saved(s)["providers"]["lm"] == {"base_url": "http://localhost:1234/v1", "api_key_file": str(key_path)}
    assert key_path.read_text().strip() == SECRET and key_path.stat().st_mode & 0o777 == 0o600
    assert credentials.resolve_api_key("lm", s.config.settings.providers["lm"]).get_secret_value() == SECRET
    assert SECRET not in "".join(e.model_dump_json() for e in fe.events)
    assert "lm  " in s.config.describe_providers() and SECRET not in s.config.describe_providers()


async def test_key_forms(tmp_path):
    s, fe = make(tmp_path)
    await run(s, "/providers key openrouter $OR_KEY")
    assert saved(s)["providers"]["openrouter"] == {"api_key_env": "OR_KEY"}
    await run(s, f"/providers key openrouter {SECRET}")
    assert not last(fe)["ok"] and "hidden prompt" in last(fe)["text"]
    s.ask_secret = None
    await run(s, "/providers key openrouter")
    assert not last(fe)["ok"] and "$ENV_VAR" in last(fe)["text"]
    answers(s, SECRET)
    await run(s, "/providers key openrouter")
    assert set(saved(s)["providers"]["openrouter"]) == {"api_key_file"}  # the env source was replaced
    answers(s, "$OTHER")
    await run(s, "/providers key openrouter")
    assert saved(s)["providers"]["openrouter"] == {"api_key_env": "OTHER"}
    assert not (tmp_path / "user" / "keys" / "openrouter").exists()  # the stored key went with it


async def test_provider_url_change_drops_built_models(tmp_path):
    s, fe = make(tmp_path, model_aliases={"main": "ollama:big", "w": "work:m"},
                 providers={"work": ProviderConfig(base_url="http://old/v1")})
    await s.deps.models.abuild("work:m")
    s.built.clear()
    await run(s, "/providers url work http://new/v1")
    assert s.config.settings.providers["work"].base_url == "http://new/v1"
    await s.deps.models.abuild("work:m")
    assert s.built == ["work:m"]  # rebuilt with the new URL
    await run(s, "/providers url work ftp://x")
    assert not last(fe)["ok"]


async def test_remove_provider(tmp_path):
    s, fe = make(tmp_path, model_aliases={"main": "ollama:big"})
    await run(s, "/providers add work http://w/v1")
    await run(s, "/models alias w work:m")
    await run(s, "/providers rm work")
    assert not last(fe)["ok"] and "alias w" in last(fe)["text"]
    await run(s, "/models alias w --rm")
    await run(s, "/providers rm work")
    assert last(fe)["ok"] and "work" not in s.config.settings.providers and "providers" not in saved(s)
    await run(s, "/providers rm ollama")
    assert not last(fe)["ok"] and "no settings" in last(fe)["text"]
    await run(s, "/providers url ollama http://gpu:11434/v1")
    await run(s, "/providers rm ollama")
    assert last(fe)["text"] == "ollama: back to the built-in settings"


async def test_provider_test_and_names(tmp_path):
    s, fe = make(tmp_path, providers={"down": ProviderConfig(base_url="http://d/v1")})
    await run(s, "/providers test down")
    assert last(fe)["text"] == "can't reach down"
    await run(s, "/providers add bad:name http://x/v1")
    assert not last(fe)["ok"]
    await run(s, "/providers add ollama http://x/v1")
    assert "already exists" in last(fe)["text"]


# -- R6: listing models -------------------------------------------------------------------------------

@pytest.fixture
async def models_server():
    async def handler(request):
        if request.headers.get("Authorization") != "Bearer good":
            return web.json_response({}, status=401)
        return web.json_response({"data": [{"id": "z"}, {"id": "a"}, {"bad": 1}]})

    app = web.Application()
    app.router.add_get("/v1/models", handler)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url("/v1"))
    await server.close()


async def test_list_models(tmp_path, models_server):
    st = settings(tmp_path, model_aliases={"main": "ollama:x"},
                  providers={"good": ProviderConfig(base_url=models_server, api_key="good"),
                             "bad": ProviderConfig(base_url=models_server, api_key="nope"),
                             "gone": ProviderConfig(base_url="http://127.0.0.1:9/v1")})
    assert await list_models("good", st) == ["a", "z"]
    assert await list_models("bad", st) == "bad rejected the API key (HTTP 401)"
    assert "can't reach gone" in await list_models("gone", st)
    assert "unknown provider" in await list_models("nobody", st)


# -- R8: settings panel requests ---------------------------------------------------------------------

async def test_panel_requests(tmp_path):
    s, fe = make(tmp_path)
    await config_request(s, {"op": "provider_set", "new": True, "name": "lm", "base_url": "http://l/v1",
                             "key": SECRET})
    assert "lm" in saved(s)["providers"] and SECRET not in "".join(e.model_dump_json() for e in fe.events)
    await config_request(s, {"op": "task_set", "task": "jimbo-compaction", "value": "lm:x"})
    assert saved(s)["models"] == {"jimbo-compaction": "lm:x"}
    await config_request(s, {"op": "task_set", "task": "jimbo-compaction", "value": None})
    assert "models" not in saved(s)
    await config_request(s, {"op": "bogus"})
    assert not last(fe)["ok"]
    s._task = asyncio.get_running_loop().create_future()  # a run in progress
    await config_request(s, {"op": "alias_set", "name": "x", "spec": "lm:y"})
    assert "run is in progress" in last(fe)["text"] and "x" not in s.config.settings.model_aliases
    await config_request(s, {"op": "get"})  # reading is fine
    assert fe.events[-1].kind == "config"
    s._task.cancel()


async def test_snapshot_lists_agents_and_hides_keys(tmp_path):
    s, _ = make(tmp_path, providers={"work": ProviderConfig(base_url="http://w/v1", api_key=SECRET)})
    snap = s.config.snapshot(s)
    assert {"task": "jimbo-compaction", "spec": "ollama:big", "via": None, "source": "main", "value": None,
            "agent": False} in snap["tasks"]
    assert any(t["agent"] and t["task"] == "explore" for t in snap["tasks"])
    work = next(p for p in snap["providers"] if p["name"] == "work")
    assert work == {"name": "work", "type": "openai_compat", "base_url": "http://w/v1", "key": "inline",
                    "builtin": False, "changed": True}
    assert SECRET not in str(snap)


def test_keychain_source_is_described():
    cfg = ProviderConfig(api_key_cmd=credentials.keychain_cmd("openrouter"))
    assert credentials.describe_key_source(cfg) == "keychain jimbo-openrouter"


async def test_run_frontend_binds_config_controls(tmp_path):
    from jimbo.core.runner import run_frontend

    s, fe = make(tmp_path)
    bound = {}
    fe.bind_config = lambda f: bound.setdefault("config", f)
    fe.bind_model_list = lambda f: bound.setdefault("models", f)

    async def ask_secret(message):
        return None

    fe.ask_secret = ask_secret
    await run_frontend(s, fe)
    assert any(e.kind == "config" for e in fe.events)
    assert set(bound) == {"config", "models"} and s.ask_secret is ask_secret
    assert await bound["models"]("ollama") == ["a", "b"]
