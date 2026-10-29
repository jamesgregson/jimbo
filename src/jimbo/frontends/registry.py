from ..core.interaction import Frontend
from .cli_plain import PlainFrontend

FRONTENDS: dict[str, type] = {"plain": PlainFrontend}


def build_frontend(name: str) -> Frontend:
    try:
        return FRONTENDS[name]()
    except KeyError:
        raise ValueError(f"unknown ui {name!r}; known: {sorted(FRONTENDS)}") from None
