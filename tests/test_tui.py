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
    fe, out = make(["y", "n", "a", EOFError()])
    assert (await fe.request_approval(req)).allow
    assert not (await fe.request_approval(req)).allow
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
