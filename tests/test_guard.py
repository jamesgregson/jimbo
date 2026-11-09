import json

import pytest
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.core import redact as rd
from jimbo.wiring import build_session


@pytest.fixture(autouse=True)
def clean():
    rd.clear()
    yield
    rd.clear()


@pytest.mark.parametrize("secret,kind", [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----", "private key"),
    ("AKIAIOSFODNN7EXAMPLE", "aws access key"),
    ("ghp_" + "a1B2" * 9, "github token"),
    ("github_pat_11ABCDEFG0123456789_abcdefghij", "github token"),
    ("glpat-abcdefghij0123456789", "gitlab token"),
    ("xoxb-1234567890-abcdefghij", "slack token"),
    ("sk_live_" + "a1" * 12, "stripe key"),
    ("AIza" + "B" * 35, "google api key"),
    ("sk-ant-api03-" + "x" * 30, "anthropic key"),
    ("sk-or-v1-" + "0f" * 16, "openrouter key"),
    ("sk-proj-" + "Ab1" * 10, "openai key"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "jwt"),
])
def test_formats(secret, kind):
    assert rd.redact(f"token: {secret} end") == f"token: [redacted: {kind}] end"


@pytest.mark.parametrize("text", [
    "commit 2dc4084e5f1a3b9c7d6e8f0a1b2c3d4e5f6a7b8c", "id 123e4567-e89b-12d3-a456-426614174000",
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "def sk_test(): pass",
    "ask-for-help", "risk-assessment-model-for-shell", "AKIA short",
])
def test_no_false_hits_on_ordinary_text(text):
    assert rd.redact(text) == text


def test_a_cut_off_private_key_hides_the_rest():
    assert rd.redact("x -----BEGIN RSA PRIVATE KEY-----\nMIIE\nmore") == "x [redacted: private key]"


def test_exact_values(monkeypatch):
    rd.add_secret("hunter2hunter2xyz", "openrouter api key")
    rd.add_secret("short")  # too short to hide by value
    rd.add_secret("/Users/x/.config/thing")  # a path, not a secret
    assert rd.redact("key=hunter2hunter2xyz short /Users/x/.config/thing") == \
        "key=[redacted: openrouter api key] short /Users/x/.config/thing"
    monkeypatch.setenv("MY_SERVICE_TOKEN", "abcdefghijkl1234")
    monkeypatch.setenv("HOME_NOT_SECRET", "abcdefghijkl9999")
    rd.add_env_secrets(lambda name: name.endswith("TOKEN"))
    assert rd.redact("abcdefghijkl1234 abcdefghijkl9999") == "[redacted: $MY_SERVICE_TOKEN] abcdefghijkl9999"
    assert rd.found("x abcdefghijkl1234 AKIAIOSFODNN7EXAMPLE") == ["$MY_SERVICE_TOKEN", "aws access key"]


def test_redact_value_reaches_into_lists():
    assert rd.redact_value(["AKIAIOSFODNN7EXAMPLE", 3, {"a": "AKIAIOSFODNN7EXAMPLE"}]) == [
        "[redacted: aws access key]", 3, {"a": "[redacted: aws access key]"}]


def scripted(calls):
    """A model that makes `calls` (tool, args) in order, then says done."""
    steps = iter(calls)

    async def stream(messages, info):
        if (nxt := next(steps, None)) is not None:
            yield {0: DeltaToolCall(name=nxt[0], json_args=json.dumps(nxt[1]))}
        else:
            yield "done"
    return FunctionModel(stream_function=stream)


def results(s):
    return [str(p.content) for m in s.history for p in m.parts if isinstance(p, (ToolReturnPart, RetryPromptPart))]


async def test_tool_results_and_retries_are_redacted_before_the_model_and_log(tmp_path):
    (tmp_path / "creds.txt").write_text("aws AKIAIOSFODNN7EXAMPLE\n")
    fe = ScriptedFrontend()
    s = build_session(settings(tmp_path, approval=["rules", "yolo"]), fe,
                      model=scripted([("read_file", {"path": "creds.txt"}),
                                      ("shell", {"command": "echo AKIAIOSFODNN7EXAMPLE; exit 3"})]))
    await s.send("go")
    seen = results(s)
    assert "[redacted: aws access key]" in seen[0] and "AKIAIOSFODNN7EXAMPLE" not in "".join(seen)
    logged = [e.data["content"] for e in fe.events if e.kind == "tool_result"]
    assert logged and "AKIAIOSFODNN7EXAMPLE" not in "".join(logged)


