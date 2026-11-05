import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, InitSettingsSource, PydanticBaseSettingsSource

from .tools.sandbox import SandboxMode

# The only setting read from the environment: tests use it to stay away from the real ~/.jimbo.
USER_DIR_ENV = "JIMBO_USER_DIR"

# Agents, skills and commands shipped with jimbo, laid out like .jimbo/. Any user file of the same name wins.
BUNDLED_DIR = Path(__file__).parent / "bundled"

# A cloned repo's .jimbo/config.toml must not be able to loosen approvals, the sandbox, or where
# code and credentials are sent. These keys are only read from ~/.jimbo/config.toml (or env/CLI).
UNTRUSTED_KEYS = {"model", "api_key", "ollama_base_url", "openai_base_url", "openrouter_base_url",
                  "workspace", "web_host", "approval", "approval_allow", "sandbox", "sandbox_writable",
                  "sandbox_network", "log_dir", "user_dir", "providers", "model_aliases", "models", "tasks",
                  "secret_files"}
RENAMED_TASKS = {"shell_assess": "jimbo-shell-assess", "compaction": "jimbo-compaction"}


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
    send_num_ctx: bool = False  # Ollama-style options.num_ctx in the request body; other APIs may reject it
    requests_per_minute: int | None = Field(None, gt=0)  # client-side cap, shared by every model of this provider

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

    # off: never. ask: when the user is about to be asked. always: for every call, including ones a
    # policy already allowed.
    mode: Literal["off", "ask", "always"] = "ask"
    timeout: float = 30.0  # seconds to wait for the model before carrying on without its answer


class Settings(BaseSettings):
    """Runtime configuration. Precedence: CLI flags > USER_DIR_ENV > <workspace>/.jimbo/config.toml >
    ~/.jimbo/config.toml > defaults."""

    ollama_base_url: str = "http://localhost:11434/v1"
    openai_base_url: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = "unused"  # legacy: only used for the "openai" provider; prefer [providers.<name>]
    # Endpoints and credentials by provider name. Built-ins (ollama, openai, openrouter) can be
    # overridden here; any other name defines a new OpenAI-compatible endpoint.
    providers: dict[str, ProviderConfig] = {}
    # Short names for "<provider>:<model name>" specs, e.g. fast = "ollama:qwen3:4b". `main` is required: it
    # is the model you talk to and the default for every other task. Skills and agents may only pick aliases.
    model_aliases: dict[str, str] = {}
    # Which model runs each task (an alias or a raw "<provider>:<name>"); unset tasks use `main`.
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
    ui: str = "auto"  # auto: tui for an interactive session in a terminal, otherwise plain
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_open_browser: bool = True
    # Policy chain, evaluated in order; first non-"ask" verdict wins.
    approval: list[str] = ["rules", "readonly", "remembered", "ask"]
    # "tool" or "tool:<glob on first arg>", e.g. "shell:git status*", "read_file:.env*". Deny wins.
    approval_allow: list[str] = []
    approval_deny: list[str] = []
    # File names the model can't read or grep, even with --yolo. Shell can still reach them, with approval.
    secret_files: list[str] = [".env", ".env.local", ".env.*.local", ".env.development", ".env.production",
                               ".env.staging", ".env.test", "*.pem", "*.key", "id_rsa", "id_ed25519"]
    request_limit: int = 50
    log_sessions: bool = True
    log_dir: Path = Path.home() / ".jimbo" / "sessions"
    user_dir: Path = Path.home() / ".jimbo"  # config.toml, commands/ (and later skills/)
    bundled: bool = True  # False: load nothing from BUNDLED_DIR. A repo may set this; it only removes files
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
        init = init_settings.init_kwargs
        env = {"user_dir": v} if (v := os.environ.get(USER_DIR_ENV)) else {}
        user_path = Path(init.get("user_dir") or env.get("user_dir") or Path.home() / ".jimbo") / "config.toml"
        user = _read_toml(user_path)
        if "model" in user:
            raise ValueError(f'{user_path}: `model` was replaced by main = "..." under [model_aliases]')
        for old in sorted(RENAMED_TASKS.keys() & {*user.get("models", {}), *user.get("tasks", {})}):
            raise ValueError(f"{user_path}: task `{old}` was renamed to `{RENAMED_TASKS[old]}`")
        _warn_loose_permissions(user_path, user)
        project = _read_toml(Path(init.get("workspace") or Path.cwd()) / ".jimbo" / "config.toml")
        if ignored := sorted(UNTRUSTED_KEYS & project.keys()):
            print(f"jimbo: ignoring {', '.join(ignored)} in workspace .jimbo/config.toml "
                  "(set them in ~/.jimbo/config.toml)", file=sys.stderr)
        merged = {**user, **{k: v for k, v in project.items() if k not in UNTRUSTED_KEYS}}
        if deny := [*user.get("approval_deny", []), *project.get("approval_deny", [])]:
            merged["approval_deny"] = deny  # project deny rules add to the user's, never replace them
        return init_settings, InitSettingsSource(settings_cls, env), InitSettingsSource(settings_cls, merged)

    def task(self, name: str) -> TaskConfig:
        return self.tasks.get(name) or TaskConfig()
