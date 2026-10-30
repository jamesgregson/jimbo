from typing import Callable

from ..core.interaction import Frontend
from .cli_plain import PlainFrontend

def _tui() -> Frontend:
    from .tui import TuiFrontend  # imported lazily so `plain` doesn't need rich/prompt_toolkit

    return TuiFrontend()


FRONTENDS: dict[str, Callable[[], Frontend]] = {"plain": PlainFrontend, "tui": _tui}


def build_frontend(name: str) -> Frontend:
    try:
        return FRONTENDS[name]()
    except KeyError:
        raise ValueError(f"unknown ui {name!r}; known: {sorted(FRONTENDS)}") from None
