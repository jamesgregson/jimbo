from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
                                  UserPromptPart)
from pydantic_ai.models.function import FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.core.compaction import SUMMARY_PREFIX, split_point
from jimbo.wiring import build_session


def req(text):
    return ModelRequest(parts=[UserPromptPart(text)])


def call(i):
    return ModelResponse(parts=[ToolCallPart("read_file", {"path": "a"}, tool_call_id=f"c{i}")])


def ret(i):
    return ModelRequest(parts=[ToolReturnPart("read_file", "x" * 2000, tool_call_id=f"c{i}")])


def reply(text):
    return ModelResponse(parts=[TextPart(text)])


def test_split_never_separates_tool_pair():
    h = [req("a"), call(1), ret(1), reply("r1"), req("b"), call(2), ret(2), reply("r2")]
    for keep in range(1, 8):
        cut = split_point(h, keep)
        assert cut is not None and (cut == len(h) or not isinstance(h[cut].parts[0], ToolReturnPart))


def test_split_single_long_run_keeps_tail():
    h = [req("a"), call(1), ret(1), call(2), ret(2)]
    assert split_point(h, 2, allow_empty=False) is None
    assert split_point(h, 2) == len(h)


def model_with_summary(seen):
    def answer(messages):
        text = str(messages[-1].parts[-1].content)
        if "Transcript to summarize" in text:
            seen.append(text)
            return "SUMMARY"
        return "ok"

    def script(messages, info):
        return ModelResponse(parts=[TextPart(answer(messages))])

    async def stream(messages, info):
        yield answer(messages)
    return FunctionModel(script, stream_function=stream)


async def test_compact_command_summarizes_and_keeps_tail(tmp_path):
    fe, seen = ScriptedFrontend(), []
    s = build_session(settings(tmp_path, compact_keep=2), fe, model=model_with_summary(seen))
    for t in ("one", "two", "three"):
        await s.send(t)
    n = len(s.history)
    await s.commands.dispatch(s, "/compact auth flow")
    assert len(s.history) < n
    assert s.history[0].parts[0].content == SUMMARY_PREFIX + "SUMMARY"
    assert "one" in seen[0] and "auth flow" in seen[0]
    assert s.history[-1].parts[0].content == "ok" and "three" in str(s.history[-2].parts[0].content)
    ev = [e for e in fe.events if e.kind == "compaction"]
    assert ev and "tokens_after" in ev[0].data
    out = [e for e in fe.events if e.kind == "command_output"][-1].data["text"]
    assert "tokens (saved ~" in out


async def test_nothing_to_compact(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path), fe, model=model_with_summary([]))
    await s.commands.dispatch(s, "/compact")
    assert [e for e in fe.events if e.kind == "command_output"][-1].data["text"] == "Nothing to compact."


async def test_auto_compaction_before_send(tmp_path):
    fe, seen = ScriptedFrontend(), []
    s = build_session(settings(tmp_path, compact_keep=2, context_window=1000), fe,
                      model=model_with_summary(seen))
    await s.send("one")
    await s.send("two")
    assert not seen
    s.context_tokens = 900
    await s.send("three")
    assert seen and s.history[0].parts[0].content.startswith(SUMMARY_PREFIX)
    assert not [e for e in fe.events if e.kind == "error"]


async def test_midrun_compaction_keeps_pending_tool_pair(tmp_path):
    fe, seen = ScriptedFrontend(), []

    def answer(messages):
        text = str(messages[-1].parts[-1].content)
        if "Transcript to summarize" in text:
            seen.append(text)
            return ModelResponse(parts=[TextPart("SUMMARY")])
        if text == "go":
            return ModelResponse(parts=[ToolCallPart("list_dir", {"path": "."})])
        return ModelResponse(parts=[TextPart("done" if isinstance(messages[-1].parts[-1], ToolReturnPart)
                                             else "ok")])

    async def stream(messages, info):
        r = answer(messages).parts[0]
        if isinstance(r, ToolCallPart):
            from pydantic_ai.models.function import DeltaToolCall
            yield {0: DeltaToolCall(name=r.tool_name, json_args='{"path": "."}')}
        else:
            yield r.content
    s = build_session(settings(tmp_path, compact_keep=2, context_window=1), fe,
                      model=FunctionModel(lambda m, i: answer(m), stream_function=stream))
    s.compact_threshold = 0  # no between-turn compaction while building history
    await s.send("one")
    await s.send("two")
    s.compact_threshold = 0.5
    s.context_tokens = 0
    assert await s.send("go") == "done"
    assert seen and not [e for e in fe.events if e.kind == "error"]
    assert s.history[0].parts[0].content.startswith(SUMMARY_PREFIX)


def busy_and_after(fe):
    """The busy events, and the kinds of events that came between them."""
    kinds = [e.kind for e in fe.events]
    start, end = kinds.index("busy"), len(kinds) - 1 - kinds[::-1].index("busy")
    return [e.data for e in fe.events if e.kind == "busy"], kinds[start + 1:end], kinds[end + 1:]


async def test_compact_command_shows_busy_until_done(tmp_path):
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, compact_keep=2), fe, model=model_with_summary([]))
    for t in ("one", "two", "three"):
        await s.send(t)
    await s.commands.dispatch(s, "/compact")
    busy, during, after = busy_and_after(fe)
    assert busy == [{"label": "compacting", "on": True}, {"label": "compacting", "on": False}]
    assert "compaction" in during and "command_output" in after


async def test_compact_failure_still_ends_busy(tmp_path):
    def broken(messages, info):
        raise RuntimeError("model down")

    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, compact_keep=2), fe, model=model_with_summary([]))
    for t in ("one", "two", "three"):
        await s.send(t)
    s.deps.models.resolve = lambda task: FunctionModel(broken)  # the compaction model fails
    await s.commands.dispatch(s, "/compact")
    busy, _, after = busy_and_after(fe)
    assert [b["on"] for b in busy] == [True, False]
    assert [e.data for e in fe.events if e.kind == "command_output"][-1]["ok"] is False


async def test_asking_the_model_to_compact_compacts(tmp_path):
    fe, seen = ScriptedFrontend(), []

    def answer(messages):
        last = messages[-1].parts[-1]
        text = str(last.content)
        if "Transcript to summarize" in text:
            seen.append(text)
            return ModelResponse(parts=[TextPart("SUMMARY")])
        if text == "please compact, keep the auth details":
            return ModelResponse(parts=[ToolCallPart("compact", {"focus": "auth"})])
        return ModelResponse(parts=[TextPart("compacted" if isinstance(last, ToolReturnPart) else "ok")])

    async def stream(messages, info):
        r = answer(messages).parts[0]
        if isinstance(r, ToolCallPart):
            from pydantic_ai.models.function import DeltaToolCall
            yield {0: DeltaToolCall(name=r.tool_name, json_args='{"focus": "auth"}')}
        else:
            yield r.content
    s = build_session(settings(tmp_path, compact_keep=2), fe,
                      model=FunctionModel(lambda m, i: answer(m), stream_function=stream))
    await s.send("one")
    await s.send("two")
    assert not seen  # no context window: nothing compacts on its own
    assert await s.send("please compact, keep the auth details") == "compacted"
    assert len(seen) == 1 and "Pay particular attention to: auth" in seen[0]
    assert s.history[0].parts[0].content.startswith(SUMMARY_PREFIX)
    assert s.deps.compact_requested is None and [e for e in fe.events if e.kind == "compaction"]
