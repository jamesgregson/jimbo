import asyncio
from io import StringIO

from rich.console import Console

from jimbo.core.approval import ApprovalRequest
from jimbo.core.events import Event
from jimbo.frontends.tui import TuiFrontend


def make(answers=()):
    out, answers = StringIO(), list(answers)

    async def ask(_msg):
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    return TuiFrontend(Console(file=out, force_terminal=False, width=100), ask), out


def ev(seq, kind, **data):
    return Event(seq=seq, session_id="s", kind=kind, data=data)


async def test_streamed_turn_not_duplicated():
    fe, out = make()
    for e in [
        ev(1, "assistant_text_delta", text="hello "),
        ev(2, "assistant_text_delta", text="world"),
        ev(3, "tool_call", id="1", tool="read_file", args={"path": "a.txt"}),
        ev(4, "tool_result", id="1", ok=True, content="1 hi"),
        ev(5, "assistant_text_delta", text="done"),
        ev(6, "assistant_text", text="done"),
        ev(7, "usage", requests=2, input_tokens=10, output_tokens=5),
        ev(8, "run_finished", ok=True),
    ]:
        await fe.render(e)
    s = out.getvalue()
    assert "hello world" in s and "read_file(path='a.txt')" in s and "✓ 1 hi" in s
    assert s.count("done") == 1
    assert "2 req · 10 in · 5 out" in s


async def test_unstreamed_text_and_error():
    fe, out = make()
    await fe.render(ev(1, "assistant_text", text="plain answer"))
    await fe.render(ev(2, "error", type="Boom", message="bad"))
    assert "plain answer" in out.getvalue() and "Boom: bad" in out.getvalue()


async def test_approval_mapping():
    req = ApprovalRequest(id="1", tool="write_file", summary="write a.txt", detail="--- a\n+++ b")
    fe, out = make(["y", "n", "", "a", EOFError()])
    assert (await fe.request_approval(req)).allow
    denied = await fe.request_approval(req)  # "n" is followed by an optional reason; empty skips it
    assert not denied.allow and denied.note is None
    d = await fe.request_approval(req)
    assert d.allow and d.remember == "session"
    assert not (await fe.request_approval(req)).allow
    assert "Approve write_file" in out.getvalue()


async def test_input_quit_and_double_interrupt():
    fe, _ = make(["hi", EOFError()])
    assert await fe.next_user_input() == "hi"
    assert await fe.next_user_input() is None
    fe, _ = make([KeyboardInterrupt(), KeyboardInterrupt()])
    assert await fe.next_user_input() is None


async def test_commands_completion_output_and_interrupt():
    from prompt_toolkit.document import Document
    fe, out = make()
    await fe.render(ev(1, "commands", commands=[{"name": "help", "help": "list"}, {"name": "clear", "help": "x"}]))
    got = [c.text for c in fe.completer.get_completions(Document("/he"), None)]
    assert got == ["/help"]
    assert not list(fe.completer.get_completions(Document("/help me"), None))
    await fe.render(ev(2, "command_output", command="help", ok=True, text="/help — list"))
    await fe.render(ev(3, "context", tokens=250, window=1000,
                             turn={"requests": 1, "input_tokens": 200, "output_tokens": 50},
                             session={"requests": 3, "input_tokens": 1500, "output_tokens": 90}))
    await fe.render(ev(4, "usage", requests=1, input_tokens=200, output_tokens=50))
    await fe.render(ev(5, "run_finished", ok=False, cancelled=True))
    s = out.getvalue()
    assert "/help — list" in s and "(interrupted)" in s and "ctx 250/1.0k (25%)" in s and "session 1.5k in 90 out" in s and fe._stats


async def test_todos_checklist():
    fe, out = make()
    await fe.render(ev(1, "todos", items=[{"content": "one", "status": "done"},
                                          {"content": "two", "status": "in_progress"},
                                          {"content": "three", "status": "pending"}]))
    s = out.getvalue()
    assert "✓ one" in s and "▸ two" in s and "○ three" in s


async def test_deny_reason_is_the_note():
    req = ApprovalRequest(id="1", tool="shell", summary="rm -rf build")
    fe, _ = make(["n", "use git clean instead"])
    d = await fe.request_approval(req)
    assert not d.allow and d.note == "use git clean instead"


async def test_approval_and_steer_share_the_main_prompt():
    lines: asyncio.Queue[str] = asyncio.Queue()

    async def ask(_msg):
        return await lines.get()

    out = StringIO()
    fe = TuiFrontend(Console(file=out, force_terminal=False, width=100), ask)
    req = ApprovalRequest(id="1", tool="shell", summary="make")
    reading = asyncio.create_task(fe.next_user_input())  # the prompt that stays up during a run
    await asyncio.sleep(0.01)
    approval = asyncio.create_task(fe.request_approval(req))
    await asyncio.sleep(0.01)
    await lines.put("n")
    await asyncio.sleep(0.01)
    await lines.put("too slow")
    d = await asyncio.wait_for(approval, 1)
    assert not d.allow and d.note == "too slow" and not reading.done()
    await lines.put("also check the tests")  # no question pending: this is a steer
    assert await asyncio.wait_for(reading, 1) == "also check the tests"


