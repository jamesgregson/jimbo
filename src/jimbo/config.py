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
                  "sandbox_network", "sandbox_image", "sandbox_readonly", "sandbox_ports", "sandbox_env",
                  "sandbox_memory", "sandbox_cpus", "log_dir", "user_dir", "scratch_keep_days", "providers",
                  "model_aliases", "models", "tasks", "secret_files", "lsp_servers", "git_write",
                  "sandbox_readable", "sandbox_system_temp", "secret_files_shell", "shell_env_keep", "read_paths",
                  "protected_write"}
ALWAYS_PROTECTED = [".git/hooks/**", ".git/config"]  # in protected_paths whatever the config says
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


class ServerConfig(BaseModel):
    """A language server. With the name of a built-in server, unset fields keep the built-in's values."""

    model_config = ConfigDict(extra="forbid")

    command: list[str] | None = None  # argv, no shell
    extensions: list[str] | None = None  # e.g. [".rb"]
    language_id: str | None = None  # sent in didOpen; default: the extension without the dot
    env: dict[str, str] = {}  # added to the environment
    init_options: dict = {}  # sent as initializationOptions
    settings: dict = {}  # returned for workspace/configuration requests


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
    # How hard the model thinks before it answers, sent as the reasoning effort. Unset: the model's default.
    # Whether a server honours it depends on the server and the model.
    thinking: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
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
    # False: shell commands may only read git. True: they may change the local repository, asking each time.
    # Changing a remote (git push, gh pr create, ...) is always denied.
    git_write: bool = False
    # File names the model can't read or grep, even with --yolo. Shell can still reach them, with approval.
    secret_files: list[str] = [".env", ".env.local", ".env.*.local", ".env.development", ".env.production",
                               ".env.staging", ".env.test", "*.pem", "*.key", "id_rsa", "id_ed25519", "id_ecdsa",
                               "id_dsa", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", ".netrc", ".npmrc",
                               ".pypirc", ".git-credentials", "credentials.json", "service-account*.json",
                               "*.tfstate", "*.tfstate.backup", ".htpasswd", "kubeconfig"]
    request_limit: int = 50
    log_sessions: bool = True
    log_dir: Path = Path.home() / ".jimbo" / "sessions"
    user_dir: Path = Path.home() / ".jimbo"  # config.toml, commands/ (and later skills/)
    scratch_keep_days: float = 7  # session scratch folders unchanged for this long are deleted at startup
    bundled: bool = True  # False: load nothing from BUNDLED_DIR. A repo may set this; it only removes files
    tool_retries: int = 3

    lsp: bool = True  # False: start no language servers and hide the lsp_* tools
    lsp_disabled: list[str] = []  # server names not to use; a workspace config adds to the user's list
    lsp_diagnostics: bool = True  # write_file and edit_file report the errors an edit introduced
    lsp_wait: float = 3.0  # seconds to wait for a server's errors after an edit
    lsp_timeout: float = 60.0  # seconds to wait for a server's answer to one request
    lsp_servers: dict[str, ServerConfig] = {}  # name -> server; replaces a built-in server of the same name

    shell_timeout: int = 120  # seconds, default per command; the model may ask for up to shell_timeout_max
    shell_timeout_max: int = 600
    # auto: sandbox-exec when available (macOS); on: refuse to run without it; docker, podman: a container
    sandbox: SandboxMode = "auto"
    sandbox_writable: list[Path] = []  # extra writable paths besides workspace, scratch and cache dirs
    # seatbelt: extra folders in the home folder commands may read. The rest of it is unreadable, except tool folders.
    sandbox_readable: list[Path] = []
    sandbox_system_temp: bool = False  # seatbelt: True lets commands write /tmp and the macOS temp folder again
    read_paths: list[Path] = []  # extra read-only folders for read_file, grep, glob and list_dir, e.g. library source
    shell_env_keep: list[str] = []  # variables passed to commands even though their names look secret
    # Files that run code when the project is used later. File tools and sandboxed commands can't change them. A
    # workspace config adds to the list; ALWAYS_PROTECTED can't be removed.
    protected_paths: list[str] = [".git/hooks/**", ".git/config", ".github/**", ".gitlab-ci.yml", ".vscode/**",
                                  ".idea/**", ".envrc", ".husky/**", ".claude/settings*.json"]
    # deny: file tools can't change protected_paths. ask: they can, asking each time in every mode. Commands can't
    # write them in a sandbox either way.
    protected_write: Literal["deny", "ask"] = "deny"
    secret_files_shell: bool = False  # True: commands can't read secret_files either (seatbelt; masked in containers)
    sandbox_network: bool = True
    sandbox_image: str = ""  # docker, podman: empty means the bundled image, built on first use
    sandbox_readonly: list[Path] = []  # docker, podman: extra paths mounted read-only
    sandbox_ports: list[int | str] = []  # docker, podman: forwards opened when the container starts, 5173 or "8080:5173"
    sandbox_env: dict[str, str] = {}  # docker, podman: environment variables set in the container
    sandbox_memory: str = "8g"  # container --memory; "" means no limit
    sandbox_cpus: float = 0  # container --cpus; 0 means no limit

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
        if "protected_paths" in project:  # a workspace adds protected paths, never removes them
            base = user.get("protected_paths", cls.model_fields["protected_paths"].default)
            merged["protected_paths"] = list(dict.fromkeys([*base, *project["protected_paths"]]))
        if off := [*user.get("lsp_disabled", []), *project.get("lsp_disabled", [])]:
            merged["lsp_disabled"] = off
        return init_settings, InitSettingsSource(settings_cls, env), InitSettingsSource(settings_cls, merged)

    def protected(self) -> list[str]:
        """protected_paths, with ALWAYS_PROTECTED."""
        return list(dict.fromkeys([*ALWAYS_PROTECTED, *self.protected_paths]))

    def task(self, name: str) -> TaskConfig:
        return self.tasks.get(name) or TaskConfig()
