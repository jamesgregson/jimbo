from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
import json

from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.wiring import build_session

ITEMS = [{"content": "read code", "status": "done"}, {"content": "fix bug", "status": "in_progress"},
         {"content": "run tests"}]


def scripted(items):
    def script(messages, info):
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(parts=[ToolCallPart("todo_write", {"items": items})])
    async def stream(messages, info):
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            yield "ok"
        else:
            yield {0: DeltaToolCall(name="todo_write", json_args=json.dumps({"items": items}))}
    return FunctionModel(script, stream_function=stream)


def todo_events(fe):
    return [e.data["items"] for e in fe.events if e.kind == "todos"]


async def test_todo_write_updates_state_and_emits(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=scripted(ITEMS))
    await s.send("plan it")
    assert [t.content for t in s.deps.todos] == ["read code", "fix bug", "run tests"]
    assert todo_events(fe) == [[{"content": "read code", "status": "done"},
                                {"content": "fix bug", "status": "in_progress"},
                                {"content": "run tests", "status": "pending"}]]
    await s.commands.dispatch(s, "/todos")
    out = [e for e in fe.events if e.kind == "command_output"][-1].data["text"]
    assert out == "[x] read code\n[~] fix bug\n[ ] run tests"


async def test_two_in_progress_rejected(tmp_path):
    fe = ScriptedFrontend()
    bad = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
    s = build_session(settings(tmp_path), fe, model=scripted(bad))
    await s.send("x")
    assert s.deps.todos == [] and todo_events(fe) == []


async def test_clear_resets_todos(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=scripted(ITEMS))
    await s.send("plan it")
    await s.commands.dispatch(s, "/clear")
    assert s.deps.todos == [] and todo_events(fe)[-1] == []
    await s.commands.dispatch(s, "/todos")
    assert [e for e in fe.events if e.kind == "command_output"][-1].data["text"] == "No todos."