async def test_steer_event_and_block_streaming():
    fe, out = make()
    await fe.render(ev(1, "user_message", text="go", attachments=[]))
    assert fe._running
    await fe.render(ev(2, "assistant_text_delta", text="first block\n\nsecond"))
    assert "first block" in out.getvalue() and "second" not in out.getvalue()  # only complete blocks print
    await fe.render(ev(3, "user_message", text="use rg", attachments=[], steer=True))
    assert "second" in out.getvalue() and "steering: use rg" in out.getvalue()
    await fe.render(ev(4, "run_finished", ok=True))
    assert not fe._running


async def test_code_fence_blank_lines_do_not_split_blocks():
    from jimbo.frontends.tui import _block_end
    assert _block_end("```py\na\n\nb\n") is None
    assert _block_end("para\n\nnext") == 5


async def test_approval_shows_risk_assessment():
    a = {"summary": "Deletes the build directory.", "risk": "high", "reasons": ["forced or recursive delete"],
         "model": "ollama:small", "raised": False}
    fe, out = make(["y"])
    await fe.request_approval(ApprovalRequest(id="1", tool="shell", summary="run: rm -rf build", assessment=a))
    text = out.getvalue()
    assert "HIGH" in text and "Deletes the build directory." in text
    assert "forced or recursive delete" in text and "assessed by ollama:small" in text


async def test_toolbar_shows_model_and_modes():
    f = TuiFrontend(console=Console(file=StringIO()), ask=lambda m: None)
    status = {"model": "p:m", "workspace": "/w", "mode": "normal", "yolo": False, "sandboxed": True}
    await f.render(Event(seq=1, session_id="s", kind="status", data=status))
    assert f._toolbar() == "p:m · s · "  # the session id
    await f.render(Event(seq=2, session_id="s", kind="status", data={**status, "mode": "plan", "yolo": True}))
    assert "[PLAN]" in f._toolbar() and "[YOLO]" in f._toolbar()
    assert f.console.file.getvalue().count("jimbo · p:m") == 1  # the banner prints once


async def test_shell_output_is_shown_with_a_failing_exit_in_view():
    f = TuiFrontend(console=Console(file=StringIO(), width=100), ask=lambda m: None)
    await f.render(Event(seq=1, session_id="s", kind="tool_result",
                         data={"id": "1", "tool": "shell", "ok": True, "content": "exit code 1\nFAILED test_x"}))
    out = f.console.file.getvalue()
    assert "exit code 1" in out and "FAILED test_x" in out


async def test_sentence_at_approval_denies_with_it_as_the_reason():
    async def ask(message):
        return "actually keep the dist folder"

    f = TuiFrontend(console=Console(file=StringIO(), width=100), ask=ask)
    d = await f.request_approval(ApprovalRequest(id="1", tool="shell", summary="run", always="this exact command"))
    assert not d.allow and d.note == "actually keep the dist folder"


async def test_confirm_needs_an_exact_yes():
    for answer, ok in [("y", True), ("YES", True), ("n", False), ("", False), ("yes please", False),
                       (EOFError(), False), (KeyboardInterrupt(), False)]:
        fe, out = make([answer])
        assert await fe.confirm("Are you sure? Something bad") is ok
        assert "Are you sure? Something bad" in out.getvalue()


async def test_busy_shows_in_the_toolbar():
    fe, _ = make()
    await fe.render(ev(1, "busy", label="compacting", on=True))
    assert fe._toolbar().startswith("compacting…")
    await fe.render(ev(2, "busy", label="compacting", on=False))
    assert not (fe._toolbar() or "").startswith("compacting")


async def test_secret_answers_stay_out_of_history():
    from prompt_toolkit.history import InMemoryHistory
    from jimbo.frontends.tui import _SkippableHistory

    fe, _ = make(["sk-secret"])
    assert await fe.ask_secret("key: ") == "sk-secret" and not fe._secret
    hidden = {"on": True}
    h = _SkippableHistory(InMemoryHistory(), lambda: hidden["on"])
    h.append_string("sk-secret")
    hidden["on"] = False
    h.append_string("/providers key x")
    assert h.get_strings() == ["/providers key x"]
    fe, _ = make([KeyboardInterrupt()])
    assert await fe.ask_secret("key: ") is None


