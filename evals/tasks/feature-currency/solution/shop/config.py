import os
import tomllib
from pathlib import Path

DEFAULTS = {"storage": "memory", "tax_rate": 0.1, "page_size": 20, "sender": "orders@example.com",
            "currency": "USD"}


def load_settings(path="shop.toml", env=None):
    env = os.environ if env is None else env
    settings = dict(DEFAULTS)
    p = Path(path)
    if p.is_file():
        settings.update(tomllib.loads(p.read_text()))
    for key in DEFAULTS:
        value = env.get(f"SHOP_{key.upper()}")
        if value is not None:
            settings[key] = type(DEFAULTS[key])(value)
    return settings
