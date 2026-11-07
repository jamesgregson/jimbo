import asyncio
import io
import json
import os
import subprocess
import sys

from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from helpers import settings
from jimbo.core.events import PROTOCOL_VERSION
from jimbo.core.runner import run_frontend
from jimbo.frontends.stdio import StdioFrontend
from jimbo.wiring import build_session


class Pipe:
    """stdin for the frontend: lines written here are read by its reader thread."""

    def __init__(self):
        r, w = os.pipe()
        self.read = os.fdopen(r, "r", encoding="utf-8")
        self._w = os.fdopen(w, "w", encoding="utf-8", buffering=1)

    def send(self, msg: dict) -> None:
        self._w.write(json.dumps(msg) + "\n")

    def close(self) -> None:
        self._w.close()


class Lines(io.StringIO):
    """stdout for the frontend, read back as JSON messages."""

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in self.getvalue().splitlines()]

    async def wait_for(self, kind: str, start: int = 0, timeout: float = 5) -> dict:
        async def look():
            while True:
                for m in self.messages()[start:]:
                    if m["type"] == kind:
                        return m
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(look(), timeout)


def write_model(path="notes.txt", content="hello\n"):
    """Writes one file, then says done."""
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {0: DeltaToolCall(name="write_file", json_args=json.dumps({"path": path, "content": content}))}
        else:
            yield "done"
    return FunctionModel(stream_function=stream)


async def test_hello_is_the_first_line(tmp_path):
    out = Lines()
    StdioFrontend(tmp_path, stdin=io.StringIO(""), stdout=out)
    hello = out.messages()[0]
    assert hello["type"] == "hello" and hello["protocol"] == PROTOCOL_VERSION
    assert hello["workspace"] == str(tmp_path.resolve()) and hello["version"]


async def test_session_over_stdio(tmp_path):
    stdin, out = Pipe(), Lines()
    fe = StdioFrontend(tmp_path, stdin=stdin.read, stdout=out)
    s = build_session(settings(tmp_path), fe, model=write_model())
    await fe.start()
    run = asyncio.create_task(run_frontend(s, fe))
    stdin.send({"type": "send", "text": "write the notes"})

    ask = await out.wait_for("approval")
    req = ask["request"]
    assert req["tool"] == "write_file" and "+hello" in req["detail"]
    assert req["proposed"] == {"path": str((tmp_path / "notes.txt").resolve()), "text": "hello\n"}
    stdin.send({"type": "approve", "id": req["id"], "decision": "yes"})

    await out.wait_for("approval_closed")
    n = len(out.messages())
    stdin.send({"type": "changes_get"})
    changes = await out.wait_for("changes", n)
    assert changes["files"] == [{"path": str((tmp_path / "notes.txt").resolve()), "before": None}]

    stdin.close()  # end of stdin quits
    await asyncio.wait_for(run, 5)
    assert (tmp_path / "notes.txt").read_text() == "hello\n"
    msgs = out.messages()
    kinds = [m["event"]["kind"] for m in msgs if m["type"] == "event"]
    assert "run_finished" in kinds and "tool_result" in kinds
    logged = [m["event"]["data"] for m in msgs if m["type"] == "event" and m["event"]["kind"] == "approval_requested"]
    assert logged and "proposed" not in logged[0]  # the event (and the session log) keep the diff only


async def test_end_of_stdin_denies_an_open_approval(tmp_path):
    stdin, out = Pipe(), Lines()
    fe = StdioFrontend(tmp_path, stdin=stdin.read, stdout=out)
    s = build_session(settings(tmp_path), fe, model=write_model())
    await fe.start()
    run = asyncio.create_task(run_frontend(s, fe))
    stdin.send({"type": "send", "text": "write the notes"})
    await out.wait_for("approval")
    stdin.close()
    await asyncio.wait_for(run, 5)
    assert not (tmp_path / "notes.txt").exists()


async def test_confirm_answer_before_the_session_runs(tmp_path):
    stdin, out = Pipe(), Lines()
    fe = StdioFrontend(tmp_path, stdin=stdin.read, stdout=out)
    await fe.start()
    asked = asyncio.ensure_future(fe.confirm("accept?"))
    msg = await out.wait_for("confirm")
    stdin.send({"type": "confirm_answer", "id": msg["id"], "value": True})
    assert await asyncio.wait_for(asked, 5) is True
    stdin.close()


async def test_trace_and_image_requests(tmp_path):
    stdin, out = Pipe(), Lines()
    fe = StdioFrontend(tmp_path, stdin=stdin.read, stdout=out)
    await fe.start()
    stdin.send({"type": "trace_get"})
    assert "error" in await out.wait_for("trace")
    log = tmp_path / "abc.jsonl"
    log.write_text('{"seq":1,"kind":"user_message","data":{"text":"hi"}}\n')
    fe.bind_trace(lambda: log)
    n = len(out.messages())
    stdin.send({"type": "trace_get"})
    assert 'data-name="abc.jsonl"' in (await out.wait_for("trace", n))["html"]
    stdin.send({"type": "image_get", "id": "nope"})
    assert (await out.wait_for("image_data"))["error"]
    stdin.close()


def test_only_protocol_lines_reach_stdout(tmp_path):
    """Prints and child processes after the frontend is built go to stderr."""
    code = ("from jimbo.frontends.stdio import StdioFrontend; import os; StdioFrontend(); print('stray'); "
            "os.system('echo child')")
    p = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["type"] == "hello"
    assert "stray" in p.stderr and "child" in p.stderr
