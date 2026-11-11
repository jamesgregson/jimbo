"""Suites (evals/suites/<name>/suite.toml) and their tasks (tasks/<id>/task.toml, or a suite's source)."""

import importlib
import tomllib
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVALS_DIR = Path(__file__).parent
SUITES_DIR = EVALS_DIR / "suites"
FIXTURES_DIR = EVALS_DIR / "fixtures"
CACHE_DIR = EVALS_DIR / ".cache"  # where a suite's source may build its tasks' workspaces
ALL = "all"  # --suite all: every suite


def resolve(spec: str) -> Callable:
    """`module:function` -> the function."""
    module, sep, name = spec.partition(":")
    if not sep or not module or not name:
        raise ValueError(f"want module:function, got {spec!r}")
    try:
        fn = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"can't import {spec}: {e}") from None
    if not callable(fn):
        raise ValueError(f"{spec} is not a function")
    return fn


class Requires(BaseModel):
    """What a suite needs before any of its trials start."""

    model_config = ConfigDict(extra="forbid")

    commands: list[str] = []  # on PATH, or in the sandbox image with --sandbox docker or podman
    sandbox: list[str] = []  # --sandbox must be one of these
    network: bool = False  # --no-network is refused


class Defaults(BaseModel):
    """Values for each task of the suite that doesn't set them."""

    model_config = ConfigDict(extra="forbid")

    timeout: float | None = None
    trials: int | None = None  # when run gets no --trials
    jobs: int | None = None  # when run gets no --jobs
    deny: list[str] = []  # added to each task's deny
    settings: dict[str, Any] = {}  # merged under each task's [settings]


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    dir: Path = Path()
    description: str = ""
    default: bool = False  # runs when run gets no --suite
    pytest: bool = True  # check-task runs on its tasks in uv run pytest
    source: str | None = None  # module:function that returns the suite's tasks, instead of tasks/
    grade: str | None = None  # module:function that grades the suite's trials after they finish
    requires: Requires = Field(default_factory=Requires)
    defaults: Defaults = Field(default_factory=Defaults)

    def tasks(self) -> list["Task"]:
        """The suite's tasks, with its defaults filled in."""
        if self.source:
            made = resolve(self.source)(self)
            try:
                tasks = [t if isinstance(t, Task) else Task(**t, suite=self) for t in made]
            except ValueError as e:
                raise ValueError(f"{self.dir / 'suite.toml'}: source {self.source}: {e}") from None
        else:
            root = self.dir / "tasks"
            tasks = [load_task(d, self) for d in sorted(root.iterdir()) if (d / "task.toml").is_file()] \
                if root.is_dir() else []
        return [self.apply(t) for t in tasks]

    def apply(self, task: "Task") -> "Task":
        d = self.defaults
        update: dict[str, Any] = {"suite": self, "deny": [*d.deny, *task.deny],
                                  "settings": {**d.settings, **task.settings}}
        if d.timeout is not None and "timeout" not in task.model_fields_set:
            update["timeout"] = d.timeout
        return task.model_copy(update=update)


class EventCheck(BaseModel):
    """Count the events of `kind` whose data has every field in `match`, and compare with min / max."""

    model_config = ConfigDict(extra="allow")  # every other key is a field to match, such as tool or ok

    kind: str
    min: int | None = None
    max: int | None = None

    @property
    def match(self) -> dict[str, Any]:
        return dict(self.model_extra or {})

    @model_validator(mode="after")
    def _bounded(self) -> "EventCheck":
        if self.min is None and self.max is None:
            raise ValueError(f"event check on {self.kind!r} needs min or max")
        return self


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str | None = None  # run in the workspace after check/ is copied in; exit code 0 is a pass
    timeout: float = 300.0
    events: list[EventCheck] = []
    no_repeated_denials: bool = False  # a call that was denied is not made again with the same arguments


