from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.core.approval import ApprovalDecision
from jimbo.tools.paths import PathEscape, resolve_in_workspace
from jimbo.wiring import build_session


def test_path_escape(tmp_path):
    (tmp_path / "ok.txt").write_text("x")
    assert resolve_in_workspace(tmp_path, "ok.txt") == (tmp_path / "ok.txt").resolve()
    with pytest.raises(PathEscape):
        resolve_in_workspace(tmp_path, "../etc/passwd")
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(PathEscape):
        resolve_in_workspace(tmp_path, "link/passwd")


async def test_session_runs_tools_and_emits_events(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld\n")
    fe = ScriptedFrontend()
    def script(messages, info):
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart):
            return ModelResponse(parts=[TextPart(f"saw: {last.content}")])
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": "a.txt"})])

    async def stream(messages, info):
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart):
            yield f"saw: {last.content}"
        else:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path": "a.txt"}')}

    s = build_session(settings(tmp_path), fe, model=FunctionModel(script, stream_function=stream))
    out = await s.send("go")
    assert out == "saw: 1\thello\n2\tworld"
    assert out is not None
    kinds = [e.kind for e in fe.events]
    assert "tool_call" in kinds and kinds[-1] == "run_finished"
    assert "approval_resolved" in kinds  # read-only auto-allowed, but still logged
    assert [e.seq for e in fe.events] == sorted(e.seq for e in fe.events)


async def test_denied_non_readonly_fails_closed(tmp_path):
    from jimbo.core.deps import ToolDenied
    fe = ScriptedFrontend(allow=False)
    s = build_session(settings(tmp_path), fe, model=TestModel())
    with pytest.raises(ToolDenied):
        await s.deps.authorize("write_file", {}, "write x")


async def test_remember_for_session(tmp_path):
    class Always(ScriptedFrontend):
        async def request_approval(self, req):
            self.asked = getattr(self, "asked", 0) + 1
            return ApprovalDecision(allow=True, remember="session")
    fe = Always()
    s = build_session(settings(tmp_path), fe, model=TestModel())
    await s.deps.authorize("write_file", {}, "w1")
    await s.deps.authorize("write_file", {}, "w2")
    assert fe.asked == 1


async def test_write_and_edit_tools(tmp_path):
    from types import SimpleNamespace
    from pydantic_ai import ModelRetry
    from jimbo.tools.fs import edit_file, write_file

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=TestModel())
    ctx = SimpleNamespace(deps=s.deps)
    await write_file(ctx, "sub/r.txt", "a\nb\n")
    assert (tmp_path / "sub/r.txt").read_text() == "a\nb\n"
    await edit_file(ctx, "sub/r.txt", "b", "c")
    assert (tmp_path / "sub/r.txt").read_text() == "a\nc\n"
    with pytest.raises(ModelRetry):
        await edit_file(ctx, "sub/r.txt", "zzz", "x")
    with pytest.raises(ModelRetry):
        await write_file(ctx, "../x.txt", "no")
    fe.allow = False
    s2 = build_session(settings(tmp_path), fe, model=TestModel())
    with pytest.raises(ModelRetry):
        await write_file(SimpleNamespace(deps=s2.deps), "n.txt", "x")
    assert not (tmp_path / "n.txt").exists()


async def test_parallel_approvals_are_serialized():
    import asyncio
    from jimbo.core.approval import AskUser, RememberedDecisions, ToolCallInfo
    active = peak = asked = 0

    async def ask(req):
        nonlocal active, peak, asked
        asked += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ApprovalDecision(allow=True, remember="session")

    au = AskUser(ask, RememberedDecisions())
    call = ToolCallInfo(tool="write_file", args={}, summary="s", workspace=Path("."))
    vs = await asyncio.gather(*(au.evaluate(call) for _ in range(3)))
    assert peak == 1 and asked == 1 and all(v.action == "allow" for v in vs)


