"""One-line rendering of the `context` event, shared by the text frontends."""


def agent_prefix(d: dict) -> str:
    """Who an event came from, if an agent: `reviewer (ollama:qwen3) › `."""
    a = d.get("agent")
    return f"{a['name']} ({a['model']}) › " if a else ""


def _n(v: int) -> str:
    return f"{v / 1000:.1f}k" if v >= 1000 else str(v)


def thinking_tokens(usage) -> int:
    """Reasoning tokens in a RunUsage (already counted in its output tokens). pydantic-ai fills
    `output_reasoning_tokens` for providers it knows; OpenAI-compatible ones leave `reasoning_tokens` in `details`.
    When both are set they count the same tokens."""
    return max(usage.output_reasoning_tokens, usage.details.get("reasoning_tokens", 0))


def _out(u: dict) -> str:
    """`200 out`, or `200 out (150 thinking)` when the model reported reasoning tokens."""
    thinking = u.get("thinking_tokens", 0)
    return f"{_n(u['output_tokens'])} out" + (f" ({_n(thinking)} thinking)" if thinking else "")


def output_preview(content: str, lines: int = 10) -> list[str]:
    """The last `lines` of a shell result's output (its first line is the exit status)."""
    body = content.strip().splitlines()[1:]
    if len(body) <= lines:
        return body
    return [f"… {len(body) - lines} earlier lines (/output shows them)", *body[-lines:]]


def format_stats(d: dict) -> str:
    """e.g. `turn 3, cycle 7/50 · ctx 3.2k/32k (10%) · this turn 1.1k in 200 out · session 9.0k in 1.2k out · 7 req`.
    A cycle is one model request: the model thinks, then calls tools or answers."""
    cycles = f"turn {d['turns']}, cycle {d['turn']['requests']}/{d['limit']} · " if d.get("turns") else ""
    ctx = f"{cycles}ctx {_n(d['tokens'])}"
    if d.get("window"):
        ctx += f"/{_n(d['window'])} ({100 * d['tokens'] / d['window']:.0f}%)"
    t, s = d["turn"], d["session"]
    return (f"{ctx} · this turn {_n(t['input_tokens'])} in {_out(t)}"
            f" · session {_n(s['input_tokens'])} in {_out(s)} · {s['requests']} req")


class AgentProgress:
    """What a running agent is doing, from its forwarded events, as one status line."""

    def __init__(self, name: str) -> None:
        self.name, self.phase, self.chars, self.requests = name, "starting", 0, 0

    def update(self, kind: str, d: dict) -> None:
        if kind == "context":
            self.requests = d["turn"]["requests"]
        elif kind == "tool_call":
            self.phase, self.chars = f"running {d['tool']}", 0
        elif kind in ("assistant_thinking_delta", "assistant_text_delta"):
            phase = "thinking" if kind == "assistant_thinking_delta" else "writing"
            if phase != self.phase:
                self.phase, self.chars = phase, 0
            self.chars += len(d["text"])

    def line(self) -> str:
        what = f"{self.phase}… {self.chars:,} chars" if self.phase in ("thinking", "writing") else f"{self.phase}…"
        return f"{self.name} › {what}" + (f" · request {self.requests}" if self.requests else "")
