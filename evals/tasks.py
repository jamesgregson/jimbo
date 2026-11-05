"""Task folders (evals/tasks/<id>/) and their task.toml."""

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVALS_DIR = Path(__file__).parent
TASKS_DIR = EVALS_DIR / "tasks"
FIXTURES_DIR = EVALS_DIR / "fixtures"


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
    prompt: str
    fixture: str | None = None  # a folder in evals/fixtures/ to start from, instead of the task's repo/
    tags: list[str] = []
    timeout: float = 600.0
    mode: Literal["normal", "plan"] = "normal"
    followups: list[str] = []
    deny: list[str] = []
    settings: dict[str, Any] = {}
    check: Check = Field(default_factory=Check)
    judge: Judge | None = None

    @property
    def repo(self) -> Path:
        return FIXTURES_DIR / self.fixture if self.fixture else self.dir / "repo"

    @property
    def check_dir(self) -> Path:
        return self.dir / "check"

    @property
    def solution(self) -> Path:
        return self.dir / "solution"

    @model_validator(mode="after")
    def _graded(self) -> "Task":
        if not (self.check.command or self.check.events or self.check.no_repeated_denials or self.judge):
            raise ValueError("task has no way to pass or fail: add [check] command, [[check.events]] or [judge]")
        return self


def load_task(d: Path) -> Task:
    with open(d / "task.toml", "rb") as f:
        data = tomllib.load(f)
    try:
        task = Task(**data, id=d.name, dir=d)
    except ValueError as e:
        raise ValueError(f"{d / 'task.toml'}: {e}") from None
    if not task.repo.is_dir():
        raise ValueError(f"{d}: no fixture {task.fixture!r}" if task.fixture else f"{d}: no repo/ folder")
    return task


def load_tasks(ids: list[str] | None = None, tags: list[str] | None = None, root: Path = TASKS_DIR) -> list[Task]:
    """Every task under `root`, or those with one of `ids` or one of `tags`. Unknown ids are an error."""
    found = {d.name: d for d in sorted(root.iterdir()) if (d / "task.toml").is_file()}
    if unknown := sorted(set(ids or []) - found.keys()):
        raise ValueError(f"unknown task: {', '.join(unknown)}")
    tasks = [load_task(d) for d in found.values()]
    if ids or tags:
        tasks = [t for t in tasks if t.id in (ids or []) or set(t.tags) & set(tags or [])]
    return tasks
