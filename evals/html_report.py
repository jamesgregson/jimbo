"""report.html: a run's results as a page, with each trial's checks, judge verdict, diff and a link to its trace."""

import html
import json
from pathlib import Path

from .report import by_task, cost, load_results, mean, passes, tokens
from .tasks import TASKS_DIR, Task, load_task

DIFF_LIMIT = 20_000  # characters of a diff shown inline

STYLE = """
:root { --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e3e3e8; --panel: #f6f6f8;
        --pass: #1a7f37; --fail: #c62828; --link: #0b5cad; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161618; --fg: #ececf1; --muted: #9a9aa3; --line: #2e2e33; --panel: #1f1f23;
          --pass: #4ac26b; --fail: #ff6b6b; --link: #6cb6ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 8px; }
h3 { font-size: 15px; margin: 0; display: inline; }
a { color: var(--link); }
.muted { color: var(--muted); }
.pass { color: var(--pass); font-weight: 600; }
.fail { color: var(--fail); font-weight: 600; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
.meta td:first-child { color: var(--muted); width: 160px; }
.task { border: 1px solid var(--line); border-radius: 8px; margin: 12px 0; padding: 12px 16px; }
.task > p { margin: 6px 0; }
.trial { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 10px; }
.links a { margin-right: 14px; }
pre { background: var(--panel); border-radius: 6px; padding: 10px 12px; overflow-x: auto;
      white-space: pre-wrap; word-break: break-word; font: 12px/1.45 ui-monospace, Menlo, monospace; margin: 6px 0; }
details > summary { cursor: pointer; color: var(--muted); margin: 6px 0; }
.wrap { overflow-x: auto; }
"""


def esc(text) -> str:
    return html.escape(str(text))


def status(ok: bool, label: str | None = None) -> str:
    return f'<span class="{"pass" if ok else "fail"}">{esc(label or ("pass" if ok else "FAIL"))}</span>'


def read(path: Path, limit: int | None = None) -> str | None:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    return text if limit is None or len(text) <= limit else text[:limit] + "\n[cut]"


def task_info(task_id: str) -> Task | None:
    """The task as it is now. It may have changed since the run."""
    try:
        return load_task(TASKS_DIR / task_id)
    except (OSError, ValueError):
        return None


def trial_html(run_dir: Path, r: dict) -> str:
    folder = Path(r["task"]) / str(r["trial"])
    links = [(name, folder / name) for name in ("trace.html", "check.txt", "diff.patch", "session.jsonl")
             if (run_dir / folder / name).is_file()]
    stats = (f'{r["requests"]} requests · {tokens(r):,} tokens · {cost([r])} · {r["seconds"]:.0f}s · '
             f'{sum(r.get("tool_calls", {}).values())} tool calls, {r.get("tool_retries", 0)} failed · '
             f'{r.get("denials", 0)} denied')
    parts = [f'<div class="trial" id="{esc(r["task"])}-{r["trial"]}">',
             f'<p>Trial {r["trial"]}: {status(r["pass"])} <span class="muted">· {esc(stats)}</span></p>']
    if r.get("error"):
        parts.append(f'<p class="fail">Error: {esc(r["error"])}</p>')
    parts.append('<p class="links">' + "".join(f'<a href="{esc(p.as_posix())}">{esc(n)}</a>' for n, p in links) + "</p>")
    check = read(run_dir / folder / "check.txt")
    if check and check.strip():
        parts.append(f"<pre>{esc(check.strip())}</pre>")
    diff = read(run_dir / folder / "diff.patch", DIFF_LIMIT)
    if diff:
        parts.append(f"<details><summary>Changes ({diff.count(chr(10))} lines)</summary><pre>{esc(diff)}</pre></details>")
    parts.append("</div>")
    return "\n".join(parts)


def task_html(run_dir: Path, task_id: str, rs: list[dict]) -> str:
    task = task_info(task_id)
    ok = all(r["pass"] for r in rs)
    parts = [f'<section class="task" id="{esc(task_id)}">', f"<h3>{esc(task_id)}</h3> {status(ok, passes(rs))}"]
    if task:
        tags = ", ".join(task.tags)
        parts.append(f'<p class="muted">{esc(tags)}{" · plan mode" if task.mode == "plan" else ""}'
                     f'{" · fixture " + esc(task.fixture) if task.fixture else ""}</p>')
        parts.append(f"<p><b>Prompt</b></p><pre>{esc(chr(10).join([task.prompt, *task.followups]))}</pre>")
        if task.check.command:
            parts.append(f"<p><b>Check command</b></p><pre>{esc(task.check.command)}</pre>")
        if task.judge:
            parts.append(f"<p><b>Rubric</b></p><pre>{esc(task.judge.rubric.strip())}</pre>")
    else:
        parts.append('<p class="muted">task.toml not found: the task was removed or renamed after this run.</p>')
    parts += [trial_html(run_dir, r) for r in rs]
    parts.append("</section>")
    return "\n".join(parts)


def write_html_report(run_dir: Path) -> Path:
    results = load_results(run_dir)
    groups = by_task(results)
    try:
        meta = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        meta = {}
    n = sum(r["pass"] for r in results)
    failed_first = sorted(groups, key=lambda t: (all(r["pass"] for r in groups[t]), t))
    rows = "\n".join(
        f'<tr><td><a href="#{esc(t)}">{esc(t)}</a></td><td>{status(all(r["pass"] for r in rs), passes(rs))}</td>'
        f'<td class="n">{mean(rs, "requests"):.1f}</td><td class="n">{sum(map(tokens, rs)) / len(rs):,.0f}</td>'
        f'<td class="n">{cost(rs)}</td><td class="n">{mean(rs, "seconds"):.0f}s</td></tr>'
        for t in failed_first for rs in [groups[t]])
    models = meta.get("models", {})
    meta_rows = [("Passed", f"{n}/{len(results)} trials ({100 * n / max(len(results), 1):.0f}%)"),
                 ("Tokens", f"{sum(map(tokens, results)):,}"), ("Estimated cost", cost(results, total=True)),
                 ("Main model", models.get("main", "?")), ("Judge model", models.get("evals-judge", "?")),
                 ("Trials per task", meta.get("trials", "?")),
                 ("jimbo commit", f'{(meta.get("commit") or "?")[:12]}{" (with uncommitted changes)" if meta.get("dirty") else ""}'),
                 ("Started", meta.get("started", "?")), ("Finished", meta.get("finished", "not finished"))]
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eval run {esc(run_dir.name)}</title><style>{STYLE}</style></head>
<body><main>
<h1>Eval run {esc(run_dir.name)}</h1>
<p class="muted">Each trial links to its trace, check output, diff and session log. Task prompts and rubrics are
read from evals/tasks/ as they are now.</p>
<table class="meta">{"".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in meta_rows)}</table>
<h2>Tasks</h2>
<div class="wrap"><table><tr><th>task</th><th>pass</th><th class="n">requests</th><th class="n">tokens</th>
<th class="n">cost</th><th class="n">time</th></tr>
{rows}</table></div>
<h2>Details</h2>
{"".join(task_html(run_dir, t, groups[t]) for t in failed_first)}
</main></body></html>
"""
    out = run_dir / "report.html"
    out.write_text(page, "utf-8")
    return out
