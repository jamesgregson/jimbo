from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
import json

from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.core.approval import ApprovalDecision
from jimbo.tools.plan import plan_steps
from jimbo.wiring import build_session

PLAN = "Plan:\n- read code\n  - nested\n2. fix bug\n- [ ] run tests"


def model(calls):
    """Calls each (tool, args) in turn, then answers."""
    it = iter(calls)

    def script(messages, info):
        nxt = next(it, None)
        return ModelResponse(parts=[ToolCallPart(*nxt)] if nxt else [TextPart("done")])
    async def stream(messages, info):
        r = script(messages, info)
        part = r.parts[0]
        if isinstance(part, ToolCallPart):
            yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}
        else:
            yield part.content
    return FunctionModel(script, stream_function=stream)


def retries(s):
    return [str(p.content) for m in s.history for p in m.parts
            if isinstance(p, ToolReturnPart) and p.outcome == "retried"]


def results(s, tool):
    return [str(p.content) for m in s.history for p in m.parts
            if isinstance(p, ToolReturnPart) and p.tool_name == tool and p.outcome == "success"]


def test_plan_steps():
    assert [t.content for t in plan_steps(PLAN)] == ["read code", "fix bug", "run tests"]


async def test_plan_mode_denies_writes_and_exit_plan_approves(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=model([
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("exit_plan", {"plan": PLAN}),
    ]))
    await s.commands.dispatch(s, "/plan")
    assert s.deps.mode == "plan"
    await s.send("go")
    assert not (tmp_path / "a.txt").exists()
    assert any("plan mode" in r for r in retries(s))
    assert s.deps.mode == "normal"
    assert [t.content for t in s.deps.todos] == ["read code", "fix bug", "run tests"]


async def test_rejected_plan_stays_in_plan_mode_even_with_yolo(tmp_path):
    fe = ScriptedFrontend(allow=False)
    s = build_session(settings(tmp_path), fe, model=model([("exit_plan", {"plan": PLAN})]))
    await s.commands.dispatch(s, "/approve yolo")
    await s.commands.dispatch(s, "/plan on")
    await s.send("go")
    assert s.deps.mode == "plan" and s.deps.todos == []
    assert any("not approved" in r for r in results(s, "exit_plan"))


async def test_rejection_feedback_steers_the_next_plan(tmp_path):
    class Reject(ScriptedFrontend):
        async def request_approval(self, req):
            return ApprovalDecision(allow=False, note="use sqlite, not postgres")

    fe = Reject()
    calls = [("exit_plan", {"plan": f"- plan {i}"}) for i in range(5)]  # more rejections than tool_retries allows
    s = build_session(settings(tmp_path), fe, model=model(calls))
    await s.commands.dispatch(s, "/plan on")
    assert await s.send("go") == "done"
    got = results(s, "exit_plan")
    assert len(got) == 5 and not retries(s) and not [e for e in fe.events if e.kind == "error"]
    assert "The feedback: use sqlite, not postgres" in got[0] and "call exit_plan again" in got[0]
    assert s.deps.mode == "plan"


async def test_exit_plan_hidden_outside_plan_mode(tmp_path):
    seen = []

    def script(messages, info):
        seen.append([t.name for t in info.function_tools])
        return ModelResponse(parts=[TextPart("ok")])

    async def stream(messages, info):
        seen.append([t.name for t in info.function_tools])
        yield "ok"
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=FunctionModel(script, stream_function=stream))
    await s.send("a")
    await s.commands.dispatch(s, "/plan")
    await s.send("b")
    assert "exit_plan" not in seen[0] and "exit_plan" in seen[1]
