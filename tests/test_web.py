import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jimbo.core.approval import ApprovalRequest
from jimbo.core.events import Event
from jimbo.frontends.web import WebFrontend


@pytest.fixture
async def client():
    fe = WebFrontend(open_browser=False)
    c = TestClient(TestServer(fe.build_app()))
    await c.start_server()
    c.fe = fe
    yield c
    await c.close()


async def test_taken_port_moves_to_the_next_free_one(capsys):
    first = WebFrontend(port=0, open_browser=False)
    await first.start()
    taken = first._runner.addresses[0][1]
    first.port = taken
    capsys.readouterr()
    second = WebFrontend(port=taken, open_browser=False)
    await second.start()
    try:
        assert second.port > taken and second._runner.addresses[0][1] == second.port
        assert f"http://127.0.0.1:{second.port}/?token={second.token}" in capsys.readouterr().out
    finally:
        await second.stop()
        await first.stop()


async def test_no_free_port_is_a_clean_exit(monkeypatch):
    monkeypatch.setattr("jimbo.frontends.web.PORT_ATTEMPTS", 1)
    first = WebFrontend(port=0, open_browser=False)
    await first.start()
    second = WebFrontend(port=first._runner.addresses[0][1], open_browser=False)
    try:
        with pytest.raises(SystemExit, match="no free port"):
            await second.start()
    finally:
        await second.stop()
        await first.stop()


def ws_url(c, token=None):
    return f"/ws?token={c.fe.token if token is None else token}"


async def test_index_served(client):
    r = await client.get("/")
    assert r.status == 200 and "Jimbo" in await r.text()


async def test_rejects_bad_token_and_foreign_origin(client):
    r = await client.get("/ws?token=nope")
    assert r.status == 403
    r = await client.get(ws_url(client), headers={"Origin": "http://evil.example"})
    assert r.status == 403


async def test_input_events_and_replay(client):
    fe = client.fe
    await fe.render(Event(seq=1, session_id="s", kind="user_message", data={"text": "early"}))
    ws = await client.ws_connect(ws_url(client))
    assert (await ws.receive_json())["event"]["data"]["text"] == "early"  # replayed history
    await ws.send_json({"type": "send", "text": "hello"})
    assert await asyncio.wait_for(fe.next_user_input(), 1) == "hello"
    await fe.render(Event(seq=2, session_id="s", kind="assistant_text", data={"text": "hi"}))
    assert (await ws.receive_json())["event"]["kind"] == "assistant_text"


@pytest.mark.parametrize("decision,allow,remember", [("yes", True, "no"), ("no", False, "no"),
                                                     ("always", True, "session")])
async def test_approval_roundtrip(client, decision, allow, remember):
    ws = await client.ws_connect(ws_url(client))
    task = asyncio.create_task(client.fe.request_approval(ApprovalRequest(id="7", tool="write_file", summary="s")))
    msg = await ws.receive_json()
    assert msg["type"] == "approval" and msg["request"]["id"] == "7"
    await ws.send_json({"type": "approve", "id": "7", "decision": decision})
    d = await asyncio.wait_for(task, 1)
    assert d.allow is allow and d.remember == remember
    assert (await ws.receive_json())["type"] == "approval_closed"


async def test_cancel_message_calls_bound_cancel(client):
    called = asyncio.Event()
    client.fe.bind_cancel(called.set)
    ws = await client.ws_connect(ws_url(client))
    await ws.send_json({"type": "cancel"})
    await asyncio.wait_for(called.wait(), 1)


async def test_deny_note_roundtrip(client):
    ws = await client.ws_connect(ws_url(client))
    task = asyncio.create_task(client.fe.request_approval(ApprovalRequest(id="8", tool="shell", summary="s")))
    await ws.receive_json()
    await ws.send_json({"type": "approve", "id": "8", "decision": "no", "note": " use rg instead "})
    d = await asyncio.wait_for(task, 1)
    assert not d.allow and d.note == "use rg instead"


