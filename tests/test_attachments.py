import base64

from pydantic_ai.messages import BinaryContent, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.core.attachments import MAX_IMAGE_BYTES, UserMessage, check_image, expand_mentions
from jimbo.core.runner import run_frontend
from jimbo.frontends.tui import FileCompleter, TuiFrontend
from jimbo.frontends.web import WebFrontend
from jimbo.wiring import build_session

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def echo_model(seen):
    def script(messages, info):
        return ModelResponse(parts=[TextPart("ok")])
    async def stream(messages, info):
        seen.append(messages)
        yield "ok"

    return FunctionModel(script, stream_function=stream)


def user_content(messages):
    return [p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, UserPromptPart)][-1]


def test_expand_mentions(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    text, meta = expand_mentions("look at @a.py, and @missing.txt and me@a.py and @../x", tmp_path)
    assert '<file path="a.py">\nprint(1)\n\n</file>' in text and text.startswith("look at @a.py")
    assert [m["path"] for m in meta] == ["a.py"]
    assert expand_mentions("nothing here", tmp_path) == ("nothing here", [])


def test_expand_mentions_skips_binary_and_truncates(tmp_path):
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "big.txt").write_text("x" * 100_000)
    text, meta = expand_mentions("@bin @big.txt", tmp_path)
    assert [m["path"] for m in meta] == ["big.txt"] and meta[0]["truncated"] and "[truncated]" in text


def test_check_image():
    assert check_image("image/png", PNG) is None
    assert "unsupported" in check_image("text/html", b"x")
    assert "limit" in check_image("image/png", b"x" * (MAX_IMAGE_BYTES + 1))


async def test_send_images_and_mentions(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    fe, seen = ScriptedFrontend(), []
    s = build_session(settings(tmp_path), fe, model=echo_model(seen))
    await s.send(["see @a.txt", BinaryContent(PNG, media_type="image/png")])
    content = user_content(seen[0])
    assert isinstance(content, list) and "hello" in content[0] and isinstance(content[1], BinaryContent)
    ev = next(e for e in fe.events if e.kind == "user_message")
    assert ev.data["text"] == "see @a.txt"  # the log keeps what the user typed, no bytes
    assert ev.data["attachments"] == [{"kind": "file", "path": "a.txt", "bytes": 5, "truncated": False},
                                      {"kind": "image", "media_type": "image/png", "bytes": len(PNG)}]
    assert PNG not in ev.model_dump_json().encode()


async def test_images_dropped_for_text_only_model(tmp_path):
    fe, seen = ScriptedFrontend(), []
    s = build_session(settings(tmp_path, vision=False), fe, model=echo_model(seen))
    await s.send(["what is this", BinaryContent(PNG, media_type="image/png")])
    assert user_content(seen[0]) == "what is this"
    assert any(e.kind == "command_output" and not e.data["ok"] and "vision" in e.data["text"] for e in fe.events)


async def test_runner_sends_image_only_message_and_skips_commands(tmp_path):
    seen = []
    fe = ScriptedFrontend([UserMessage("/help", [BinaryContent(PNG, media_type="image/png")])])
    s = build_session(settings(tmp_path), fe, model=echo_model(seen))
    await run_frontend(s, fe)
    assert len(seen) == 1  # went to the model, not the command registry


def test_tui_collapses_long_pastes_and_lifts_images():
    fe = TuiFrontend(workspace=None)
    long = "\n".join(str(i) for i in range(30))
    assert fe.collapse_paste("a\nb") == "a\nb"
    chip = fe.collapse_paste(long.replace("\n", "\r\n"))
    assert chip == "[pasted #1: 30 lines]"
    assert fe.expand(f"x {chip} y") == f"x {long} y"
    fe._images[1] = BinaryContent(PNG, media_type="image/png")
    msg = fe.expand("what is [image #1] ?")
    assert isinstance(msg, UserMessage) and msg.text == "what is  ?" and len(msg.images) == 1
    fe._images[1] = BinaryContent(PNG, media_type="image/png")
    assert fe.expand("chip deleted") == "chip deleted" and not fe._images


def test_file_completer(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    (tmp_path / "README.md").write_text("")
    from prompt_toolkit.document import Document
    c = FileCompleter(tmp_path)
    got = [x.text for x in c.get_completions(Document("read @ma"), None)]
    assert got == ["@src/main.py"]
    assert list(c.get_completions(Document("mail me@ma"), None)) == []


async def test_web_send_with_images():
    fe = WebFrontend(open_browser=False)
    ok = base64.b64encode(PNG).decode()
    fe._on_message('{"type":"send","text":"hi","images":[{"media_type":"image/png","data":"%s"},'
                   '{"media_type":"text/html","data":"%s"},{"media_type":"image/png","data":"!!"}]}' % (ok, ok))
    msg = await fe.next_user_input()
    assert isinstance(msg, UserMessage) and msg.text == "hi" and [i.data for i in msg.images] == [PNG]
    fe._on_message('{"type":"send","text":"plain"}')
    assert await fe.next_user_input() == "plain"
    fe._on_message('{"type":"send","text":"  "}')
    assert fe._inputs.empty()


async def test_web_file_completion(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    (tmp_path / "README.md").write_text("")
    fe = WebFrontend(open_browser=False, workspace=tmp_path)
    async with TestClient(TestServer(fe.build_app())) as c:
        ws = await c.ws_connect(f"/ws?token={fe.token}")
        await ws.send_json({"type": "complete", "prefix": "ma"})
        assert await ws.receive_json() == {"type": "files", "prefix": "ma", "files": ["src/main.py"]}


def test_tui_shift_enter_maps_to_newline_binding():
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys
    assert ANSI_SEQUENCES["\x1b[13;2u"] == (Keys.Escape, Keys.ControlM)