async def test_model_completion():
    from prompt_toolkit.document import Document

    fe, _ = make()
    await fe.render(ev(1, "config", path="p", aliases={"main": "ollama:a", "fast": "ollama:b"}, tasks=[],
                       providers=[{"name": "ollama"}, {"name": "work"}]))
    asked = []

    async def lister(provider):
        asked.append(provider)
        return ["m1", "m2"]

    fe.bind_model_list(lister)
    c = fe.model_completer

    def complete(text):
        return [x.text for x in c.get_completions(Document(text), None)]

    assert complete("/model f") == ["fast"]
    assert complete("/models set jimbo-compaction ") == ["main", "fast", "ollama:", "work:"]
    assert complete("/models alias x ") == ["ollama:", "work:"]  # no aliases of aliases
    assert complete("/model work:") == []  # asked; offered once it answers
    await asyncio.sleep(0)
    assert asked == ["work"] and complete("/model work:m") == ["work:m1", "work:m2"]
    assert complete("/model nobody:") == [] and asked == ["work"]  # unknown providers aren't asked
    assert complete("hello ") == []


async def test_agent_progress_is_a_toolbar_status_not_text():
    f = TuiFrontend(console=Console(file=StringIO()), ask=lambda m: None)
    tag = {"agent": {"name": "reviewer", "model": "p:m"}}
    await f.render(Event(seq=1, session_id="s", kind="user_message", data={"text": "go", "attachments": []}))
    await f.render(Event(seq=2, session_id="s", kind="context", data={
        "tokens": 1, "window": 10, "turn": {"requests": 3, "input_tokens": 1, "output_tokens": 1},
        "session": {"requests": 3, "input_tokens": 1, "output_tokens": 1}, **tag}))
    for chunk in ("let me ", "think"):
        await f.render(Event(seq=3, session_id="s", kind="assistant_thinking_delta", data={"text": chunk, **tag}))
    assert "reviewer › thinking… 12 chars · request 3" in f._toolbar()
    await f.render(Event(seq=4, session_id="s", kind="assistant_text_delta", data={"text": "found it", **tag}))
    assert "reviewer › writing… 8 chars" in f._toolbar()
    await f.render(Event(seq=5, session_id="s", kind="tool_call",
                         data={"id": "reviewer.1", "tool": "grep", "args": {}, **tag}))
    assert "reviewer › running grep…" in f._toolbar()
    await f.render(Event(seq=6, session_id="s", kind="tool_result",
                         data={"id": "1", "tool": "run_agent", "ok": True, "content": "report"}))
    assert "reviewer" not in f._toolbar()
    out = f.console.file.getvalue()
    assert "let me think" not in out and "found it" not in out


async def test_questions_take_turns_at_the_main_prompt():
    """/approve auto typed while steering asks to confirm; an approval the model asks for meanwhile must wait its
    turn, not take over the prompt and the answer meant for the confirm."""
    lines: asyncio.Queue[str] = asyncio.Queue()

    async def ask(_msg):
        return await lines.get()

    out = StringIO()
    fe = TuiFrontend(Console(file=out, force_terminal=False, width=100), ask)
    reading = asyncio.create_task(fe.next_user_input())
    await asyncio.sleep(0.01)
    confirm = asyncio.create_task(fe.confirm("Turn on auto?"))
    await asyncio.sleep(0.01)
    approval = asyncio.create_task(fe.request_approval(ApprovalRequest(id="1", tool="shell", summary="make")))
    await asyncio.sleep(0.01)
    assert "Approve shell" not in out.getvalue()  # its panel waits until the confirm is answered
    await lines.put("y")
    assert await asyncio.wait_for(confirm, 1) is True and not approval.done()
    await asyncio.sleep(0.01)
    assert "Approve shell" in out.getvalue()
    await lines.put("n")
    await asyncio.sleep(0.01)
    await lines.put("")
    assert not (await asyncio.wait_for(approval, 1)).allow and not reading.done()
    reading.cancel()


async def test_paste_command_attaches_the_clipboard_image(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    clip = {"data": png}

    async def clipboard():
        return clip["data"]
    monkeypatch.setattr("jimbo.frontends.tui.clipboard_png", clipboard)
    fe, out = make(["/paste what is this?"])
    msg = await fe.next_user_input()
    assert msg.text == "what is this?" and [i.data for i in msg.images] == [png]

    seen = []
    answers = ["/paste", "why so bad"]

    async def ask(prompt):
        seen.append(prompt)
        return answers.pop(0)
    fe = TuiFrontend(Console(file=StringIO()), ask)
    msg = await fe.next_user_input()  # /paste alone: the chip waits in the prompt for the question
    assert msg.text == "why so bad" and len(msg.images) == 1 and len(seen) == 2

    clip["data"] = None
    fe, out = make(["/paste", "plain"])
    assert await fe.next_user_input() == "plain" and "no image on the clipboard" in out.getvalue()
