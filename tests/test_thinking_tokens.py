"""Thinking (reasoning) tokens: counted from either place pydantic-ai records them, and shown in the totals."""

from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from helpers import ScriptedFrontend, settings
from jimbo.core.cost import Spend
from jimbo.core.stats import format_stats, thinking_tokens
from jimbo.wiring import build_session


def test_counted_from_the_field_or_the_details():
    assert thinking_tokens(RunUsage(output_tokens=9, output_reasoning_tokens=6)) == 6
    assert thinking_tokens(RunUsage(output_tokens=9, details={"reasoning_tokens": 6})) == 6  # OpenAI-compatible
    assert thinking_tokens(RunUsage(output_reasoning_tokens=6, details={"reasoning_tokens": 6})) == 6  # not twice
    assert thinking_tokens(RunUsage(output_tokens=9)) == 0


def test_shown_only_when_reported():
    spend = Spend()
    spend.add(RunUsage(requests=1, input_tokens=100, output_tokens=2500, details={"reasoning_tokens": 1200}), 0.01)
    assert "2.5k out (1.2k thinking)" in spend.summary() and spend.to_dict()["thinking_tokens"] == 1200
    assert Spend.from_dict(spend.to_dict()).thinking_tokens == 1200  # kept across a resume
    plain = {"requests": 1, "input_tokens": 1, "output_tokens": 2}
    d = {"tokens": 10, "window": None, "turn": {**plain, "thinking_tokens": 150}, "session": plain}
    assert "turn context 1 in 2 out (150 thinking)" in format_stats(d) and "session 1 in 2 out ·" in format_stats(d)


async def test_the_session_reports_them(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=TestModel())
    s.turn_usage = RunUsage(requests=1, input_tokens=10, output_tokens=40, output_reasoning_tokens=30)
    s.total_usage = s.turn_usage
    await s._emit_usage()
    await s.emit_context()
    usage = [e.data for e in fe.events if e.kind == "usage"][-1]
    context = [e.data for e in fe.events if e.kind == "context"][-1]
    assert usage["thinking_tokens"] == 30 and context["turn"]["thinking_tokens"] == 30
    assert s.spend.thinking_tokens == 30


async def test_turns_and_cycles_are_counted_and_shown(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, request_limit=30), fe, model=TestModel(call_tools=["list_dir"]))
    await s.send("one")
    await s.send("two")
    d = [e.data for e in fe.events if e.kind == "context"][-1]
    n = s.turn_usage.requests
    assert d["turns"] == 2 and d["limit"] == 30 and d["turn"]["requests"] == n >= 1
    assert format_stats(d).startswith(f"turn 2, cycle {n}/30 · ctx")
    assert s.spend.turns == 2 and s.spend.summary().startswith(f"2 turns · {s.spend.requests} requests")
    s.spend.turns = 1
    assert s.spend.summary().startswith("1 turn ·")
