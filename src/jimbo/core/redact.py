"""Hide secrets in text before the model or the session log sees it: known token formats, and the exact values of
keys jimbo knows (provider API keys, secret-looking environment variables)."""

import os
import re

# (what it is, pattern). Specific formats come before the generic `sk-` one.
FORMATS: list[tuple[str, re.Pattern[str]]] = [(kind, re.compile(p)) for kind, p in [
    ("private key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"),
    ("aws access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github token", r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ("gitlab token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("stripe key", r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}"),
    ("google api key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("openrouter key", r"\bsk-or-[A-Za-z0-9_-]{20,}"),
    ("openai key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]]
MIN_VALUE = 12  # shorter values (and paths) are not hidden by value: too likely to be ordinary text

_values: dict[str, str] = {}  # secret value -> what it is


def add_secret(value: str | None, kind: str = "api key") -> None:
    """Hide `value` wherever it appears from now on."""
    if value and len(value) >= MIN_VALUE and not value.startswith("/") and not any(c.isspace() for c in value):
        _values[value] = kind


def add_env_secrets(is_secret) -> None:
    """Hide the values of environment variables whose names `is_secret(name)` says are secret."""
    for k, v in os.environ.items():
        if is_secret(k):
            add_secret(v, f"${k}")


def clear() -> None:
    _values.clear()


def redact(text: str) -> str:
    if not text:
        return text
    for value in sorted(_values, key=len, reverse=True):  # longest first, so a key that contains another goes whole
        if value in text:
            text = text.replace(value, f"[redacted: {_values[value]}]")
    for kind, rx in FORMATS:
        text = rx.sub(f"[redacted: {kind}]", text)
    return text


def found(text: str) -> list[str]:
    """What kinds of secret `text` contains."""
    kinds = [_values[v] for v in _values if v in text]
    return list(dict.fromkeys([*kinds, *(kind for kind, rx in FORMATS if rx.search(text))]))


def redact_value(value):
    """`value` with strings redacted, inside lists and dicts too (tool results can be lists with images)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    return value