async def test_note_ignored_unless_denied(client):
    ws = await client.ws_connect(ws_url(client))
    task = asyncio.create_task(client.fe.request_approval(ApprovalRequest(id="9", tool="shell", summary="s")))
    await ws.receive_json()
    await ws.send_json({"type": "approve", "id": "9", "decision": "yes", "note": "x"})
    assert (await asyncio.wait_for(task, 1)).note is None


async def test_approval_carries_assessment(client):
    ws = await client.ws_connect(ws_url(client))
    a = {"summary": "s", "risk": "low", "reasons": [], "model": None, "raised": False}
    task = asyncio.create_task(client.fe.request_approval(
        ApprovalRequest(id="9", tool="shell", summary="run: ls", assessment=a)))
    assert (await ws.receive_json())["request"]["assessment"] == a
    await ws.send_json({"type": "approve", "id": "9", "decision": "yes"})
    await asyncio.wait_for(task, 1)


async def test_approval_mode_message_calls_bound_setter(client):
    got = asyncio.Queue()

    async def set_mode(mode):
        await got.put(mode)

    client.fe.bind_approval_mode(set_mode)
    ws = await client.ws_connect(ws_url(client))
    await ws.send_json({"type": "approval_mode", "mode": 3})  # ignored: not a string
    await ws.send_json({"type": "approval_mode", "mode": "auto"})
    assert await asyncio.wait_for(got.get(), 1) == "auto"


async def test_page_warns_before_auto_or_yolo(client):
    text = await (await client.get("/")).text()
    assert ("Are you sure? Jimbo will make automated decisions on your behalf and WILL destroy your code, "
            "system and reputation") in text


async def test_secret_dialog_and_config_messages(client):
    fe = client.fe
    got = asyncio.Queue()

    async def handle(msg):
        await got.put(msg)

    async def lister(provider):
        return ["a"] if provider == "lm" else "can't reach x"

    fe.bind_config(handle)
    fe.bind_model_list(lister)
    ws = await client.ws_connect(ws_url(client))
    asked = asyncio.ensure_future(fe.ask_secret("API key for lm: "))
    msg = await ws.receive_json()
    assert msg["type"] == "secret" and msg["message"] == "API key for lm: "
    await ws.send_json({"type": "secret_answer", "id": msg["id"], "value": "sk-x"})
    assert await asyncio.wait_for(asked, 1) == "sk-x"
    assert (await ws.receive_json()) == {"type": "secret_closed", "id": msg["id"]}
    assert "sk-x" not in json.dumps(fe._history)

    await ws.send_json({"type": "config_set", "op": "alias_rm", "name": "fast"})
    assert await asyncio.wait_for(got.get(), 1) == {"op": "alias_rm", "name": "fast"}
    await ws.send_json({"type": "config_get"})
    assert await asyncio.wait_for(got.get(), 1) == {"op": "get"}
    await ws.send_json({"type": "models_list", "provider": "lm"})
    assert await ws.receive_json() == {"type": "models", "provider": "lm", "models": ["a"]}
    await ws.send_json({"type": "models_list", "provider": "x"})
    assert await ws.receive_json() == {"type": "models", "provider": "x", "error": "can't reach x"}


async def test_confirm_is_yes_or_no_and_replayed(client):
    asked = asyncio.ensure_future(client.fe.confirm("accept?"))
    await asyncio.sleep(0)
    ws = await client.ws_connect(ws_url(client))  # connects after the question: it is replayed
    msg = await ws.receive_json()
    while msg["type"] != "confirm":
        msg = await ws.receive_json()
    assert msg["message"] == "accept?"
    await ws.send_json({"type": "confirm_answer", "id": msg["id"], "value": "yes"})  # only true counts
    assert await asyncio.wait_for(asked, 1) is False
    asked = asyncio.ensure_future(client.fe.confirm("accept?"))
    msg = await ws.receive_json()
    while msg["type"] != "confirm":
        msg = await ws.receive_json()
    await ws.send_json({"type": "confirm_answer", "id": msg["id"], "value": True})
    assert await asyncio.wait_for(asked, 1) is True


