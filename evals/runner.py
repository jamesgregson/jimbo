"""Running trials: one task, one attempt, in its own temp workspace with its own session."""

import asyncio
import inspect
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
from jimbo.core.stats import thinking_tokens
from jimbo.core.trace import trace_page
from jimbo.providers.preflight import ModelFacts, inspect_model
from jimbo.providers.registry import all_providers, build_model_from_spec, provider_config
from jimbo.tools import container_sandbox
from jimbo.tools.sandbox import available as sandbox_available
from jimbo.tools.shell import kill_background
from jimbo.wiring import build_session

from .grading import JUDGE_TASK, Outcome, check_events, judge, run_check
from .tasks import EVALS_DIR, Suite, Task, resolve

RESULTS_DIR = EVALS_DIR / "results"
# --sandbox -> the sandbox setting
SANDBOX_MODES = {"seatbelt": "on", "docker": "docker", "podman": "podman", "off": "off"}
EVAL_CHAIN = ["rules", "yolo"]  # deny rules still apply; everything else is allowed
BASE_DENY = ["shell:git push*", "shell:sudo *"]
PLAN_NOTE = ("This is an evaluation run: the plan is recorded and will not be carried out. "
             "Do not call exit_plan again; end your turn with a one-line summary.")
REPO_ROOT = EVALS_DIR.parent
DEFAULT_JOBS = 4
GRADE_MARK = "--- suite grade ---"  # in check.txt: what follows is the suite grader's output


@dataclass
class RunConfig:
    model: str | None = None  # main model: an alias or <provider>:<name>; None uses the config's
    task_models: dict[str, str] = field(default_factory=dict)  # like /models set
    trials: int | None = None  # None: each suite's default, else 1
    retry_failed_only: bool = False  # run a task's trials one after another, stopping at its first pass
    jobs: int | None = None  # None: the smallest of the suites' defaults, else 4
    sandbox: str = "seatbelt"  # seatbelt, docker or podman (a container per trial), or off
    network: bool = True
    lsp: bool = True  # False: no language servers, to compare runs with and without them
    keep: bool = False  # keep each trial's temp workspace
    requests_per_minute: int | None = None  # every provider's cap, replacing the config's; 0 removes it
    user_config: Path | None = None  # config.toml with providers and aliases; default ~/.jimbo/config.toml
    model_builder: object = None  # tests: a fake model builder for build_session and the judge
    main_model: object = None  # tests: a ready-made main model


def trials_for(task: Task, cfg: RunConfig) -> int:
    return cfg.trials or (task.suite and task.suite.defaults.trials) or 1


def jobs_for(suites: list[Suite], cfg: RunConfig) -> int:
    given = [s.defaults.jobs for s in suites if s.defaults.jobs]
    return cfg.jobs or (min(given) if given else DEFAULT_JOBS)


def suites_of(tasks: list[Task]) -> list[Suite]:
    return list({t.suite.name: t.suite for t in tasks if t.suite}.values())


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
                 sandbox=SANDBOX_MODES[cfg.sandbox], sandbox_network=cfg.network, lsp=cfg.lsp,
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


WORKSPACE_VAR = "${WORKSPACE}"  # in a fixture's compile_commands.json: the trial's workspace, which must be absolute


