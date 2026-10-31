import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator
from pydantic_settings import BaseSettings, InitSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

from .tools.sandbox import SandboxMode

# A cloned repo's .jimbo/config.toml must not be able to loosen approvals, the sandbox, or where
# code and credentials are sent. These keys are only read from ~/.jimbo/config.toml (or env/CLI).
UNTRUSTED_KEYS = {"model", "api_key", "ollama_base_url", "openai_base_url", "openrouter_base_url",
                  "workspace", "web_host", "approval", "approval_allow", "sandbox", "sandbox_writable",
                  "sandbox_network", "log_dir", "user_dir", "providers", "model_aliases", "models", "tasks"}


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from None


def _warn_loose_permissions(path: Path, config: dict) -> None:
    inline = "api_key" in config or any(isinstance(p, dict) and "api_key" in p
                                        for p in (config.get("providers") or {}).values())
    try:
        loose = path.stat().st_mode & 0o077
    except OSError:
        return
    if inline and loose:
        print(f"jimbo: {path} holds an API key but is readable by other users (chmod 600 it)", file=sys.stderr)


class ProviderConfig(BaseModel):
    """One model endpoint and its credentials. At most one key source may be set; keys are never logged."""

    model_config = ConfigDict(extra="forbid")

    type: str = "openai_compat"  # which adapter speaks to it (see providers/registry.py)
    base_url: str | None = None
    api_key: SecretStr | None = None  # inline in ~/.jimbo/config.toml (discouraged)
    api_key_env: str | None = None  # name of an environment variable
    api_key_file: Path | None = None  # file holding the key; must not be group/world readable
    api_key_cmd: list[str] | None = None  # argv (no shell) whose stdout is the key, e.g. a keychain lookup
    fallback_key_envs: list[str] = []  # env vars tried when no explicit source is set
    require_key: bool = False  # fail at build time instead of sending the dummy "unused" key
    send_num_ctx: bool = True  # Ollama-style options.num_ctx in the request body

    @model_validator(mode="after")
    def _one_source(self) -> "ProviderConfig":
        if len(self.key_sources()) > 1:
            raise ValueError(f"set at most one of api_key, api_key_env, api_key_file, api_key_cmd "
                             f"(got {', '.join(self.key_sources())})")
        if self.api_key_cmd is not None and not self.api_key_cmd:
            raise ValueError("api_key_cmd must be a non-empty list of arguments")
        return self

    def key_sources(self) -> list[str]:
        return [n for n in ("api_key", "api_key_env", "api_key_file", "api_key_cmd") if getattr(self, n) is not None]


class TaskConfig(BaseModel):
    """Per-task knobs, separate from which model runs the task (that is `Settings.models`)."""

    model_config = ConfigDict(extra="forbid")

    # off: never. auto: only when a model was chosen for the task. ask: when the user is about to be
    # asked. always: for every call, including ones a policy already allowed.
    mode: Literal["off", "auto", "ask", "always"] = "auto"
    timeout: float = 8.0  # seconds to wait for the model before carrying on without its answer


class Settings(BaseSettings):
    """Runtime configuration. Precedence: CLI flags > JIMBO_* env vars > <workspace>/.jimbo/config.toml >
    ~/.jimbo/config.toml > defaults."""

    model_config = SettingsConfigDict(env_prefix="JIMBO_")

    # "<provider>:<model name>", e.g. "ollama:coder-qwen3.8-27b:latest"
    model: str = "ollama:coder-qwen3.6-27b:latest"
    ollama_base_url: str = "http://localhost:11434/v1"
    openai_base_url: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = "unused"  # legacy: only used for the "openai" provider; prefer [providers.<name>]
    # Endpoints and credentials by provider name. Built-ins (ollama, openai, openrouter) can be
    # overridden here; any other name defines a new OpenAI-compatible endpoint.
    providers: dict[str, ProviderConfig] = {}
    # Short names for model specs, e.g. fast = "ollama:qwen3:4b". Skills and agents may only pick aliases.
    model_aliases: dict[str, str] = {}
    # Which model runs each task (an alias, "main", or a raw "<provider>:<name>"); unset tasks use `model`.
    models: dict[str, str] = {}
    tasks: dict[str, TaskConfig] = {}
    num_ctx: int | None = 90000  # best-effort; /v1 may ignore it, prefer baking into the Modelfile
    context_window: int | None = None  # tokens; falls back to num_ctx. Used for /usage and compaction
    compact_threshold: float = 0.8  # context fill ratio that triggers auto-compaction; 0 disables
    compact_keep: int = 6  # recent messages kept verbatim when compacting
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

        user_path = pick("user_dir", Path.home() / ".jimbo") / "config.toml"
        user = _read_toml(user_path)
        _warn_loose_permissions(user_path, user)
        project = _read_toml(pick("workspace", Path.cwd()) / ".jimbo" / "config.toml")
        if ignored := sorted(UNTRUSTED_KEYS & project.keys()):
            print(f"jimbo: ignoring {', '.join(ignored)} in workspace .jimbo/config.toml "
                  "(set them in ~/.jimbo/config.toml)", file=sys.stderr)
        merged = {**user, **{k: v for k, v in project.items() if k not in UNTRUSTED_KEYS}}
        if deny := [*user.get("approval_deny", []), *project.get("approval_deny", [])]:
            merged["approval_deny"] = deny  # project deny rules add to the user's, never replace them
        toml = InitSettingsSource(settings_cls, merged)
        return init_settings, env_settings, dotenv_settings, toml, file_secret_settings

    def task(self, name: str) -> TaskConfig:
        return self.tasks.get(name) or TaskConfig()
