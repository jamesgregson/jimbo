import asyncio

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
