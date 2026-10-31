"""Resolve a provider's API key from its configured source. Values are never logged or put in errors."""

import os
import subprocess
from pathlib import Path

from pydantic import SecretStr

from ..config import ProviderConfig

CMD_TIMEOUT = 10  # seconds
_cmd_cache: dict[tuple[str, ...], SecretStr] = {}


class CredentialError(ValueError):
    """A key could not be obtained. The message never contains key material."""


def clear_cache() -> None:
    _cmd_cache.clear()


def _from_file(name: str, path: Path) -> str:
    path = path.expanduser()
    try:
        if path.stat().st_mode & 0o077:
            raise CredentialError(f"provider {name}: {path} is readable by group/others (chmod 600 it)")
        return path.read_text("utf-8")
    except OSError as e:
        raise CredentialError(f"provider {name}: cannot read api_key_file {path}: {e.strerror}") from None


def _from_cmd(name: str, argv: list[str]) -> str:
    key = tuple(argv)
    if key not in _cmd_cache:
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CMD_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise CredentialError(f"provider {name}: api_key_cmd timed out after {CMD_TIMEOUT}s") from None
        except OSError as e:
            raise CredentialError(f"provider {name}: api_key_cmd could not run: {e.strerror}") from None
        if r.returncode != 0:  # stderr is deliberately not echoed: some tools print secrets there
            raise CredentialError(f"provider {name}: api_key_cmd failed (exit {r.returncode})")
        _cmd_cache[key] = SecretStr(r.stdout.strip())
    return _cmd_cache[key].get_secret_value()


def resolve_api_key(name: str, cfg: ProviderConfig) -> SecretStr | None:
    """The key for provider `name`, or None when it has none (local providers send a dummy)."""
    value: str | None = None
    if cfg.api_key is not None:
        value = cfg.api_key.get_secret_value()
    elif cfg.api_key_env is not None:
        value = os.environ.get(cfg.api_key_env)
        if not value:
            raise CredentialError(f"provider {name}: environment variable {cfg.api_key_env} is not set")
    elif cfg.api_key_file is not None:
        value = _from_file(name, cfg.api_key_file)
    elif cfg.api_key_cmd is not None:
        value = _from_cmd(name, cfg.api_key_cmd)
    else:
        value = next((v for e in cfg.fallback_key_envs if (v := os.environ.get(e))), None)
    value = value.strip() if value else None
    if cfg.key_sources() and not value:
        raise CredentialError(f"provider {name}: the configured API key is empty")
    if not value and cfg.require_key:
        raise CredentialError(f"provider {name} has no API key configured "
                        f"(set api_key_env, api_key_file or api_key_cmd under [providers.{name}])")
    return SecretStr(value) if value else None


def describe_key_source(cfg: ProviderConfig) -> str:
    """Where the key comes from, for /models; never the key itself."""
    if cfg.api_key is not None:
        return "inline"
    if cfg.api_key_env is not None:
        return f"${cfg.api_key_env}"
    if cfg.api_key_file is not None:
        return f"file {cfg.api_key_file}"
    if cfg.api_key_cmd is not None:
        return f"cmd {cfg.api_key_cmd[0]}"
    if cfg.fallback_key_envs:
        return "$" + " or $".join(cfg.fallback_key_envs) + " if set"
    return "none"