async def test_cancelled_secret_is_none(client):
    ws = await client.ws_connect(ws_url(client))
    asked = asyncio.ensure_future(client.fe.ask_secret("key: "))
    msg = await ws.receive_json()
    await ws.send_json({"type": "secret_answer", "id": msg["id"], "value": None})
    assert await asyncio.wait_for(asked, 1) is None


async def test_trace_page_needs_the_token_and_embeds_the_log(client, tmp_path):
    log = tmp_path / "abc.jsonl"
    log.write_text('{"seq":1,"kind":"user_message","data":{"text":"hi"}}\n')
    assert (await client.get(f"/trace?token={client.fe.token}")).status == 404  # not bound: no logging
    client.fe.bind_trace(lambda: log)
    assert (await client.get("/trace?token=nope")).status == 403
    r = await client.get(f"/trace?token={client.fe.token}")
    assert r.status == 200 and 'data-name="abc.jsonl">{"seq":1' in await r.text()
    client.fe.bind_trace(lambda: tmp_path / "missing.jsonl")
    assert (await client.get(f"/trace?token={client.fe.token}")).status == 404


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


async def test_shown_image_is_served_as_it_was_when_shown(client, tmp_path):
    f = tmp_path / "plot.png"
    f.write_bytes(PNG)
    await client.fe.render(Event(seq=1, session_id="s", kind="image",
                                 data={"id": "abc", "path": "plot.png", "file": str(f), "media_type": "image/png",
                                       "bytes": len(PNG), "caption": "a chart"}))
    f.write_bytes(b"changed later")
    assert (await client.get("/image/abc?token=nope")).status == 403
    assert (await client.get(f"/image/missing?token={client.fe.token}")).status == 404
    r = await client.get(f"/image/abc?token={client.fe.token}")
    assert r.status == 200 and r.content_type == "image/png" and await r.read() == PNG
    assert "sandbox" in r.headers["Content-Security-Policy"] and r.headers["X-Content-Type-Options"] == "nosniff"


async def test_model_shows_an_image_in_the_web_ui(tmp_path):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
    from helpers import settings
    from jimbo.wiring import build_session
    (tmp_path / "plot.png").write_bytes(PNG)
    steps = iter([{0: DeltaToolCall(name="show_image", json_args=json.dumps({"path": "plot.png", "caption": "c"}))},
                  "done"])

    async def stream(messages, info):
        assert "show_image" in [t.name for t in info.function_tools]
        yield next(steps)
    fe = WebFrontend(open_browser=False)
    s = build_session(settings(tmp_path), fe, model=FunctionModel(stream_function=stream))
    assert await s.send("plot it") == "done"
    shown = [m["event"]["data"] for m in fe._history if m["event"]["kind"] == "image"]
    assert len(shown) == 1 and shown[0]["path"] == "plot.png" and shown[0]["caption"] == "c"
    c = TestClient(TestServer(fe.build_app()))
    await c.start_server()
    try:
        r = await c.get(f"/image/{shown[0]['id']}?token={fe.token}")
        assert r.status == 200 and await r.read() == PNG
    finally:
        await c.close()


async def test_page_gets_the_diff_not_the_whole_proposed_file(client):
    ws = await client.ws_connect(ws_url(client))
    task = asyncio.create_task(client.fe.request_approval(
        ApprovalRequest(id="5", tool="write_file", summary="s", detail="+x", proposed={"path": "/p", "text": "x"})))
    msg = await ws.receive_json()
    assert msg["request"]["detail"] == "+x" and "proposed" not in msg["request"]
    await ws.send_json({"type": "approve", "id": "5", "decision": "yes"})
    await asyncio.wait_for(task, 1)
