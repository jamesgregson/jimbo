from pydantic import SecretStr
from pydantic_ai.models import Model

from ..config import ProviderConfig, Settings
from .base import ProviderSpec
from .openai_compat import OpenAICompatProvider

ADAPTERS: dict[str, ProviderSpec] = {"openai_compat": OpenAICompatProvider()}


def builtin_providers(settings: Settings) -> dict[str, ProviderConfig]:
    """The always-available providers. The legacy `*_base_url` / `api_key` settings feed these."""
    legacy_key = SecretStr(settings.api_key) if settings.api_key not in ("", "unused") else None
    return {
        "ollama": ProviderConfig(base_url=settings.ollama_base_url, send_num_ctx=True),
        "openai": ProviderConfig(base_url=settings.openai_base_url, api_key=legacy_key,
                                 fallback_key_envs=["OPENAI_API_KEY"], require_key=True),
        "openrouter": ProviderConfig(base_url=settings.openrouter_base_url,
                                     fallback_key_envs=["OPENROUTER_API_KEY"], require_key=True),
    }


def provider_config(name: str, settings: Settings) -> ProviderConfig:
    """Built-in defaults overlaid with the user's `[providers.<name>]`; unknown names raise ValueError."""
    base = builtin_providers(settings).get(name)
    user = settings.providers.get(name)
    if user is None:
        if base is None:
            raise ValueError(f"unknown provider {name!r}; known: {sorted({*builtin_providers(settings), *settings.providers})}")
        return base
    if base is None:
        return user
    if user.key_sources():  # an explicit source replaces the legacy global key
        base = base.model_copy(update={"api_key": None})
    return base.model_copy(update=user.model_dump(exclude_unset=True) | (
        {"api_key": user.api_key} if user.api_key is not None else {}))


def all_providers(settings: Settings) -> dict[str, ProviderConfig]:
    return {n: provider_config(n, settings) for n in sorted({*builtin_providers(settings), *settings.providers})}


def split_spec(spec: str) -> tuple[str, str]:
    provider, _, name = spec.partition(":")
    if not provider or not name:
        raise ValueError(f"model must look like '<provider>:<name>', got {spec!r}")
    return provider, name


def build_model_from_spec(spec: str, settings: Settings) -> Model:
    provider, name = split_spec(spec)
    cfg = provider_config(provider, settings)
    try:
        adapter = ADAPTERS[cfg.type]
    except KeyError:
        raise ValueError(f"provider {provider!r}: unknown type {cfg.type!r}; known: {sorted(ADAPTERS)}") from None
    return adapter.build_model(name, provider, cfg, settings)
