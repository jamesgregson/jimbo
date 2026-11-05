"""Running trials: one task, one attempt, in its own temp workspace with its own session."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jimbo.config import USER_DIR_ENV, ProviderConfig, Settings
from jimbo.core.approval import ApprovalDecision, ApprovalRequest
from jimbo.core.cost import estimate_cost
from jimbo.core.events import Event
from jimbo.core.jsonl import JsonlSink
from jimbo.core.models import KNOWN_TASKS, MAIN, ModelRouter
from jimbo.core.trace import trace_page
from jimbo.providers.preflight import ModelFacts, inspect_model
from jimbo.providers.registry import all_providers, build_model_from_spec, provider_config
from jimbo.tools.sandbox import available as sandbox_available
from jimbo.wiring import build_session

from .grading import JUDGE_TASK, Outcome, check_events, judge, run_check
from .tasks import EVALS_DIR, Task

RESULTS_DIR = EVALS_DIR / "results"
EVAL_CHAIN = ["rules", "yolo"]  # deny rules still apply; everything else is allowed
BASE_DENY = ["shell:git push*", "shell:sudo *"]
PLAN_NOTE = ("This is an evaluation run: the plan is recorded and will not be carried out. "
             "Do not call exit_plan again; end your turn with a one-line summary.")
REPO_ROOT = EVALS_DIR.parent


@dataclass
class RunConfig:
    model: str | None = None  # main model: an alias or <provider>:<name>; None uses the config's
    task_models: dict[str, str] = field(default_factory=dict)  # like /models set
    trials: int = 1
    retry_failed_only: bool = False  # run a task's trials one after another, stopping at its first pass
    jobs: int = 4
    sandbox: bool = True
    network: bool = True
    keep: bool = False  # keep each trial's temp workspace
    requests_per_minute: int | None = None  # every provider's cap, replacing the config's; 0 removes it
    user_config: Path | None = None  # config.toml with providers and aliases; default ~/.jimbo/config.toml
    model_builder: object = None  # tests: a fake model builder for build_session and the judge
    main_model: object = None  # tests: a ready-made main model


class EvalFrontend:
    """Sends the prompts in order. Nobody answers approvals; only plan mode's exit_plan reaches here."""

    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)

    async def next_user_input(self) -> str | None:
        return self.inputs.pop(0) if self.inputs else None

    async def render(self, event: Event) -> None:
        pass

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        if req.tool == "exit_plan":
            return ApprovalDecision(allow=False, note=PLAN_NOTE)
        return ApprovalDecision(allow=False, note="not run: nobody is available to approve it")


class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def user_config_path(cfg: RunConfig) -> Path:
    return cfg.user_config or Path(os.environ.get(USER_DIR_ENV) or Path.home() / ".jimbo") / "config.toml"


def make_settings(cfg: RunConfig, task: Task | None, workspace: Path, user_dir: Path, log_dir: Path) -> Settings:
    """Settings for one trial. user_dir holds only a copy of the user's config.toml, so the user's skills,
    commands, agents and AGENTS.md don't change the results."""
    user_dir.mkdir(parents=True, exist_ok=True)
    if (src := user_config_path(cfg)).is_file() and not (user_dir / "config.toml").exists():
        shutil.copy(src, user_dir / "config.toml")
    s = Settings(workspace=workspace, user_dir=user_dir, log_dir=log_dir, ui="plain", approval=list(EVAL_CHAIN),
                 sandbox="on" if cfg.sandbox else "off", sandbox_network=cfg.network,
                 **(task.settings if task else {}))
    s.approval_deny = [*s.approval_deny, *BASE_DENY, *(task.deny if task else [])]
    if cfg.model:
        s.model_aliases[MAIN] = s.model_aliases.get(cfg.model, cfg.model)
    s.models.update(cfg.task_models)
    if cfg.requests_per_minute is not None:
        rpm = cfg.requests_per_minute or None
        for name in all_providers(s):
            user = s.providers.get(name)
            s.providers[name] = (user.model_copy(update={"requests_per_minute": rpm}) if user
                                 else ProviderConfig(requests_per_minute=rpm))
    return s


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=jimbo-evals", "-c", "user.email=evals@localhost",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def prepare_workspace(task: Task, ws: Path) -> None:
    shutil.copytree(task.repo, ws)
    git(ws, "init", "-q")
    (ws / ".git" / "info" / "exclude").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "fixture", "--allow-empty")


