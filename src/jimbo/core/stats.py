"""One-line rendering of the `context` event, shared by the text frontends."""


def _n(v: int) -> str:
    return f"{v / 1000:.1f}k" if v >= 1000 else str(v)


def format_stats(d: dict) -> str:
    """e.g. `ctx 3.2k/32k (10%) · turn 1.1k in 200 out · session 9.0k in 1.2k out · 7 req`."""
    ctx = f"ctx {_n(d['tokens'])}"
    if d.get("window"):
        ctx += f"/{_n(d['window'])} ({100 * d['tokens'] / d['window']:.0f}%)"
    t, s = d["turn"], d["session"]
    return (f"{ctx} · turn {_n(t['input_tokens'])} in {_n(t['output_tokens'])} out"
            f" · session {_n(s['input_tokens'])} in {_n(s['output_tokens'])} out · {s['requests']} req")
