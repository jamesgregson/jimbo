from typing import Callable

from ..core.interaction import Frontend
from .cli_plain import PlainFrontend

def _tui() -> Frontend:
    from .tui import TuiFrontend  # imported lazily so `plain` doesn't need rich/prompt_toolkit

    return TuiFrontend()


def _web() -> Frontend:
    from ..config import Settings
    from .web import WebFrontend  # lazy: needs aiohttp

    st = Settings()
    return WebFrontend(st.web_host, st.web_port, st.web_open_browser)


FRONTENDS: dict[str, Callable[[], Frontend]] = {"plain": PlainFrontend, "tui": _tui, "web": _web}


def build_frontend(name: str) -> Frontend:
    try:
        return FRONTENDS[name]()
    except KeyError:
        raise ValueError(f"unknown ui {name!r}; known: {sorted(FRONTENDS)}") from None
