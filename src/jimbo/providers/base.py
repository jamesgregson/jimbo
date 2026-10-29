from typing import Protocol

from pydantic_ai.models import Model

from ..config import Settings


class ProviderSpec(Protocol):
    def build_model(self, model_name: str, settings: Settings) -> Model: ...
