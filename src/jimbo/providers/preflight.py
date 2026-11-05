"""Startup check of the main model: catch what would otherwise fail on the first prompt, and learn what the
server knows about the model. Anything inconclusive (a slow server, no /models endpoint) lets jimbo start."""

import asyncio
import re
from dataclasses import dataclass, field

import aiohttp
from pydantic import SecretStr

from ..config import ProviderConfig, Settings
from .credentials import resolve_api_key
from .registry import provider_config, split_spec

TIMEOUT = 3.0  # seconds
LIST_TIMEOUT = 10.0  # seconds; some providers list hundreds of models
OPENAI_URL = "https://api.openai.com/v1"
OLLAMA_DEFAULT_CONTEXT = 4096  # what Ollama usually runs a model at without a num_ctx in its Modelfile
_NUM_CTX = re.compile(r"^num_ctx\s+(\d+)", re.M)


@dataclass
class ModelFacts:
    problem: str | None = None  # jimbo can't work with this model
    notes: list[str] = field(default_factory=list)  # worth telling the user
    context: int | None = None  # the context window it really has
    vision: bool | None = None
    prices: dict[str, float] | None = None  # USD per token, as OpenRouter's /models lists them


def _prices(raw: object) -> dict[str, float] | None:
    """Per-token prices from a /models entry's `pricing` (OpenRouter's format: numbers as strings)."""
    if not isinstance(raw, dict):
        return None
    prices = {}
    for k, v in raw.items():
        try:
            prices[k] = float(v)
        except (TypeError, ValueError):
            pass
    return prices if "prompt" in prices and "completion" in prices else None


async def _ollama(http: aiohttp.ClientSession, base: str, name: str) -> ModelFacts:
    root = base.removesuffix("/v1")
    try:
        async with http.post(f"{root}/api/show", json={"model": name}) as r:
            if r.status == 404:
                return ModelFacts(f"Ollama doesn't have model {name!r}: run `ollama pull {name}`, or choose another model")
            if r.status != 200:
                return ModelFacts()
            info = await r.json()
        async with http.get(f"{root}/api/ps") as r:
            loaded = (await r.json()).get("models", []) if r.status == 200 else []
    except aiohttp.ClientConnectorError:
        return ModelFacts(f"can't reach Ollama at {root}: is it running? (`ollama serve`)")
    caps = info.get("capabilities")
    if caps is not None and "tools" not in caps:
        return ModelFacts(f"Ollama model {name!r} doesn't support tool calling: choose another model")
    facts = ModelFacts(vision="vision" in caps if caps is not None else None)
    tagged = name if ":" in name else f"{name}:latest"
    running = next((m.get("context_length") for m in loaded if m.get("name") == tagged), None)
    baked = _NUM_CTX.search(info.get("parameters") or "")
    if running or baked:
        facts.context = running or int(baked.group(1))
    else:
        facts.context = OLLAMA_DEFAULT_CONTEXT
        facts.notes.append(f"{name} has no num_ctx in its Modelfile, so assuming Ollama runs it at "
                           f"{OLLAMA_DEFAULT_CONTEXT} tokens. That's too small for real work: see "
                           '"Give Ollama more context" in the README')
    return facts


async def _model_list(http: aiohttp.ClientSession, base: str, provider: str) -> list | str | None:
    """The provider's /models data. A string is a problem jimbo can't work around; None is inconclusive."""
    try:
        async with http.get(f"{base}/models") as r:
            if r.status in (401, 403):
                return f"{provider} rejected the API key (HTTP {r.status})"
            if r.status != 200:
                return None
            body = await r.json(content_type=None)
    except aiohttp.ClientConnectorError:
        return f"can't reach {provider} at {base}"
    except ValueError:  # not JSON: this endpoint doesn't really implement /models
        return None
    models = body.get("data") if isinstance(body, dict) else None
    return models if isinstance(models, list) else None


async def _openai_compat(http: aiohttp.ClientSession, base: str, provider: str, name: str) -> ModelFacts:
    models = await _model_list(http, base, provider)
    if models is None:
        return ModelFacts()
    if isinstance(models, str):
        return ModelFacts(models)
    found = next((m for m in models if isinstance(m, dict) and m.get("id") == name), None)
    if found is None:
        # a warning only: gateways often serve models they don't list
        return ModelFacts(notes=[f"{provider} doesn't list model {name!r}"] if models else [])
    return ModelFacts(context=found.get("context_length"), prices=_prices(found.get("pricing")))


def _client(key: SecretStr | None, timeout: float) -> aiohttp.ClientSession:
    headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=headers)


async def inspect_model(spec: str, settings: Settings) -> ModelFacts:
    provider, name = split_spec(spec)
    cfg: ProviderConfig = provider_config(provider, settings)
    if cfg.type != "openai_compat":
        return ModelFacts()
    base = (cfg.base_url or OPENAI_URL).rstrip("/")
    async with _client(resolve_api_key(provider, cfg), TIMEOUT) as http:
        try:
            if provider == "ollama":
                return await _ollama(http, base, name)
            return await _openai_compat(http, base, provider, name)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ModelFacts()


async def list_models(provider: str, settings: Settings) -> list[str] | str:
    """The ids of the models `provider` serves, or why they couldn't be listed."""
    try:
        cfg = provider_config(provider, settings)
        if cfg.type != "openai_compat":
            return f"can't list models for provider type {cfg.type!r}"
        base = (cfg.base_url or OPENAI_URL).rstrip("/")
        key = await asyncio.to_thread(resolve_api_key, provider, cfg)  # may run a key command
    except ValueError as e:
        return str(e)
    async with _client(key, LIST_TIMEOUT) as http:
        try:
            models = await _model_list(http, base, provider)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return f"no answer from {provider} at {base}"
    if models is None:
        return f"{provider} at {base} doesn't list its models"
    if isinstance(models, str):
        return models
    return sorted({m["id"] for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)})
