"""Model and provider changes made from inside jimbo: saved to ~/.jimbo/config.toml, then applied to the running
session. Comments and layout in the file are kept. Only the user file is written; a workspace config can't hold
these keys (see UNTRUSTED_KEYS)."""

import asyncio
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomlkit
from pydantic import TypeAdapter, ValidationError
from tomlkit.exceptions import TOMLKitError

from .agent import build_agent
from .config import ProviderConfig, Settings
from .core.commands import NAME_RE
from .core.models import MAIN
from .providers import credentials
from .providers.credentials import delete_key, describe_key_source, store_key
from .providers.preflight import ModelFacts, list_models
from .providers.registry import all_providers, builtin_providers, split_spec

if TYPE_CHECKING:
    from .core.session import AgentSession

SECTIONS = ("model_aliases", "models", "providers")
KEY_SOURCES = ("api_key", "api_key_env", "api_key_file", "api_key_cmd")
ENV_RE = re.compile(r"\$([A-Za-z_]\w*)")
Inspector = Callable[[str, Settings], Awaitable[ModelFacts]]


class ConfigFile:
    """The user's config.toml. Each edit reads the file again, so changes made in an editor since startup are kept."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> tomlkit.TOMLDocument:
        try:
            return tomlkit.parse(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return tomlkit.document()
        except TOMLKitError as e:
            raise ValueError(f"{self.path}: {e}") from None

    def section(self, name: str) -> dict:
        return self.read().unwrap().get(name) or {}

    def edit(self, change: Callable[[tomlkit.TOMLDocument], None]) -> dict:
        """Apply `change`, check the sections jimbo edits, and write. Returns the new file as plain data."""
        doc = self.read()
        change(doc)
        data = doc.unwrap()
        for name in SECTIONS:
            try:
                TypeAdapter(Settings.model_fields[name].annotation).validate_python(data.get(name, {}))
            except ValidationError as e:
                err = e.errors()[0]
                where = ".".join(map(str, (name, *err["loc"])))
                raise ValueError(f"not saved: {where}: {err['msg'].removeprefix('Value error, ')}") from None
        self._write(tomlkit.dumps(doc))
        return data

    def _write(self, text: str) -> None:
        target = self.path.resolve()  # a symlinked config (dotfiles) stays a symlink
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config.", suffix=".toml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _table(container, name: str, *, super_table: bool = False):
    if name not in container:
        container[name] = tomlkit.table(is_super_table=super_table)
    return container[name]


def _remove(container, *path: str) -> bool:
    """Delete container[path...]; tables left empty are removed too. False if it wasn't there."""
    head, *rest = path
    if head not in container:
        return False
    if rest:
        found = _remove(container[head], *rest)
    else:
        del container[head]
        return True
    if found and not container[head]:
        del container[head]
    return found


def _provider_of(value: str) -> str | None:
    provider, colon, _ = value.partition(":")
    return provider if colon else None


def _check_name(kind: str, name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"{name!r} can't be a {kind} name: use letters, digits, - and _")


def _check_url(url: str) -> None:
    if not re.fullmatch(r"https?://\S+", url):
        raise ValueError(f"{url!r} isn't an http(s) URL")


