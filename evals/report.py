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


def rate(rs: list[dict]) -> float:
    return sum(r["pass"] for r in rs) / len(rs) if rs else 0.0


def passes(rs: list[dict]) -> str:
    n = sum(r["pass"] for r in rs)
    return ("pass" if n else "FAIL") if len(rs) == 1 else f"{n}/{len(rs)}"


def run_report(run_dir: Path) -> str:
    results = load_results(run_dir)
    groups = by_task(results)
    rows = [[task, passes(rs), f"{mean(rs, 'requests'):.1f}", f"{sum(map(tokens, rs)) / len(rs):,.0f}",
             cost(rs), f"{mean(rs, 'seconds'):.0f}s"] for task, rs in groups.items()]
    out = [table(["task", "pass", "requests", "tokens", "cost", "time"], rows)]
    n = sum(r["pass"] for r in results)
    out.append(f"\n{n}/{len(results)} trials passed ({100 * n / max(len(results), 1):.0f}%), "
               f"{sum(map(tokens, results)):,} tokens, {cost(results, total=True)} estimated, run {run_dir.name}")
    failed = [r for r in results if not r["pass"]]
    if failed:
        out.append("\nFailed trials:")
        for r in sorted(failed, key=lambda r: (r["task"], r["trial"])):
            trial = run_dir / r["task"] / str(r["trial"])
            why = f" ({r['error']})" if r.get("error") else ""
            out.append(f"  {r['task']} #{r['trial']}{why}\n    {trial / 'check.txt'}\n    {trial / 'trace.html'}")
    return "\n".join(out)


def compare_report(a_dir: Path, b_dir: Path) -> str:
    a, b = by_task(load_results(a_dir)), by_task(load_results(b_dir))
    shared = sorted(a.keys() & b.keys(), key=lambda t: (rate(b[t]) - rate(a[t]) >= 0, t))  # worse first
    rows = []
    for t in shared:
        d = rate(b[t]) - rate(a[t])
        rows.append([t, passes(a[t]), passes(b[t]), f"{d:+.0%}" if d else "",
                     f"{mean(a[t], 'requests'):.1f}", f"{mean(b[t], 'requests'):.1f}",
                     f"{sum(map(tokens, a[t])) / len(a[t]):,.0f}", f"{sum(map(tokens, b[t])) / len(b[t]):,.0f}",
                     cost(a[t]), cost(b[t])])
    out = [f"A: {a_dir.name}\nB: {b_dir.name}\n",
           table(["task", "A pass", "B pass", "change", "A req", "B req", "A tokens", "B tokens", "A cost", "B cost"],
                 rows)]
    ra = [r for t in shared for r in a[t]]
    rb = [r for t in shared for r in b[t]]
    out.append(f"\nshared tasks: A {rate(ra):.0%}, B {rate(rb):.0%} of trials passed; "
               f"tokens per trial A {sum(map(tokens, ra)) / max(len(ra), 1):,.0f}, "
               f"B {sum(map(tokens, rb)) / max(len(rb), 1):,.0f}; estimated cost A {cost(ra, total=True)}, "
               f"B {cost(rb, total=True)}")
    for name, only in (("A", a.keys() - b.keys()), ("B", b.keys() - a.keys())):
        if only:
            out.append(f"only in {name}: {', '.join(sorted(only))}")
    return "\n".join(out)