async def test_approval_detail_is_redacted(tmp_path):
    class Fe(ScriptedFrontend):
        asked = []

        async def request_approval(self, req):
            self.asked.append(req)
            return await super().request_approval(req)
    fe = Fe()
    s = build_session(settings(tmp_path), fe, model=scripted([("shell", {"command": "echo AKIAIOSFODNN7EXAMPLE"})]))
    await s.send("go")
    assert "AKIAIOSFODNN7EXAMPLE" not in fe.asked[0].detail and "[redacted" in fe.asked[0].detail


async def test_a_call_that_keeps_failing_gets_a_note_then_stops_the_turn(tmp_path):
    fe = ScriptedFrontend()
    calls = [("shell", {"command": "false"})] * 7
    s = build_session(settings(tmp_path, approval=["rules", "yolo"], tool_retries=10), fe, model=scripted(calls))
    out = await s.send("go")
    seen = results(s)
    assert len(seen) == 5  # the sixth reply was made text only
    assert "failed the same way 3 times" in seen[2] and "failed the same way" not in seen[1] + seen[3]
    assert [e.data["text"] for e in fe.events if e.kind == "warning"] == [
        "jimbo stopped this turn: shell failed the same way 5 times."]
    assert s.deps.stop_reason
    await s.send("again")  # the next turn starts clean
    assert s.deps.failures == {} or max(s.deps.failures.values()) < 5


async def test_retries_count_as_failures_too(tmp_path):
    s = build_session(settings(tmp_path, approval=["rules", "yolo"], tool_retries=10), ScriptedFrontend(),
                      model=scripted([("read_file", {"path": "missing.txt"})] * 4))
    await s.send("go")
    assert "failed the same way 3 times" in results(s)[2]


async def test_different_failures_are_counted_apart(tmp_path):
    s = build_session(settings(tmp_path, approval=["rules", "yolo"]), ScriptedFrontend(),
                      model=scripted([("shell", {"command": f"exit {i}"}) for i in range(1, 6)]))
    await s.send("go")
    assert not any("failed the same way" in r for r in results(s)) and s.deps.stop_reason is None


async def test_a_file_changed_since_it_was_read_is_not_overwritten(tmp_path):
    from types import SimpleNamespace
    from pydantic_ai import ModelRetry
    from jimbo.core.approval import ApprovalGate, Yolo
    from jimbo.core.deps import Deps
    from jimbo.tools.fs import edit_file, multi_edit, read_file, write_file, Edit
    c = SimpleNamespace(tool_call_id=None, deps=Deps(workspace=tmp_path.resolve(), gate=ApprovalGate([Yolo()])))
    f = tmp_path / "app.py"
    f.write_text("a = 1\n")
    await read_file(c, "app.py")
    await edit_file(c, "app.py", "a = 1", "a = 2")  # its own change is fine
    await edit_file(c, "app.py", "a = 2", "a = 3")
    f.write_text("a = 3\nb = 9  # the user's edit\n")
    for call in (write_file(c, "app.py", "a = 4\n"), edit_file(c, "app.py", "a = 3", "a = 4"),
                 multi_edit(c, "app.py", [Edit(old="a = 3", new="a = 4")])):
        with pytest.raises(ModelRetry, match="app.py changed since you last read it"):
            await call
    with pytest.raises(ModelRetry):  # still refused until it is read again
        await edit_file(c, "app.py", "a = 3", "a = 4")
    await read_file(c, "app.py")
    await edit_file(c, "app.py", "a = 3", "a = 4")
    assert f.read_text() == "a = 4\nb = 9  # the user's edit\n"
    (tmp_path / "never_read.py").write_text("x\n")
    await write_file(c, "never_read.py", "y\n")  # nothing to compare with
    await write_file(c, "new.py", "z\n")


async def test_a_change_during_the_approval_prompt_is_caught(tmp_path):
    from types import SimpleNamespace
    from pydantic_ai import ModelRetry
    from jimbo.core.approval import ApprovalGate, Verdict
    from jimbo.core.deps import Deps
    from jimbo.tools.fs import read_file, write_file
    f = tmp_path / "app.py"
    f.write_text("a = 1\n")

    class EditsWhileAsking:
        name = "x"

        async def evaluate(self, call):
            if call.tool == "write_file":
                f.write_text("a = 1\nb = 2\n")
            return Verdict(action="allow", decided_by="x")
    c = SimpleNamespace(tool_call_id=None, deps=Deps(workspace=tmp_path.resolve(), gate=ApprovalGate([EditsWhileAsking()])))
    await read_file(c, "app.py")
    with pytest.raises(ModelRetry, match="changed since"):
        await write_file(c, "app.py", "a = 5\n")
    assert f.read_text() == "a = 1\nb = 2\n"
