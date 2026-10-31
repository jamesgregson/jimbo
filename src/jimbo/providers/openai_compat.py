from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from ..config import ProviderConfig, Settings
from .credentials import resolve_api_key


class OpenAICompatProvider:
    """Any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, OpenAI itself)."""

    def build_model(self, model_name: str, name: str, cfg: ProviderConfig, settings: Settings) -> Model:
        key = resolve_api_key(name, cfg)
        provider = OpenAIProvider(base_url=cfg.base_url, api_key=key.get_secret_value() if key else "unused")
        ms: OpenAIChatModelSettings = {"timeout": settings.request_timeout}
        if settings.temperature is not None:
            ms["temperature"] = settings.temperature
        if cfg.send_num_ctx and settings.num_ctx is not None:
            ms["extra_body"] = {"options": {"num_ctx": settings.num_ctx}}
        return OpenAIChatModel(model_name, provider=provider, settings=ms)
