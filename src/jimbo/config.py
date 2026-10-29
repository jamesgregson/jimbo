from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Override with JIMBO_* env vars or CLI flags."""

    model_config = SettingsConfigDict(env_prefix="JIMBO_")

    # "<provider>:<model name>", e.g. "ollama:coder-qwen3.8-27b:latest"
    model: str = "ollama:coder-qwen3.6-27b:latest"
    ollama_base_url: str = "http://localhost:11434/v1"
    openai_base_url: str | None = None
    api_key: str = "unused"
    num_ctx: int | None = 90000  # best-effort; /v1 may ignore it, prefer baking into the Modelfile
    temperature: float | None = None
    request_timeout: float = 600.0

    workspace: Path = Path.cwd()
    ui: str = "plain"
    # Policy chain, evaluated in order; first non-"ask" verdict wins.
    approval: list[str] = ["readonly", "remembered", "ask"]
    request_limit: int = 50
    tool_retries: int = 3

    def split_model(self) -> tuple[str, str]:
        provider, _, name = self.model.partition(":")
        if not name:
            raise ValueError(f"model must look like '<provider>:<name>', got {self.model!r}")
        return provider, name
