"""Which model runs which task. The main agent is one task among several (`jimbo-shell-assess`, `jimbo-compaction`, ...)."""

import asyncio
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass

from pydantic_ai.models import Model

from ..config import Settings

MAIN = "main"
# Tasks jimbo itself defines, with a description for /models. Any other name works too.
KNOWN_TASKS = {
    MAIN: "the agent you talk to",
    "jimbo-shell-assess": "summary and risk assessment of shell commands",
    "jimbo-compaction": "summarizing old history to free context",
}


@dataclass
class Resolution:
    task: str
    spec: str  # "<provider>:<name>"
    source: str  # where the choice came from: config, session, skill <name>, or main (fallback)
    via: str | None = None  # the alias that was expanded, if any


class ModelRouter:
    """Resolves a task name to a model. Precedence, highest first:

    1. skill scopes (alias names only: they may pick among the user's models, never define one)
    2. session overrides (`/models set`)
    3. `[models]` in the user's config
    4. the main model (the required `main` alias)
    Built models are cached by spec, so tasks sharing a model share a client. Agents are resolved by
    `for_agent`, which puts the user's choices above the agent's file.
    """

    def __init__(self, settings: Settings, builder: Callable[[str, Settings], Model],
                 *, main_model: Model | None = None) -> None:
        self.settings = settings
        self._builder = builder
        self._main_spec = settings.model_aliases.get(MAIN, "")
        if not self._main_spec and main_model is None:
            raise ValueError('no main model: set main = "<provider>:<name>" under [model_aliases] '
                             "in ~/.jimbo/config.toml")
        self._main_session = False
        self._injected = main_model  # a ready-made main model (tests); dropped by set_main
        self._cache: dict[str, Model] = {}
        self._session: dict[str, str] = {}
        self._scopes: list[tuple[str, dict[str, str]]] = []

    # -- resolution ---------------------------------------------------------------------------

    def _lookup(self, task: str) -> tuple[str, str] | None:
        for owner, overrides in reversed(self._scopes):
            if task in overrides:
                return overrides[task], owner
        if task in self._session:
            return self._session[task], "session"
        if task in self.settings.models:
            return self.settings.models[task], "config"
        return None

    def _expand(self, value: str) -> tuple[str, str | None]:
        if value == MAIN:
            return self._main_spec, None
        if value in self.settings.model_aliases:
            return self.settings.model_aliases[value], value
        return value, None

    def explain(self, task: str) -> Resolution:
        if task == MAIN:
            return Resolution(MAIN, self._main_spec, "session" if self._main_session else "config")
        found = self._lookup(task)
        if found is None:
            return Resolution(task, self._main_spec, "main")
        spec, via = self._expand(found[0])
        return Resolution(task, spec, found[1], via)

    def _build(self, spec: str) -> Model:
        if spec == self._main_spec and self._injected is not None:
            return self._injected
        if spec not in self._cache:
            self._cache[spec] = self._builder(spec, self.settings)  # may raise ValueError (bad spec, no key)
        return self._cache[spec]

    def resolve(self, task: str) -> Model:
        """The model for `task`. May run a key command, so async callers use `aresolve`."""
        return self._build(self.explain(task).spec)

    async def aresolve(self, task: str) -> Model:
        return await asyncio.to_thread(self.resolve, task)

    async def abuild(self, spec: str) -> Model:
        return await asyncio.to_thread(self._build, spec)

    def for_agent(self, name: str, requested: str | None = None) -> tuple[Resolution, str | None]:
        """The model for agent `name`: session override, `[models]`, the alias its file asked for, then main.
        `requested` comes from a file that may belong to a repo, so only aliases count. Also returns a warning
        if `requested` couldn't be used."""
        for choices, source in ((self._session, "session"), (self.settings.models, "config")):
            if name in choices:
                spec, via = self._expand(choices[name])
                return Resolution(name, spec, source, via), None
        if requested == MAIN or requested in self.settings.model_aliases:
            spec, via = self._expand(requested)
            return Resolution(name, spec, f"agent {name}", via), None
        warning = f"agent {name}: unknown model alias {requested!r}; using main" if requested else None
        return Resolution(name, self._main_spec, "main"), warning

    def child(self) -> "ModelRouter":
        """A router for an agent run: same models and choices, but skills it loads scope only to itself."""
        router = copy(self)
        router._scopes = []
        return router

    # -- changing the choice ------------------------------------------------------------------

    def set_main(self, spec: str) -> Model:
        """Switch the main model. Builds it first, so a bad spec changes nothing."""
        model = self._builder(spec, self.settings)
        self._cache[spec] = model
        self._main_spec, self._main_session, self._injected = spec, True, None
        self.settings.model_aliases[MAIN] = spec
        return model

    def explain_value(self, value: str) -> str:
        """The spec an alias, "main" or raw spec stands for."""
        return self._expand(value)[0]

    def set_override(self, task: str, value: str) -> Resolution:
        """Session-level choice by the user: an alias, "main", or a raw spec. Validated by building it."""
        if task == MAIN:
            self.set_main(self._expand(value)[0])
            return self.explain(MAIN)
        self._build(self._expand(value)[0])
        self._session[task] = value
        return self.explain(task)

    def clear_override(self, task: str | None = None) -> None:
        if task is None:
            self._session.clear()
        else:
            self._session.pop(task, None)

    def push_scope(self, owner: str, overrides: dict[str, str]) -> None:
        """Skill/agent choice for the current turn. Only alias names (or "main") are accepted."""
        bad = [v for v in overrides.values() if v != MAIN and v not in self.settings.model_aliases]
        if bad:
            raise ValueError(f"{owner}: unknown model alias {', '.join(map(repr, bad))}; "
                             f"defined: {', '.join(sorted(self.settings.model_aliases)) or 'none'}")
        if MAIN in overrides:
            raise ValueError(f"{owner}: a skill cannot change the main model")
        self._scopes.append((owner, dict(overrides)))

    def pop_scope(self, owner: str | None = None) -> None:
        """Drop `owner`'s scopes, or all of them (end of turn)."""
        self._scopes = [] if owner is None else [s for s in self._scopes if s[0] != owner]

    # -- reporting ----------------------------------------------------------------------------

    def describe(self) -> list[Resolution]:
        names = [*KNOWN_TASKS, *sorted({*self.settings.models, *self._session,
                                         *(t for _, o in self._scopes for t in o)} - KNOWN_TASKS.keys())]
        return [self.explain(t) for t in names]
