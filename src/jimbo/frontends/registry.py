from typing import Callable

from ..config import Settings
from ..core.interaction import Frontend
from .cli_plain import PlainFrontend


def _plain(st: Settings) -> Frontend:
    return PlainFrontend()


def _tui(st: Settings) -> Frontend:
    from .tui import TuiFrontend  # imported lazily so `plain` doesn't need rich/prompt_toolkit

    return TuiFrontend()


def _web(st: Settings) -> Frontend:
    from .web import WebFrontend  # lazy: needs aiohttp

    return WebFrontend(st.web_host, st.web_port, st.web_open_browser)


FRONTENDS: dict[str, Callable[[Settings], Frontend]] = {"plain": _plain, "tui": _tui, "web": _web}


def build_frontend(settings: Settings) -> Frontend:
    try:
        factory = FRONTENDS[settings.ui]
    except KeyError:
        raise ValueError(f"unknown ui {settings.ui!r}; known: {sorted(FRONTENDS)}") from None
    return factory(settings)