def workspace_diff(ws: Path) -> str:
    git(ws, "add", "-A")
    return git(ws, "diff", "--cached", "--binary", "HEAD")


def metrics(events: list[Event]) -> dict:
    tools = Counter(e.data.get("tool") for e in events if e.kind == "tool_call")
    errors = [e for e in events if e.kind == "error"]
    return {
        "tool_calls": dict(sorted(tools.items())),
        "tool_retries": sum(1 for e in events if e.kind == "tool_result" and not e.data.get("ok")),
        "denials": sum(1 for e in events if e.kind == "approval_resolved" and e.data.get("action") == "deny"),
        "wrap_up": any(e.kind == "wrap_up" for e in events),
        "hit_request_limit": any(e.data.get("type") == "UsageLimitExceeded" for e in errors),
        "compactions": sum(1 for e in events if e.kind == "compaction"),
    }


def final_answer(events: list[Event]) -> str:
    texts = [e.data.get("text", "") for e in events if e.kind == "assistant_text"]
    return texts[-1] if texts else ""


def plans(events: list[Event]) -> list[str]:
    """The plans given to exit_plan, which in plan mode hold the real answer."""
    return [str(e.data.get("args", {}).get("plan", "")) for e in events
            if e.kind == "tool_call" and e.data.get("tool") == "exit_plan"]


async def run_trial(task: Task, trial: int, cfg: RunConfig, out: Path, facts: ModelFacts | None = None) -> dict:
    """Run one trial and grade it. Writes the session log, trace, diff and check output under `out`."""
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"jimbo-eval-{task.id}-"))
    ws = tmp / "ws"
    collector = Collector()
    started = time.monotonic()
    error: str | None = None
    session = None
    try:
        await asyncio.to_thread(prepare_workspace, task, ws)
        settings = make_settings(cfg, task, ws, tmp / "home" / ".jimbo", out)
        sink = JsonlSink(out)
        try:
            session = build_session(settings, EvalFrontend([]), [collector, sink],
                                    model=cfg.main_model, model_builder=cfg.model_builder)
            session.prices = facts.prices if facts else None  # so session.jsonl carries costs too
            if facts and facts.context and settings.context_window is None:
                session.context_window = facts.context
            if facts and facts.vision is False:
                session.vision = False
            if task.mode == "plan":
                session.deps.mode = "plan"
            await session.emit_status(initial=True)
            deadline = started + task.timeout
            for prompt in [task.prompt, *task.followups]:
                run = asyncio.create_task(session.send(prompt))
                done, _ = await asyncio.wait({run}, timeout=max(deadline - time.monotonic(), 0))
                if not done:
                    session.cancel()
                    await run
                    error = "timeout"
                    break
                if run.result() is None:
                    errors = [e.data.get("message", "") for e in collector.events if e.kind == "error"]
                    error = (errors[-1] if errors else "run failed")[:500]
                    break
        finally:
            sink.close()
        diff = await asyncio.to_thread(workspace_diff, ws)
        (out / "diff.patch").write_text(diff)
        outcomes: list[Outcome] = []
        if checked := await run_check(task, ws):
            outcomes.append(checked)
        if evented := check_events(task, collector.events):
            outcomes.append(evented)
        if task.judge:
            outcomes.append(await judge(task, session.deps.models, final_answer(collector.events), diff,
                                        plans(collector.events)))
    except Exception as e:  # setup or grading broke: the trial fails, the run goes on
        error = f"{type(e).__name__}: {e}"[:500]
        outcomes = []
    finally:
        if not cfg.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    log = _name_log(out, session.session_id if session else None)
    text = "\n\n".join(o.text for o in outcomes)
    (out / "check.txt").write_text((f"error: {error}\n\n" if error else "") + text + "\n")
    usage = session.total_usage if session else None
    command_exit = next((o.exit for o in outcomes if o.exit is not None), None)
    return {
        "task": task.id, "trial": trial, "pass": error is None and bool(outcomes) and all(o.ok for o in outcomes),
        "check_exit": command_exit, "requests": usage.requests if usage else 0,
        "input_tokens": usage.input_tokens if usage else 0, "output_tokens": usage.output_tokens if usage else 0,
        "cost": estimate_cost(usage, facts.prices if facts else None),
        "seconds": round(time.monotonic() - started, 1), **metrics(collector.events), "error": error,
        "log": str(log) if log else None, "workspace": str(ws) if cfg.keep else None,
    }


