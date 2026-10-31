import os
import sys
import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings, InitSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

from .tools.sandbox import SandboxMode

# A cloned repo's .jimbo/config.toml must not be able to loosen approvals, the sandbox, or where
# code and credentials are sent. These keys are only read from ~/.jimbo/config.toml (or env/CLI).
UNTRUSTED_KEYS = {"model", "api_key", "ollama_base_url", "openai_base_url", "openrouter_base_url",
                  "workspace", "web_host", "approval", "approval_allow", "sandbox", "sandbox_writable",
                  "sandbox_network", "log_dir", "user_dir"}


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from None


class Settings(BaseSettings):
    """Runtime configuration. Precedence: CLI flags > JIMBO_* env vars > <workspace>/.jimbo/config.toml >
    ~/.jimbo/config.toml > defaults."""

    model_config = SettingsConfigDict(env_prefix="JIMBO_")

    # "<provider>:<model name>", e.g. "ollama:coder-qwen3.8-27b:latest"
    model: str = "ollama:coder-qwen3.6-27b:latest"
    ollama_base_url: str = "http://localhost:11434/v1"
    openai_base_url: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = "unused"
    num_ctx: int | None = 90000  # best-effort; /v1 may ignore it, prefer baking into the Modelfile
    context_window: int | None = None  # tokens; falls back to num_ctx. Used for /usage and compaction
    vision: bool = True  # False for text-only models: pasted images are dropped with a warning
    temperature: float | None = None
    request_timeout: float = 600.0

    workspace: Path = Path.cwd()
    ui: str = "plain"
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_open_browser: bool = True
    # Policy chain, evaluated in order; first non-"ask" verdict wins.
    approval: list[str] = ["rules", "readonly", "remembered", "ask"]
    # "tool" or "tool:<glob on first arg>", e.g. "shell:git status*", "read_file:.env*". Deny wins.
    approval_allow: list[str] = []
    approval_deny: list[str] = []
    request_limit: int = 50
    log_sessions: bool = True
    log_dir: Path = Path.home() / ".jimbo" / "sessions"
    user_dir: Path = Path.home() / ".jimbo"  # config.toml, commands/ (and later skills/)
    tool_retries: int = 3

    shell_timeout: int = 120  # seconds, default per command; the model may ask for up to shell_timeout_max
    shell_timeout_max: int = 600
    sandbox: SandboxMode = "auto"  # auto: sandbox-exec when available (macOS); on: refuse to run without it
    sandbox_writable: list[Path] = []  # extra writable paths besides workspace, temp and cache dirs
    sandbox_network: bool = True

    @classmethod
    def settings_customise_sources(cls, settings_cls: type[BaseSettings], init_settings: InitSettingsSource,
                                   env_settings: PydanticBaseSettingsSource,
                                   dotenv_settings: PydanticBaseSettingsSource,
                                   file_secret_settings: PydanticBaseSettingsSource):
        def pick(name: str, default: Path) -> Path:
            return Path(init_settings.init_kwargs.get(name) or os.environ.get(f"JIMBO_{name.upper()}") or default)

        user = _read_toml(pick("user_dir", Path.home() / ".jimbo") / "config.toml")
        project = _read_toml(pick("workspace", Path.cwd()) / ".jimbo" / "config.toml")
        if ignored := sorted(UNTRUSTED_KEYS & project.keys()):
            print(f"jimbo: ignoring {', '.join(ignored)} in workspace .jimbo/config.toml "
                  "(set them in ~/.jimbo/config.toml)", file=sys.stderr)
        merged = {**user, **{k: v for k, v in project.items() if k not in UNTRUSTED_KEYS}}
        if deny := [*user.get("approval_deny", []), *project.get("approval_deny", [])]:
            merged["approval_deny"] = deny  # project deny rules add to the user's, never replace them
        toml = InitSettingsSource(settings_cls, merged)
        return init_settings, env_settings, dotenv_settings, toml, file_secret_settings

    def split_model(self) -> tuple[str, str]:
        provider, _, name = self.model.partition(":")
        if not name:
            raise ValueError(f"model must look like '<provider>:<name>', got {self.model!r}")
        return provider, name