class ConfigControl:
    """Model and provider changes from slash commands and frontend controls. `inspector(spec, settings)` checks a
    model before it becomes the main one; it is None in tests with a fake model."""

    def __init__(self, settings: Settings, inspector: Inspector | None = None) -> None:
        self.settings = settings
        self.file = ConfigFile(settings.user_dir / "config.toml")
        self.inspector = inspector
        self.lister: Callable[[str, Settings], Awaitable[list[str] | str]] = list_models
        self._models: dict[str, list[str] | str] = {}  # provider -> model ids, or why they couldn't be listed

    # -- reporting ----------------------------------------------------------------------------

    def snapshot(self, session: "AgentSession") -> dict[str, Any]:
        """The `config` event: what is set and where it comes from. Never key values."""
        router = session.deps.models
        tasks = [{"task": r.task, "spec": r.spec, "via": r.via, "source": r.source,
                  "value": self.settings.models.get(r.task), "agent": False} for r in router.describe()]
        for name, spec in session.deps.agents.items():
            r, _ = router.for_agent(name, spec.model)
            tasks.append({"task": name, "spec": r.spec, "via": r.via, "source": r.source,
                          "value": self.settings.models.get(name), "agent": True})
        return {"path": str(self.file.path), "aliases": dict(self.settings.model_aliases), "tasks": tasks,
                "providers": self._providers()}

    async def emit(self, session: "AgentSession") -> None:
        await session.emit("config", **self.snapshot(session))

    def describe_providers(self) -> str:
        rows = []
        for p in self._providers():
            flags = ("built in, changed" if p["changed"] else "built in") if p["builtin"] else ""
            rows.append(f"  {p['name']:<12}{p['base_url'] or 'default url':<32}  key: {p['key']}"
                        + (f"  ({flags})" if flags else ""))
        return "\n".join([f"Providers ({self.file.path}):", *rows])

    def _providers(self) -> list[dict]:
        builtin = builtin_providers(self.settings)
        return [{"name": n, "type": c.type, "base_url": c.base_url, "key": describe_key_source(c), "builtin": n in builtin,
                 "changed": n in self.settings.providers} for n, c in all_providers(self.settings).items()]

    # -- models -------------------------------------------------------------------------------

    async def switch_main(self, session: "AgentSession", value: str, *, save: bool = False) -> str:
        """Make `value` (an alias or spec) the main model after checking it, as at startup. A model the check
        rejects changes nothing."""
        router = session.deps.models
        spec = router.explain_value(value)
        split_spec(spec)
        facts = None
        if self.inspector is not None:
            async with session.busy("checking model"):
                facts = await self.inspector(spec, self.settings)
            if facts.problem:
                raise ValueError(facts.problem)
        model = await asyncio.to_thread(router.set_main, spec)
        session.agent = build_agent(self.settings, model)
        await self._apply_facts(session, facts)
        lines = [f"model: {spec}"]
        if save:
            self.file.edit(lambda doc: _table(doc, "model_aliases").__setitem__(MAIN, spec))
            lines[0] += f" (saved to {self.file.path})"
            await self.emit(session)
        return "\n".join([*lines, *(facts.notes if facts else [])])

    async def _apply_facts(self, session: "AgentSession", facts: ModelFacts | None) -> None:
        """The context window and vision of a new main model: config wins, then what its server says."""
        s = self.settings
        found = facts.context if facts else None
        session.context_window = s.context_window or found or s.num_ctx
        session.vision = s.vision and not (facts and facts.vision is False)
        await session.emit_context()

    async def set_alias(self, session: "AgentSession", name: str, spec: str) -> str:
        if name == MAIN:
            return await self.switch_main(session, spec, save=True)
        _check_name("alias", name)
        split_spec(spec)  # an alias names a model; it can't point at another alias
        await session.deps.models.abuild(spec)
        self.file.edit(lambda doc: _table(doc, "model_aliases").__setitem__(name, spec))
        self.settings.model_aliases[name] = spec
        await self.emit(session)
        return f"{name} = {spec} (saved)"

    async def remove_alias(self, session: "AgentSession", name: str) -> str:
        if name == MAIN:
            raise ValueError("main can't be removed; change it with /model <value> --save")
        used = sorted({*session.deps.models.users(lambda v: v == name),
                       *(t for t, v in self.file.section("models").items() if v == name)})
        if used:
            raise ValueError(f"alias {name} is used by {', '.join(used)}; change those first")
        if name not in self.file.section("model_aliases"):
            raise ValueError(f"{name} isn't an alias in {self.file.path}")
        self.file.edit(lambda doc: _remove(doc, "model_aliases", name))
        self.settings.model_aliases.pop(name, None)
        await self.emit(session)
        agents = sorted(n for n, a in session.deps.agents.items() if a.model == name)
        return f"Removed alias {name}." + (f" These agents will use main: {', '.join(agents)}" if agents else "")

    async def set_task(self, session: "AgentSession", task: str, value: str, *, save: bool = False) -> str:
        if task == MAIN:
            return await self.switch_main(session, value, save=save)
        router = session.deps.models
        r = router.set_override(task, value)  # builds it, so a bad value changes nothing
        if save:
            self.file.edit(lambda doc: _table(doc, "models").__setitem__(task, value))
            self.settings.models[task] = value
            router.clear_override(task)
            await self.emit(session)
        return f"{task}: {r.spec}" + (f" [{r.via}]" if r.via else "") + (" (saved)" if save else "")

    async def clear_task(self, session: "AgentSession", task: str, *, save: bool = False) -> str:
        router = session.deps.models
        router.clear_override(task)
        if save:
            self.file.edit(lambda doc: _remove(doc, "models", task))
            self.settings.models.pop(task, None)
            await self.emit(session)
        return f"{task}: {router.explain(task).spec}" + (" (saved)" if save else "")

    # -- providers ----------------------------------------------------------------------------

    async def set_provider(self, session: "AgentSession", name: str, *, base_url: str | None = None,
                           key: str | None = None, key_env: str | None = None, new: bool = False) -> str:
        """Add a provider, or change its URL or key. A pasted `key` is stored outside config.toml."""
        if new:
            self.check_new_provider(name, base_url)
        elif name not in all_providers(self.settings):
            raise ValueError(f"unknown provider {name!r}; add it with /providers add {name} <url>")
        elif base_url is not None:
            _check_url(base_url)
        if key_env is not None and not re.fullmatch(r"[A-Za-z_]\w*", key_env):
            raise ValueError(f"{key_env!r} isn't an environment variable name")
        old = self.settings.providers.get(name)
        source: dict[str, Any] = {}
        if key is not None:
            source = await asyncio.to_thread(store_key, name, key, self.settings.user_dir)
        elif key_env is not None:
            source = {"api_key_env": key_env}

        def change(doc) -> None:
            table = _table(_table(doc, "providers", super_table=True), name)
            if base_url is not None:
                table["base_url"] = base_url
            if source:
                for k in KEY_SOURCES:
                    table.pop(k, None)
                table.update(source)

        data = self.file.edit(change)
        if key_env is not None and old is not None:  # a pasted key overwrites the stored one; this replaces it
            await asyncio.to_thread(delete_key, name, old, self.settings.user_dir)
        self.settings.providers[name] = ProviderConfig.model_validate(data["providers"][name])
        warning = await self._provider_changed(session, name)
        await self.emit(session)
        lines = [f"{name}: saved", await self.test_provider(name)]
        return "\n".join(lines + ([warning] if warning else []))

    def check_new_provider(self, name: str, base_url: str | None) -> None:
        _check_name("provider", name)
        if name in all_providers(self.settings):
            raise ValueError(f"provider {name} already exists; use /providers url or /providers key")
        if not base_url:
            raise ValueError("a new provider needs a URL")
        _check_url(base_url)

    async def remove_provider(self, session: "AgentSession", name: str) -> str:
        """Remove a provider, or the user's overrides of a built-in one."""
        builtin = name in builtin_providers(self.settings)
        in_file = name in self.file.section("providers")
        if not in_file:
            raise ValueError(f"{name} has no settings in {self.file.path}" if builtin or name in self.settings.providers
                             else f"unknown provider {name!r}")
        if not builtin:
            uses = lambda v: _provider_of(self.settings.model_aliases.get(v, v)) == name
            used = sorted({*(f"alias {a}" for a, s in {**self.file.section("model_aliases"),
                                                        **self.settings.model_aliases}.items()
                             if _provider_of(s) == name),
                           *session.deps.models.users(uses),
                           *(t for t, v in self.file.section("models").items() if _provider_of(v) == name)})
            if used:
                raise ValueError(f"provider {name} is used by {', '.join(used)}; change those first")
        old = self.settings.providers.get(name)
        self.file.edit(lambda doc: _remove(doc, "providers", name))
        self.settings.providers.pop(name, None)
        if old is not None:
            await asyncio.to_thread(delete_key, name, old, self.settings.user_dir)
        warning = await self._provider_changed(session, name)
        await self.emit(session)
        text = f"{name}: back to the built-in settings" if builtin else f"Removed provider {name}."
        return text + (f"\n{warning}" if warning else "")

    async def _provider_changed(self, session: "AgentSession", name: str) -> str | None:
        """Drop what was built with the old settings. Returns a warning if the main model no longer builds."""
        credentials.clear_cache()
        self._models.pop(name, None)
        router = session.deps.models
        router.forget(name)
        if _provider_of(router.explain(MAIN).spec) != name:
            return None
        try:
            session.agent = build_agent(self.settings, await router.aresolve(MAIN))
        except ValueError as e:
            return f"warning: the main model can't be used now: {e}"
        return None

    async def list_models(self, provider: str, *, refresh: bool = False) -> list[str] | str:
        """The provider's model ids, or why they couldn't be listed. Kept for the session."""
        if refresh or provider not in self._models:
            self._models[provider] = await self.lister(provider, self.settings)
        return self._models[provider]

    async def test_provider(self, name: str) -> str:
        found = await self.list_models(name, refresh=True)
        if isinstance(found, str):
            return found if name in found else f"{name}: {found}"
        return f"{name}: reachable, {len(found)} model{'' if len(found) == 1 else 's'}"

    # -- frontend controls --------------------------------------------------------------------

    async def apply(self, session: "AgentSession", msg: dict) -> str | None:
        """One request from a settings panel (see the web frontend's module docstring). Returns a message
        for the user; raises ValueError when the request is refused."""

        def arg(name: str, *, optional: bool = False) -> Any:
            value = msg.get(name)
            if value is None and optional:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"config request {msg.get('op')!r}: {name} is missing")
            return value.strip()

        match msg.get("op"):
            case "get":
                await self.emit(session)
                return None
            case "main":
                return await self.switch_main(session, arg("value"), save=bool(msg.get("save")))
            case "alias_set":
                return await self.set_alias(session, arg("name"), arg("spec"))
            case "alias_rm":
                return await self.remove_alias(session, arg("name"))
            case "task_set":
                value = arg("value", optional=True)
                if value is None:
                    return await self.clear_task(session, arg("task"), save=True)
                return await self.set_task(session, arg("task"), value, save=True)
            case "provider_set":
                env = arg("key_env", optional=True)
                if env is not None and (m := ENV_RE.fullmatch(env)):
                    env = m.group(1)
                return await self.set_provider(session, arg("name"), base_url=arg("base_url", optional=True),
                                               key=arg("key", optional=True), key_env=env, new=bool(msg.get("new")))
            case "provider_rm":
                return await self.remove_provider(session, arg("name"))
            case "provider_test":
                return await self.test_provider(arg("name"))
        raise ValueError(f"unknown config request {msg.get('op')!r}")