class Judge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric: str


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    dir: Path = Path()
    suite: Suite | None = None
    prompt: str
    fixture: str | None = None  # a folder in the suite's fixtures/ or evals/fixtures/, instead of the task's repo/
    tags: list[str] = []
    timeout: float = 600.0
    mode: Literal["normal", "plan"] = "normal"
    followups: list[str] = []
    deny: list[str] = []
    settings: dict[str, Any] = {}
    check: Check = Field(default_factory=Check)
    judge: Judge | None = None

    @property
    def suite_name(self) -> str:
        return self.suite.name if self.suite else ""

    @property
    def suite_grade(self) -> str | None:
        """The suite's grader, which grades this task's trials after they all finish."""
        return self.suite.grade if self.suite else None

    @property
    def own_checks(self) -> bool:
        return bool(self.check.command or self.check.events or self.check.no_repeated_denials or self.judge)

    @property
    def repo(self) -> Path:
        if not self.fixture:
            return self.dir / "repo"
        if self.suite and (local := self.suite.dir / "fixtures" / self.fixture).is_dir():
            return local
        return FIXTURES_DIR / self.fixture

    @property
    def check_dir(self) -> Path:
        return self.dir / "check"

    @property
    def solution(self) -> Path:
        return self.dir / "solution"

    @model_validator(mode="after")
    def _graded(self) -> "Task":
        if not self.own_checks and not self.suite_grade:
            raise ValueError("task has no way to pass or fail: add [check] command, [[check.events]] or [judge]")
        return self


def load_task(d: Path, suite: Suite | None = None) -> Task:
    with open(d / "task.toml", "rb") as f:
        data = tomllib.load(f)
    try:
        task = Task(**data, id=d.name, dir=d, suite=suite)
    except ValueError as e:
        raise ValueError(f"{d / 'task.toml'}: {e}") from None
    if not task.repo.is_dir():
        raise ValueError(f"{d}: no fixture {task.fixture!r}" if task.fixture else f"{d}: no repo/ folder")
    return task


def load_suite(d: Path) -> Suite:
    with open(d / "suite.toml", "rb") as f:
        data = tomllib.load(f)
    try:
        suite = Suite(**data, name=d.name, dir=d)
        for spec in filter(None, (suite.source, suite.grade)):
            resolve(spec)
    except ValueError as e:
        raise ValueError(f"{d / 'suite.toml'}: {e}") from None
    return suite


def load_suites(root: Path = SUITES_DIR) -> dict[str, Suite]:
    """Every suite under `root`, by name."""
    suites = {d.name: load_suite(d) for d in sorted(root.iterdir()) if (d / "suite.toml").is_file()}
    if ALL in suites:
        raise ValueError(f"{root / ALL}: a suite can't be called {ALL!r}")
    return suites


def pick_suites(suites: dict[str, Suite], names: list[str] | None) -> list[Suite]:
    """The suites named (`all` for every suite), or the default suites when none are named."""
    if not names:
        return [s for s in suites.values() if s.default]
    if ALL in names:
        return list(suites.values())
    if unknown := sorted(set(names) - suites.keys()):
        raise ValueError(f"unknown suite: {', '.join(unknown)} (suites: {', '.join(suites)})")
    return [s for s in suites.values() if s.name in names]


def load_tasks(ids: list[str] | None = None, tags: list[str] | None = None, suites: list[str] | None = None,
               root: Path = SUITES_DIR) -> list[Task]:
    """The tasks of the chosen suites (the default suites when none are named), or those with one of `tags`, plus
    the tasks with one of `ids`, which are searched for in every suite. Unknown ids are an error."""
    found = load_suites(root)
    chosen = pick_suites(found, suites)
    loaded = found.values() if ids else chosen
    tasks: dict[str, Task] = {}
    for suite in loaded:
        for t in suite.tasks():
            if t.id in tasks:
                raise ValueError(f"task {t.id!r} is in suites {tasks[t.id].suite_name} and {suite.name}")
            tasks[t.id] = t
    if unknown := sorted(set(ids or []) - tasks.keys()):
        raise ValueError(f"unknown task: {', '.join(unknown)}")
    pool = {s.name for s in chosen}
    if not ids and not tags:
        return [t for t in tasks.values() if t.suite_name in pool]
    return [t for t in tasks.values()
            if t.id in (ids or []) or (t.suite_name in pool and set(t.tags) & set(tags or []))]