def prepare_workspace(task: Task, ws: Path) -> None:
    shutil.copytree(task.repo, ws)
    if (cdb := ws / "compile_commands.json").is_file():
        cdb.write_text(cdb.read_text().replace(WORKSPACE_VAR, str(ws.resolve())))
    git(ws, "init", "-q")
    # .cache/: clangd's index
    (ws / ".git" / "info" / "exclude").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.cache/\n")
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
        session = None
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
            if session is not None:
                await kill_background(session.deps)
                if session.deps.lsp:
                    await session.deps.lsp.close()
            sink.close()
        diff = await asyncio.to_thread(workspace_diff, ws)
        (out / "diff.patch").write_text(diff)
        outcomes: list[Outcome] = []
        # In the trial's sandbox: the check runs code the model wrote, and sees what the model's commands built.
        if checked := await run_check(task, ws, session.deps.sandbox):
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
        if session is not None and session.deps.sandbox:
            await session.deps.sandbox.close()
        if not cfg.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    log = _name_log(out, session.session_id if session else None)
    text = "\n\n".join(o.text for o in outcomes)
    (out / "check.txt").write_text((f"error: {error}\n\n" if error else "") + text + "\n")
    usage = session.total_usage if session else None
    command_exit = next((o.exit for o in outcomes if o.exit is not None), None)
    checked = all(o.ok for o in outcomes) if outcomes else None  # None: the task has no checks of its own
    if error is not None:
        passed = False
    elif task.suite_grade:
        passed = None if checked is not False else False  # pending until the suite's grader runs
    else:
        passed = bool(checked)
    return {
        "task": task.id, "suite": task.suite_name, "trial": trial, "pass": passed, "check_pass": checked,
        "grade": task.suite_grade, "check_exit": command_exit, "requests": usage.requests if usage else 0,
        "input_tokens": usage.input_tokens if usage else 0, "output_tokens": usage.output_tokens if usage else 0,
        "thinking_tokens": thinking_tokens(usage) if usage else 0,
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


def suite_image(suite: Suite, settings: Settings) -> str:
    """The image a suite's trials run in with --sandbox docker or podman."""
    return suite.defaults.settings.get("sandbox_image") or settings.sandbox_image or container_sandbox.bundled_image()


def missing_requirements(suites: list[Suite], cfg: RunConfig, settings: Settings, find=None) -> list[str]:
    """What each suite's [requires] asks for that this run doesn't have. `find(image, names)` gives the commands
    found in a container image (default: container_sandbox.find_commands)."""
    container = cfg.sandbox in ("docker", "podman")
    find = find or (lambda image, names: container_sandbox.find_commands(image, names, cfg.sandbox))
    missing = []
    for s in suites:
        req = s.requires
        if req.sandbox and cfg.sandbox not in req.sandbox:
            missing.append(f"{s.name}: needs --sandbox {' or '.join(req.sandbox)}")
        if req.network and not cfg.network:
            missing.append(f"{s.name}: needs network access; leave out --no-network")
        if req.commands:
            if container:
                image = suite_image(s, settings)
                found, where = find(image, req.commands), f"in the image {image}"
            else:
                found, where = {c for c in req.commands if shutil.which(c)}, "on PATH"
            if lost := [c for c in req.commands if c not in found]:
                missing.append(f"{s.name}: needs {', '.join(lost)} {where}")
    return missing


async def preflight(cfg: RunConfig, suites: list[Suite] = ()) -> tuple[Settings, ModelFacts]:
    """Check the setup once before any trial: sandbox, the suites' requirements, main model and its key."""
    if cfg.sandbox == "seatbelt" and not sandbox_available():
        raise ValueError("no sandbox-exec on this system: use --sandbox docker or podman, or --sandbox off to run commands "
                         "unconfined")
    if cfg.sandbox in ("docker", "podman"):
        if why := container_sandbox.unavailable(cfg.sandbox):
            raise ValueError(f"--sandbox {cfg.sandbox} but {cfg.sandbox.capitalize()} is not available: {why}")
        await asyncio.to_thread(container_sandbox.ensure_image, container_sandbox.bundled_image(), cfg.sandbox)
    tmp = Path(tempfile.mkdtemp(prefix="jimbo-eval-preflight-"))
    try:
        settings = make_settings(cfg, None, tmp, tmp / "home" / ".jimbo", tmp / "logs")
        if cfg.sandbox in ("docker", "podman"):
            for image in {suite_image(s, settings) for s in suites if s.requires.commands}:
                await asyncio.to_thread(container_sandbox.ensure_image, image, cfg.sandbox)
        if missing := await asyncio.to_thread(missing_requirements, list(suites), cfg, settings):
            raise ValueError("the run's suites need:\n  " + "\n  ".join(missing))
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


def read_results(run_dir: Path) -> list[dict]:
    with open(run_dir / "results.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def suite_meta(suite: Suite, tasks: list[Task], cfg: RunConfig) -> dict:
    mine = [t for t in tasks if t.suite_name == suite.name]
    return {**suite.model_dump(mode="json", exclude={"name", "dir"}),
            "trials": max((trials_for(t, cfg) for t in mine), default=None)}


async def run_suite(tasks: list[Task], cfg: RunConfig, results_dir: Path = RESULTS_DIR,
                    on_result=None) -> Path:
    """Run every trial of every task, up to the run's jobs at once, then the suites' graders. Returns the run
    folder."""
    suites = suites_of(tasks)
    settings, facts = await preflight(cfg, suites)
    specs = model_specs(settings) if cfg.main_model is None else {MAIN: "(test model)"}
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{short_name(specs[MAIN])}"
    run_dir = results_dir / run_id
    run_dir.mkdir(parents=True)
    trial_counts = {trials_for(t, cfg) for t in tasks}
    meta = {"id": run_id, **jimbo_commit(), "models": specs, "model_arg": cfg.model,
            "trials": trial_counts.pop() if len(trial_counts) == 1 else "per suite",
            "retry_failed_only": cfg.retry_failed_only, "sandbox": cfg.sandbox, "network": cfg.network, "lsp": cfg.lsp, "requests_per_minute": request_limits(settings, specs),
            "suites": {s.name: suite_meta(s, tasks, cfg) for s in suites},
            "tasks": [t.id for t in tasks],
            "task_settings": {t.id: t.settings for t in tasks if t.settings},
            "started": datetime.now().astimezone().isoformat(timespec="seconds")}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    gate = asyncio.Semaphore(jobs_for(suites, cfg))
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
        for n in range(1, trials_for(task, cfg) + 1):
            if (await one(task, n))["pass"]:
                break

    try:
        if cfg.retry_failed_only:  # tasks still run in parallel; each task's trials run in turn
            await asyncio.gather(*(until_pass(t) for t in tasks))
        else:
            await asyncio.gather(*(one(t, n) for t in tasks for n in range(1, trials_for(t, cfg) + 1)))
    finally:
        lines.close()
    if any(s.grade for s in suites):
        await grade_run(run_dir, {s.name: s for s in suites})
    meta = json.loads((run_dir / "run.json").read_text())
    meta["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    return run_dir


def _write_grade_text(check: Path, text: str) -> None:
    """Put the grader's output at the end of check.txt, replacing what an earlier grading put there."""
    old = check.read_text() if check.is_file() else ""
    old = old.split(GRADE_MARK, 1)[0].rstrip()
    check.write_text((old + "\n\n" if old else "") + f"{GRADE_MARK}\n{text.strip()}\n")


async def grade_run(run_dir: Path, suites: dict[str, Suite]) -> list[str]:
    """Run the grader of each suite in `suites` that has one, on that suite's trials in the run, and write the
    results into results.jsonl and check.txt. A trial the grader gives no result for keeps its pass value.
    Returns the graders' errors, which are also saved as grade_errors in run.json."""
    rows = read_results(run_dir)
    errors: list[str] = []
    for name, suite in suites.items():
        mine = [r for r in rows if (r.get("suite") or "core") == name]
        if not suite.grade or not mine:
            continue
        try:
            fn = resolve(suite.grade)
            if inspect.iscoroutinefunction(fn):
                graded = await fn(suite, run_dir)
            else:
                graded = await asyncio.to_thread(fn, suite, run_dir)
            got = {(g["task"], int(g["trial"])): g for g in graded}
        except Exception as e:  # the trials stay pending; `grade <run>` can try again
            errors.append(f"{name}: {suite.grade} failed: {type(e).__name__}: {e}"[:1000])
            continue
        for r in mine:
            if (g := got.get((r["task"], r["trial"]))) is None:
                continue
            r["grade"] = suite.grade
            r["grade_pass"] = bool(g["pass"])
            r["pass"] = r.get("error") is None and r.get("check_pass") is not False and r["grade_pass"]
            _write_grade_text(run_dir / r["task"] / str(r["trial"]) / "check.txt",
                              f"{suite.grade}: {'pass' if g['pass'] else 'FAIL'}\n{g.get('text', '')}")
    tmp = run_dir / "results.jsonl.tmp"
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    tmp.replace(run_dir / "results.jsonl")
    try:
        meta = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        meta = {}
    meta["grade_errors"] = errors
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    return errors
