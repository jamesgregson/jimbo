"""One-line rendering of the `context` event, shared by the text frontends."""


def agent_prefix(d: dict) -> str:
    """Who an event came from, if an agent: `reviewer (ollama:qwen3) › `."""
    a = d.get("agent")
    return f"{a['name']} ({a['model']}) › " if a else ""


def _n(v: int) -> str:
    return f"{v / 1000:.1f}k" if v >= 1000 else str(v)


def output_preview(content: str, lines: int = 10) -> list[str]:
    """The last `lines` of a shell result's output (its first line is the exit status)."""
    body = content.strip().splitlines()[1:]
    if len(body) <= lines:
        return body
    return [f"… {len(body) - lines} earlier lines (/output shows them)", *body[-lines:]]


def format_stats(d: dict) -> str:
    """e.g. `ctx 3.2k/32k (10%) · turn 1.1k in 200 out · session 9.0k in 1.2k out · 7 req`."""
    ctx = f"ctx {_n(d['tokens'])}"
    if d.get("window"):
        ctx += f"/{_n(d['window'])} ({100 * d['tokens'] / d['window']:.0f}%)"
    t, s = d["turn"], d["session"]
    return (f"{ctx} · turn {_n(t['input_tokens'])} in {_n(t['output_tokens'])} out"
            f" · session {_n(s['input_tokens'])} in {_n(s['output_tokens'])} out · {s['requests']} req")
