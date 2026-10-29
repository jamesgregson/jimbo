from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from ..config import Settings


class OpenAICompatProvider:
    """Any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, OpenAI itself)."""

    def __init__(self, base_url_attr: str, default_base_url: str | None = None):
        self._attr = base_url_attr
        self._default = default_base_url

    def build_model(self, model_name: str, settings: Settings) -> Model:
        base_url = getattr(settings, self._attr, None) or self._default
        provider = OpenAIProvider(base_url=base_url, api_key=settings.api_key)
        ms: OpenAIChatModelSettings = {"timeout": settings.request_timeout}
        if settings.temperature is not None:
            ms["temperature"] = settings.temperature
        if settings.num_ctx is not None:
            ms["extra_body"] = {"options": {"num_ctx": settings.num_ctx}}
        return OpenAIChatModel(model_name, provider=provider, settings=ms)
