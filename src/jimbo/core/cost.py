"""Estimated cost in USD of model requests. An estimate, not what the provider billed."""

from dataclasses import asdict, dataclass

from pydantic_ai.usage import RunUsage

from .stats import _n, thinking_tokens


def estimate_cost(usage: RunUsage | None, prices: dict[str, float] | None) -> float | None:
    """pydantic-ai's estimate from its price tables; failing that, one from the per-token prices the
    provider lists for the main model. None when neither can price it."""
    if usage is None:
        return None
    if usage.cost is not None:
        return float(usage.cost)
    if not prices or not usage.requests:
        return None
    cached = usage.cache_read_tokens + usage.cache_write_tokens
    return (max(usage.input_tokens - cached, 0) * prices["prompt"]
            + usage.cache_read_tokens * prices.get("input_cache_read", prices["prompt"])
            + usage.cache_write_tokens * prices.get("input_cache_write", prices["prompt"])
            + usage.output_tokens * prices["completion"])


def format_cost(usd: float) -> str:
    return f"${usd:.4f}"


@dataclass
class Spend:
    """Turns, requests, tokens and estimated cost added up over turns."""
    turns: int = 0  # user messages (and @agent calls) that started a run
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0  # reasoning tokens, included in output_tokens
    cost: float = 0.0  # of the requests that could be priced
    unpriced: int = 0  # requests with no price, so `cost` is a lower bound

    def add(self, usage: RunUsage, cost: float | None) -> None:
        self.requests += usage.requests
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.thinking_tokens += thinking_tokens(usage)
        if cost is None:
            self.unpriced += usage.requests
        else:
            self.cost += cost

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Spend":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in fields})

    def summary(self) -> str:
        """e.g. `3 turns · 12 requests · 40.1k in · 3.2k out (1.1k thinking) · $0.0123 estimated`."""
        out = f"{_n(self.output_tokens)} out" + (f" ({_n(self.thinking_tokens)} thinking)" if self.thinking_tokens else "")
        parts = [*([f"{self.turns} turn{'s' if self.turns != 1 else ''}"] if self.turns else []), f"{self.requests} requests",
                 f"{_n(self.input_tokens)} in", out]
        if self.unpriced and self.unpriced == self.requests:
            parts.append("cost unknown (no prices for this model)")
        else:
            parts.append(f"{format_cost(self.cost)} estimated"
                         + (f" ({self.unpriced} requests not priced)" if self.unpriced else ""))
        return " · ".join(parts)
