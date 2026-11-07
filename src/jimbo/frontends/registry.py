from typing import Callable

from ..config import Settings
from ..core.interaction import Frontend
from .cli_json import JsonFrontend
from .cli_plain import PlainFrontend


def _plain(st: Settings) -> Frontend:
    return PlainFrontend()


def _tui(st: Settings) -> Frontend:
    from .tui import TuiFrontend  # imported lazily so `plain` doesn't need rich/prompt_toolkit

    return TuiFrontend(workspace=st.workspace)


def _web(st: Settings) -> Frontend:
    from .web import WebFrontend  # lazy: needs aiohttp

    return WebFrontend(st.web_host, st.web_port, st.web_open_browser, st.workspace)


def _json(st: Settings) -> Frontend:
    return JsonFrontend()


def _stdio(st: Settings) -> Frontend:
    from .stdio import StdioFrontend

    return StdioFrontend(st.workspace)


FRONTENDS: dict[str, Callable[[Settings], Frontend]] = {"plain": _plain, "tui": _tui, "web": _web, "json": _json,
                                                        "stdio": _stdio}


def pick_ui(prompt: str | None, stdin_tty: bool, stdout_tty: bool) -> str:
    """What `ui = "auto"` means: the full UI for a person at a terminal, and plain for one-shot tasks and pipes."""
    return "tui" if stdin_tty and stdout_tty and not prompt else "plain"


def build_frontend(settings: Settings) -> Frontend:
    try:
        factory = FRONTENDS[settings.ui]
    except KeyError:
        raise ValueError(f"unknown ui {settings.ui!r}; known: {sorted(FRONTENDS)}") from None
    return factory(settings)
