from pydantic_ai.models import Model

from ..config import Settings
from .base import ProviderSpec
from .openai_compat import OpenAICompatProvider

PROVIDERS: dict[str, ProviderSpec] = {
    "ollama": OpenAICompatProvider("ollama_base_url"),
    "openai": OpenAICompatProvider("openai_base_url"),
}


def build_model(settings: Settings) -> Model:
    provider, name = settings.split_model()
    try:
        spec = PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider {provider!r}; known: {sorted(PROVIDERS)}") from None
    return spec.build_model(name, settings)
