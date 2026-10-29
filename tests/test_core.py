import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from jimbo.config import Settings
from jimbo.core.approval import ApprovalDecision
from jimbo.tools.paths import PathEscape, resolve_in_workspace
from jimbo.wiring import build_session


class ScriptedFrontend:
    def __init__(self, inputs=(), allow=True):
        self.inputs, self.allow, self.events = list(inputs), allow, []

    async def next_user_input(self):
        return self.inputs.pop(0) if self.inputs else None

    async def render(self, event):
        self.events.append(event)

    async def request_approval(self, req):
        return ApprovalDecision(allow=self.allow)


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

    s = build_session(Settings(workspace=tmp_path), fe, model=FunctionModel(script, stream_function=stream))
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
    s = build_session(Settings(workspace=tmp_path), fe, model=TestModel())
    with pytest.raises(ToolDenied):
        await s.deps.authorize("write_file", {}, "write x")


async def test_remember_for_session(tmp_path):
    class Always(ScriptedFrontend):
        async def request_approval(self, req):
            self.asked = getattr(self, "asked", 0) + 1
            return ApprovalDecision(allow=True, remember="session")
    fe = Always()
    s = build_session(Settings(workspace=tmp_path), fe, model=TestModel())
    await s.deps.authorize("write_file", {}, "w1")
    await s.deps.authorize("write_file", {}, "w2")
    assert fe.asked == 1
