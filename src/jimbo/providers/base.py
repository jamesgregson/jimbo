from typing import Protocol

from pydantic_ai.models import Model

from ..config import ProviderConfig, Settings


class ProviderSpec(Protocol):
    """An adapter for one kind of endpoint. `name` is the provider name the model spec used."""

    def build_model(self, model_name: str, name: str, cfg: ProviderConfig, settings: Settings) -> Model: ...
