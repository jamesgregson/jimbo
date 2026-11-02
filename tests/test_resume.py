import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from helpers import ScriptedFrontend, settings
from jimbo.core.jsonl import JsonlSink
from jimbo.core.runner import run_frontend
from jimbo.core.store import SessionStore
from jimbo.wiring import build_session


def make(tmp_path, inputs, model=None):
    st = settings(tmp_path, log_dir=tmp_path / "logs")
    fe = ScriptedFrontend(inputs)
    sink = JsonlSink(st.log_dir)
    return build_session(st, fe, [sink], model=model or TestModel(custom_output_text="hi")), fe, sink


async def test_history_and_todos_saved_after_turn(tmp_path):
    s, fe, sink = make(tmp_path, ["hello"])
    s.deps.todos = []
    await run_frontend(s, fe)
    saved = SessionStore(tmp_path / "logs").load(s.session_id)
    assert len(saved.history) == len(s.history) > 0


async def test_resume_restores_history_and_replays(tmp_path):
    s1, fe1, sink1 = make(tmp_path, ["hello"])
    await run_frontend(s1, fe1)
    sink1.close()

    last_seq = max(e.seq for e in s1.store.events(s1.session_id))
    s2, fe2, _ = make(tmp_path, [])
    msg = await s2.resume(s1.session_id[:4])
    assert s2.session_id == s1.session_id and len(s2.history) == len(s1.history)
    assert "Resumed" in msg
    kinds = [e.kind for e in fe2.events]
    assert "user_message" in kinds and "assistant_text" in kinds and "assistant_text_delta" not in kinds

    seen = []

    def f(messages, info):
        seen.append([p.content for m in messages if isinstance(m, ModelRequest)
                     for p in m.parts if isinstance(p, UserPromptPart)])
        return ModelResponse(parts=[TextPart("again")])

    async def stream(messages, info):
        f(messages, info)
        yield "again"
    with s2.agent.override(model=FunctionModel(f, stream_function=stream)):
        await s2.send("more")
    assert seen[0] == ["hello", "more"]
    assert s2._seq > last_seq


async def test_resume_command_lists_and_errors(tmp_path):
    s1, fe1, sink1 = make(tmp_path, ["hello there"])
    await run_frontend(s1, fe1)
    sink1.close()
    s2, fe2, _ = make(tmp_path, ["/resume", "/resume nope", f"/resume {s1.session_id}"])
    await run_frontend(s2, fe2)
    out = [e.data["text"] for e in fe2.events if e.kind == "command_output"]
    assert "hello there" in out[0] and s1.session_id in out[0]
    assert "no session matches" in out[1]
    assert "Resumed" in out[2]


def test_store_resolve_ambiguous(tmp_path):
    st = SessionStore(tmp_path)
    for i in ("abc1", "abc2"):
        st.save(i, [], [])
    with pytest.raises(ValueError, match="ambiguous"):
        st.resolve("abc")
    assert st.resolve("abc2") == "abc2"


async def test_clear_keeps_the_old_session_resumable(tmp_path):
    s, fe, sink = make(tmp_path, ["hello", "/clear", "after"])
    first = s.session_id
    await run_frontend(s, fe)
    sink.close()
    assert s.session_id != first
    assert s.store.load(first).history and first in s.store.ids()


async def test_clear_alone_leaves_latest_session_alone(tmp_path):
    s, fe, sink = make(tmp_path, ["hello", "/clear"])
    first = s.session_id
    await run_frontend(s, fe)
    sink.close()
    assert s.store.recent() == [first]


def test_recent_only_lists_this_workspace(tmp_path):
    here, there = SessionStore(tmp_path, tmp_path / "a"), SessionStore(tmp_path, tmp_path / "b")
    here.save("one", [], [])
    there.save("two", [], [])
    assert here.recent() == ["one"] and there.recent() == ["two"]
    assert sorted(here.ids()) == ["one", "two"]
    assert here.workspace_of("two") == str(tmp_path / "b")


async def test_resuming_another_projects_session_says_so(tmp_path):
    s1, fe1, sink1 = make(tmp_path, ["hello"])
    await run_frontend(s1, fe1)
    sink1.close()
    s2, _, _ = make(tmp_path, [])
    s2.store.workspace = tmp_path / "elsewhere"
    assert "It started in" in await s2.resume(s1.session_id)