async def test_jsonl_sink_logs_session(tmp_path):
    import json
    from jimbo.core.jsonl import JsonlSink

    sink = JsonlSink(tmp_path / "logs")
    s = build_session(settings(tmp_path), ScriptedFrontend(), [sink], model=TestModel())
    await s.send("hi")
    sink.close()
    lines = (tmp_path / "logs" / f"{s.session_id}.jsonl").read_text().splitlines()
    rows = [json.loads(x) for x in lines]
    assert rows[0]["kind"] == "user_message" and rows[-1]["kind"] == "run_finished"
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)


async def test_streamed_text_keeps_first_chunk(tmp_path):
    async def stream(messages, info):
        yield "Now "
        yield "let me"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=stream))
    await s.send("go")
    streamed = "".join(e.data["text"] for e in fe.events if e.kind == "assistant_text_delta")
    assert streamed == "Now let me"


async def test_cancel_keeps_partial_turn_and_session_usable(tmp_path):
    import asyncio
    (tmp_path / "a.txt").write_text("hello\n")
    started, calls = asyncio.Event(), []

    async def stream(messages, info):
        calls.append(1)
        last = messages[-1].parts[-1]
        if len(calls) == 1:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path": "a.txt"}')}
        elif isinstance(last, ToolReturnPart):
            yield "thinking about "
            started.set()
            await asyncio.Event().wait()  # hang until cancelled
        else:
            yield "fresh answer"

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=stream))
    assert not s.cancel()  # nothing running
    task = asyncio.create_task(s.send("go"))
    await asyncio.wait_for(started.wait(), 2)
    assert s.cancel()
    assert await asyncio.wait_for(task, 2) is None
    assert fe.events[-1].kind == "run_finished" and fe.events[-1].data == {"ok": False, "cancelled": True}
    parts = [p for m in s.history for p in m.parts]
    assert any(isinstance(p, ToolReturnPart) and "hello" in str(p.content) for p in parts)
    assert await s.send("again") == "fresh answer"


async def test_context_event(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, context_window=1000), fe, model=TestModel())
    await s.send("hi")
    ctx = [e.data for e in fe.events if e.kind == "context"]
    assert ctx and ctx[-1]["window"] == 1000 and ctx[-1]["tokens"] == s.context_tokens > 0
    last = ctx[-1]
    assert last["turn"]["input_tokens"] > 0 and last["turn"]["requests"] >= 1
    assert last["session"] == last["turn"]  # first prompt: session == turn
    await s.send("again")
    last = [e.data for e in fe.events if e.kind == "context"][-1]
    assert last["session"]["requests"] > last["turn"]["requests"]
    assert last["session"]["input_tokens"] > last["turn"]["input_tokens"]


async def test_sigint_cancels_run_not_jimbo(tmp_path):
    import asyncio, os, signal
    from jimbo.core.runner import run_frontend
    started = asyncio.Event()

    async def stream(messages, info):
        yield "working"
        started.set()
        await asyncio.Event().wait()

    fe = ScriptedFrontend(["go"])
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=stream))
    task = asyncio.create_task(run_frontend(s, fe))
    await asyncio.wait_for(started.wait(), 2)
    os.kill(os.getpid(), signal.SIGINT)
    await asyncio.wait_for(task, 2)
    assert fe.events[-1].data == {"ok": False, "cancelled": True}
    assert signal.getsignal(signal.SIGINT) is not None  # handler restored


async def test_shell_allowed_by_rule_without_asking(tmp_path):
    async def stream(messages, info):
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart):
            yield f"got: {last.content}"
        else:
            yield {0: DeltaToolCall(name="shell", json_args='{"command": "echo from-shell"}')}

    fe = ScriptedFrontend(allow=False)  # would deny if asked
    s = build_session(settings(tmp_path, approval_allow=["shell:echo *"]), fe,
                      model=FunctionModel(stream_function=stream))
    assert await s.send("go") == "got: exit code 0\nfrom-shell"
    resolved = [e.data for e in fe.events if e.kind == "approval_resolved"]
    assert resolved == [{"tool": "shell", "action": "allow", "decided_by": "rules",
                         "reason": "allowed by rule 'shell:echo *'"}]