def _name_log(out: Path, session_id: str | None) -> Path | None:
    """Rename the session log to session.jsonl and write trace.html beside it."""
    if session_id is None or not (src := out / f"{session_id}.jsonl").is_file():
        return None
    log = src.rename(out / "session.jsonl")
    (out / "trace.html").write_text(trace_page(log), "utf-8")
    return log


def short_name(spec: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", spec.rsplit("/", 1)[-1].rsplit(":", 1)[-1]).strip("-")[:40] or "model"


def jimbo_commit() -> dict:
    try:
        return {"commit": git(REPO_ROOT, "rev-parse", "HEAD").strip(),
                "dirty": bool(git(REPO_ROOT, "status", "--porcelain", "--", "src", "evals").strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


async def preflight(cfg: RunConfig) -> tuple[Settings, ModelFacts]:
    """Check the setup once before any trial: sandbox, main model and its key."""
    if cfg.sandbox and not sandbox_available():
        raise ValueError("no sandbox on this system: commands would run unconfined. Pass --no-sandbox to run anyway")
    tmp = Path(tempfile.mkdtemp(prefix="jimbo-eval-preflight-"))
    try:
        settings = make_settings(cfg, None, tmp, tmp / "home" / ".jimbo", tmp / "logs")
        if cfg.main_model is not None:
            return settings, ModelFacts()
        spec = settings.model_aliases.get(MAIN)
        if not spec:
            raise ValueError(f"no main model: pass --model, or set it in {user_config_path(cfg)}")
        facts = await inspect_model(spec, settings)
        if facts.problem:
            raise ValueError(facts.problem)
        return settings, facts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def model_specs(settings: Settings) -> dict[str, str]:
    router = ModelRouter(settings, build_model_from_spec)
    return {t: router.explain(t).spec for t in [MAIN, *KNOWN_TASKS, JUDGE_TASK, *settings.models]}


def request_limits(settings: Settings, specs: dict[str, str]) -> dict[str, int | None]:
    """The requests_per_minute cap of each provider the run's models use."""
    names = sorted({spec.partition(":")[0] for spec in specs.values() if ":" in spec})
    return {n: provider_config(n, settings).requests_per_minute for n in names}


async def run_suite(tasks: list[Task], cfg: RunConfig, results_dir: Path = RESULTS_DIR,
                    on_result=None) -> Path:
    """Run every trial of every task, up to cfg.jobs at once. Returns the run folder."""
    settings, facts = await preflight(cfg)
    specs = model_specs(settings) if cfg.main_model is None else {MAIN: "(test model)"}
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{short_name(specs[MAIN])}"
    run_dir = results_dir / run_id
    run_dir.mkdir(parents=True)
    meta = {"id": run_id, **jimbo_commit(), "models": specs, "model_arg": cfg.model, "trials": cfg.trials,
            "retry_failed_only": cfg.retry_failed_only, "sandbox": cfg.sandbox, "network": cfg.network, "requests_per_minute": request_limits(settings, specs),
            "tasks": [t.id for t in tasks],
            "task_settings": {t.id: t.settings for t in tasks if t.settings},
            "started": datetime.now().astimezone().isoformat(timespec="seconds")}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    gate = asyncio.Semaphore(cfg.jobs)
    lines = open(run_dir / "results.jsonl", "a", encoding="utf-8")

    async def one(task: Task, trial: int) -> dict:
        async with gate:
            r = await run_trial(task, trial, cfg, run_dir / task.id / str(trial), facts)
        lines.write(json.dumps(r) + "\n")
        lines.flush()
        if on_result:
            on_result(r)
        return r

    async def until_pass(task: Task) -> None:
        for n in range(1, cfg.trials + 1):
            if (await one(task, n))["pass"]:
                break

    try:
        if cfg.retry_failed_only:  # tasks still run in parallel; each task's trials run in turn
            await asyncio.gather(*(until_pass(t) for t in tasks))
        else:
            await asyncio.gather(*(one(t, n) for t in tasks for n in range(1, cfg.trials + 1)))
    finally:
        lines.close()
    meta["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    return run_dir
