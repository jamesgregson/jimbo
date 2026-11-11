"""Tables for a run and for the comparison of two runs."""

import json
from collections import defaultdict
from pathlib import Path

from .runner import RESULTS_DIR


def table(header: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    fmt = lambda r: "  ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w)
                              for i, (c, w) in enumerate(zip(r, widths))).rstrip()
    return "\n".join([fmt(header), fmt(["-" * w for w in widths]), *map(fmt, rows)])


def find_run(name: str, results_dir: Path = RESULTS_DIR) -> Path:
    """A run folder by path, id, or the start of an id (the latest match)."""
    p = Path(name)
    if (p / "results.jsonl").is_file():
        return p
    found = sorted(d for d in results_dir.glob(f"{name}*") if (d / "results.jsonl").is_file())
    if not found:
        raise ValueError(f"no run {name!r} in {results_dir}")
    return found[-1]


def load_results(run_dir: Path) -> list[dict]:
    with open(run_dir / "results.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def suite_of(r: dict) -> str:
    """A trial's suite. Runs from before suites have none: their tasks are all in core now."""
    return r.get("suite") or "core"


def by_suite(groups: dict[str, list[dict]]) -> dict[str, dict[str, list[dict]]]:
    out: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for task, rs in groups.items():
        out[suite_of(rs[0])][task] = rs
    return dict(sorted(out.items()))


def graded(rs: list[dict]) -> list[dict]:
    """The trials that have a result: not those waiting for their suite's grader."""
    return [r for r in rs if r["pass"] is not None]


def pending(rs: list[dict]) -> list[dict]:
    return [r for r in rs if r["pass"] is None]


def by_task(results: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(results, key=lambda r: (r["task"], r["trial"])):
        out[r["task"]].append(r)
    return dict(out)


def mean(rs: list[dict], key: str) -> float:
    return sum(r[key] for r in rs) / len(rs) if rs else 0.0


def tokens(r: dict) -> int:
    return r["input_tokens"] + r["output_tokens"]


def cost(rs: list[dict], total: bool = False) -> str:
    """Mean (or total) estimated cost in USD of the trials that have one; "-" when none do."""
    known = [r["cost"] for r in rs if r.get("cost") is not None]
    if not known:
        return "-"
    return f"${sum(known) if total else sum(known) / len(known):.4f}"


def load_meta(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        return {}


def retried(run_dir: Path) -> bool:
    """Whether the run used --retry-failed-only: then a task passes if any of its trials does."""
    return bool(load_meta(run_dir).get("retry_failed_only"))


def task_passed(rs: list[dict], retry: bool = False) -> bool | None:
    """Whether the task passed; None while all its trials are pending."""
    if not (rs := graded(rs)):
        return None
    return any(r["pass"] for r in rs) if retry else all(r["pass"] for r in rs)


def rate(rs: list[dict], retry: bool = False) -> float:
    """The share of graded trials that passed; with retry, 1 or 0 for whether the task passed."""
    rs = graded(rs)
    if retry:
        return float(any(r["pass"] for r in rs))
    return sum(r["pass"] for r in rs) / len(rs) if rs else 0.0


def passes(rs: list[dict], retry: bool = False) -> str:
    waiting = len(pending(rs))
    if waiting:
        rs = graded(rs)
        if not rs:
            return "pending" if waiting == 1 else f"pending x{waiting}"
        return f"{passes(rs, retry)} +{waiting} pending"
    if retry:
        first = next((r["trial"] for r in rs if r["pass"]), None)
        if first is None:
            return "FAIL" if len(rs) == 1 else f"FAIL x{len(rs)}"
        return "pass" if first == 1 else f"pass on #{first}"
    n = sum(r["pass"] for r in rs)
    return ("pass" if n else "FAIL") if len(rs) == 1 else f"{n}/{len(rs)}"


def overall_rate(groups: dict[str, list[dict]], retry: bool) -> float:
    """The share of tasks that passed with retry, else of trials. Pending trials don't count."""
    if retry:
        done = [rs for rs in groups.values() if graded(rs)]
        return sum(rate(rs, True) for rs in done) / max(len(done), 1)
    return rate([r for rs in groups.values() for r in rs])


def passed_line(groups: dict[str, list[dict]], retry: bool) -> str:
    """'3/4 trials passed (75%)', or with retry '2/3 tasks passed (67%) in 5 trials'; then ', 2 pending'."""
    waiting = sum(len(pending(rs)) for rs in groups.values())
    more = f", {waiting} pending" if waiting else ""
    if retry:
        done = [rs for rs in groups.values() if graded(rs)]
        trials = sum(len(graded(rs)) for rs in done)
        n = sum(bool(task_passed(rs, True)) for rs in done)
        return f"{n}/{len(done)} tasks passed ({100 * n / max(len(done), 1):.0f}%) in {trials} trials{more}"
    trials = sum(len(graded(rs)) for rs in groups.values())
    n = sum(r["pass"] for rs in groups.values() for r in graded(rs))
    return f"{n}/{trials} trials passed ({100 * n / max(trials, 1):.0f}%){more}"


def suite_lines(groups: dict[str, list[dict]], retry: bool) -> list[str]:
    """A pass line per suite, when the run has more than one suite."""
    suites = by_suite(groups)
    return [f"{name}: {passed_line(g, retry)}" for name, g in suites.items()] if len(suites) > 1 else []


def run_report(run_dir: Path) -> str:
    results = load_results(run_dir)
    groups = by_task(results)
    retry = retried(run_dir)
    rows = [[task, suite_of(rs[0]), passes(rs, retry), f"{mean(rs, 'requests'):.1f}",
             f"{sum(map(tokens, rs)) / len(rs):,.0f}", cost(rs), f"{mean(rs, 'seconds'):.0f}s"]
            for task, rs in groups.items()]
    out = [table(["task", "suite", "pass", "requests", "tokens", "cost", "time"], rows)]
    if lines := suite_lines(groups, retry):
        out.append("\n" + "\n".join(lines))
    out.append(f"\n{passed_line(groups, retry)}, "
               f"{sum(map(tokens, results)):,} tokens, {cost(results, total=True)} estimated, run {run_dir.name}")
    failed = [r for r in results if r["pass"] is False]
    if failed:
        out.append("\nFailed trials:")
        for r in sorted(failed, key=lambda r: (r["task"], r["trial"])):
            trial = run_dir / r["task"] / str(r["trial"])
            why = f" ({r['error']})" if r.get("error") else ""
            out.append(f"  {r['task']} #{r['trial']}{why}\n    {trial / 'check.txt'}\n    {trial / 'trace.html'}")
    if waiting := pending(results):
        out.append(f"\nPending trials, waiting for their suite's grader: {len(waiting)}. "
                   f"Run: uv run python -m evals grade {run_dir.name}")
    for e in load_meta(run_dir).get("grade_errors") or []:
        out.append(f"grader error: {e}")
    return "\n".join(out)


def compare_report(a_dir: Path, b_dir: Path) -> str:
    a, b = by_task(load_results(a_dir)), by_task(load_results(b_dir))
    a_retry, b_retry = retried(a_dir), retried(b_dir)
    change = lambda t: rate(b[t], b_retry) - rate(a[t], a_retry)
    shared = sorted(a.keys() & b.keys(), key=lambda t: (change(t) >= 0, t))  # worse first
    rows = []
    for t in shared:
        d = change(t)
        rows.append([t, passes(a[t], a_retry), passes(b[t], b_retry), f"{d:+.0%}" if d else "",
                     f"{mean(a[t], 'requests'):.1f}", f"{mean(b[t], 'requests'):.1f}",
                     f"{sum(map(tokens, a[t])) / len(a[t]):,.0f}", f"{sum(map(tokens, b[t])) / len(b[t]):,.0f}",
                     cost(a[t]), cost(b[t])])
    out = [f"A: {a_dir.name}\nB: {b_dir.name}\n",
           table(["task", "A pass", "B pass", "change", "A req", "B req", "A tokens", "B tokens", "A cost", "B cost"],
                 rows)]
    ra = [r for t in shared for r in a[t]]
    rb = [r for t in shared for r in b[t]]
    what = lambda retry: "of tasks" if retry else "of trials"
    out.append(f"\nshared tasks: A {overall_rate({t: a[t] for t in shared}, a_retry):.0%} {what(a_retry)}, "
               f"B {overall_rate({t: b[t] for t in shared}, b_retry):.0%} {what(b_retry)} passed; "
               f"tokens per trial A {sum(map(tokens, ra)) / max(len(ra), 1):,.0f}, "
               f"B {sum(map(tokens, rb)) / max(len(rb), 1):,.0f}; estimated cost A {cost(ra, total=True)}, "
               f"B {cost(rb, total=True)}")
    sa, sb = by_suite({t: a[t] for t in shared}), by_suite({t: b[t] for t in shared})
    if len(sa) > 1:
        for name in sa:
            out.append(f"  {name}: A {overall_rate(sa[name], a_retry):.0%}, B {overall_rate(sb[name], b_retry):.0%}")
    for name, only in (("A", a.keys() - b.keys()), ("B", b.keys() - a.keys())):
        if only:
            out.append(f"only in {name}: {', '.join(sorted(only))}")
    return "\n".join(out)
