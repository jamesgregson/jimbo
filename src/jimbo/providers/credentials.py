"""Resolve a provider's API key from its configured source. Values are never logged or put in errors."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr

from ..config import ProviderConfig

CMD_TIMEOUT = 10  # seconds
KEYCHAIN_ACCOUNT = "jimbo"  # keys jimbo stores in the macOS Keychain: service jimbo-<provider>, this account
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
    if value:
        from ..core.redact import add_secret
        add_secret(value, f"{name} api key")  # hidden if it ever shows up in a tool result
    return SecretStr(value) if value else None


def keychain_cmd(name: str) -> list[str]:
    return ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", f"jimbo-{name}", "-w"]


def key_file(name: str, user_dir: Path) -> Path:
    return user_dir / "keys" / name


def use_keychain() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _keychain_store(name: str, value: str) -> None:
    # `security -i` reads the command from stdin, so the key never appears in a process list.
    quoted = value.replace("\\", "\\\\").replace('"', '\\"')
    line = f'add-generic-password -U -a {KEYCHAIN_ACCOUNT} -s "jimbo-{name}" -w "{quoted}"\n'
    try:
        subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=CMD_TIMEOUT)
        stored = subprocess.run(keychain_cmd(name), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=CMD_TIMEOUT).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        stored = None
    if stored != value:  # interactive mode always exits 0, so read the key back
        raise CredentialError(f"provider {name}: could not store the key in the Keychain")


def store_key(name: str, value: str, user_dir: Path) -> dict[str, str | list[str]]:
    """Store a pasted key outside config.toml: in the macOS Keychain, else in a 0600 file under user_dir/keys.
    Returns the [providers.<name>] setting that reads it back."""
    value = value.strip()
    if not value or any(c in value for c in "\r\n\0"):
        raise CredentialError(f"provider {name}: the key is empty or has line breaks")
    if use_keychain():
        _keychain_store(name, value)
        return {"api_key_cmd": keychain_cmd(name)}
    path = key_file(name, user_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(value + "\n")
    return {"api_key_file": str(path)}


def delete_key(name: str, cfg: ProviderConfig, user_dir: Path) -> None:
    """Delete the key store_key made for `cfg`, if that is where its key comes from. Keys jimbo didn't store
    are left alone."""
    if cfg.api_key_cmd == keychain_cmd(name):
        try:
            subprocess.run(["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", f"jimbo-{name}"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=CMD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif cfg.api_key_file is not None and cfg.api_key_file.expanduser() == key_file(name, user_dir):
        key_file(name, user_dir).unlink(missing_ok=True)


def describe_key_source(cfg: ProviderConfig) -> str:
    """Where the key comes from, for /models; never the key itself."""
    if cfg.api_key is not None:
        return "inline"
    if cfg.api_key_env is not None:
        return f"${cfg.api_key_env}"
    if cfg.api_key_file is not None:
        return f"file {cfg.api_key_file}"
    if cfg.api_key_cmd is not None and cfg.api_key_cmd[:2] == ["security", "find-generic-password"] \
            and "-s" in cfg.api_key_cmd[:-1]:
        return f"keychain {cfg.api_key_cmd[cfg.api_key_cmd.index('-s') + 1]}"
    if cfg.api_key_cmd is not None:
        return f"cmd {cfg.api_key_cmd[0]}"
    if cfg.fallback_key_envs:
        return "$" + " or $".join(cfg.fallback_key_envs) + " if set"
    return "none"
