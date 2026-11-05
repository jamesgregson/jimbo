"""uv run python -m evals {run,compare,check-task,assess,compaction}. See evals/README.md."""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from jimbo.providers.registry import build_model_from_spec

from .html_report import write_html_report
from .report import compare_report, find_run, run_report
from .runner import RunConfig, make_settings, run_suite
from .side_tasks import assess_eval, compaction_eval, load_compaction_cases, load_shell_cases
from .tasks import load_tasks
from .validate import check_task


def task_models(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        task, sep, model = p.partition("=")
        if not sep or not task or not model:
            raise ValueError(f"--set wants task=model, got {p!r}")
        out[task] = model
    return out


def config(args) -> RunConfig:
    return RunConfig(model=args.model, task_models=task_models(args.set), trials=getattr(args, "trials", 1),
                     jobs=args.jobs, sandbox=not getattr(args, "no_sandbox", False),
                     network=not getattr(args, "no_network", False), keep=getattr(args, "keep", False),
                     user_config=args.config)


def model_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", help="main model: an alias or <provider>:<name> (default: main in your config)")
    p.add_argument("--set", action="append", default=[], metavar="TASK=MODEL",
                   help="model for another task, as /models set does; e.g. evals-judge=strong")
    p.add_argument("--config", type=Path, help="config.toml to read providers and aliases from "
                                               "(default: ~/.jimbo/config.toml)")
    p.add_argument("--jobs", type=int, default=4, help="trials or cases at once (default 4)")


async def cmd_run(args) -> int:
    tasks = load_tasks(args.task or None, args.tag or None)
    if not tasks:
        raise ValueError("no tasks match")
    cfg = config(args)
    total = len(tasks) * cfg.trials
    done = {"n": 0}

    def progress(r: dict) -> None:
        done["n"] += 1
        why = f"  {r['error']}" if r.get("error") else ""
        print(f"[{done['n']}/{total}] {r['task']} #{r['trial']}: {'pass' if r['pass'] else 'FAIL'} "
              f"({r['requests']} requests, {r['seconds']:.0f}s){why}", file=sys.stderr, flush=True)

    print(f"running {len(tasks)} task(s) x {cfg.trials} trial(s)", file=sys.stderr)
    run_dir = await run_suite(tasks, cfg, on_result=progress)
    print(run_report(run_dir))
    print(f"\nreport: {write_html_report(run_dir)}")
    return 0


async def cmd_report(args) -> int:
    run_dir = find_run(args.run) if args.run else latest_run()
    print(run_report(run_dir))
    print(f"\nreport: {write_html_report(run_dir)}")
    return 0


def latest_run():
    from .runner import RESULTS_DIR
    runs = sorted(d for d in RESULTS_DIR.glob("*") if (d / "results.jsonl").is_file())
    if not runs:
        raise ValueError(f"no runs in {RESULTS_DIR}")
    return runs[-1]


async def cmd_compare(args) -> int:
    print(compare_report(find_run(args.a), find_run(args.b)))
    return 0


async def cmd_check_task(args) -> int:
    bad = 0
    for task in load_tasks(args.task or None):
        problems = await check_task(task)
        bad += bool(problems)
        status = "ok" if not problems else "FAIL"
        note = "" if task.check.command else " (no check command: parsed only)"
        print(f"{status}: {task.id}{note}")
        for p in problems:
            print("  " + p.replace("\n", "\n  "))
    return 1 if bad else 0


def router_for(args):
    from jimbo.core.models import ModelRouter
    tmp = Path(tempfile.mkdtemp(prefix="jimbo-eval-side-"))
    settings = make_settings(config(args), None, tmp, tmp / "home" / ".jimbo", tmp / "logs")
    return ModelRouter(settings, build_model_from_spec)


async def cmd_assess(args) -> int:
    if args.model:  # here --model is the assessment model, not main
        args.set.insert(0, f"jimbo-shell-assess={args.model}")
        args.model = None
    print(await assess_eval(router_for(args), load_shell_cases(), jobs=args.jobs))
    return 0


async def cmd_compaction(args) -> int:
    print(await compaction_eval(router_for(args), load_compaction_cases()))
    return 0


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    ap = argparse.ArgumentParser(prog="python -m evals", description="jimbo's eval suite")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run tasks and grade them")
    model_args(p)
    p.add_argument("--task", action="append", default=[], help="run this task (repeatable)")
    p.add_argument("--tag", action="append", default=[], help="run tasks with this tag (repeatable)")
    p.add_argument("--trials", type=int, default=1, help="attempts per task (default 1)")
    p.add_argument("--no-sandbox", action="store_true", help="run where there is no sandbox (not macOS)")
    p.add_argument("--no-network", action="store_true", help="block network access for shell commands")
    p.add_argument("--keep", action="store_true", help="keep each trial's workspace")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="print a run's table and write its report.html")
    p.add_argument("run", nargs="?", help="run id, start of an id, or folder (default: the latest run)")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("compare", help="compare two runs")
    p.add_argument("a", help="run id, start of an id, or folder")
    p.add_argument("b")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("check-task", help="check that tasks' checks fail unsolved and pass solved")
    p.add_argument("task", nargs="*")
    p.set_defaults(fn=cmd_check_task)

    p = sub.add_parser("assess", help="rate the shell commands in evals/assess/shell.toml")
    model_args(p)
    p.set_defaults(fn=cmd_assess)

    p = sub.add_parser("compaction", help="check which facts survive compaction")
    model_args(p)
    p.set_defaults(fn=cmd_compaction)

    args = ap.parse_args()
    try:
        raise SystemExit(asyncio.run(args.fn(args)))
    except ValueError as e:
        raise SystemExit(f"evals: {e}") from None


if __name__ == "__main__":
    main()
