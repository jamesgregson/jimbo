import asyncio

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from helpers import ScriptedFrontend, settings
from jimbo.core.approval import MODE_WARNING, YOLO_CHAIN, ApprovalDecision
from jimbo.core.runner import run_frontend
from jimbo.wiring import build_session


def user_texts(messages):
    return [p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts
            if isinstance(p, UserPromptPart)]


def model(seen, tool="list_dir"):
    """Calls `tool` once, then answers; records the user prompts of every request."""
    def answer(messages):
        seen.append(user_texts(messages))
        if isinstance(messages[-1].parts[-1], ToolReturnPart) or len(seen[-1]) > 1 and seen[-1][-1] != "go":
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool, {"path": "."})])

    async def stream(messages, info):
        r = answer(messages).parts[0]
        if isinstance(r, ToolCallPart):
            yield {0: DeltaToolCall(name=r.tool_name, json_args='{"path": "."}')}
        else:
            yield r.content
    return FunctionModel(lambda m, i: answer(m), stream_function=stream)


class SteeringFrontend(ScriptedFrontend):
    """Steers the session when the first tool call is rendered (mid-run)."""

    def __init__(self, session_ref, steer_with="use rg instead", **kw):
        super().__init__(**kw)
        self.ref, self.steer_with = session_ref, steer_with

    async def render(self, event):
        await super().render(event)
        if event.kind == "tool_call" and self.steer_with:
            msg, self.steer_with = self.steer_with, None
            self.ref["s"].steer(msg)


async def test_steer_reaches_the_next_request_and_history(tmp_path):
    seen, ref = [], {}
    fe = SteeringFrontend(ref)
    s = ref["s"] = build_session(settings(tmp_path), fe, model=model(seen))
    assert await s.send("go") == "done"
    assert seen[0] == ["go"] and seen[1] == ["go", "use rg instead"]
    assert user_texts(s.history) == ["go", "use rg instead"]
    steer = [e for e in fe.events if e.kind == "user_message" and e.data.get("steer")]
    assert [e.data["text"] for e in steer] == ["use rg instead"]
    assert s.take_steers() == []


async def test_undelivered_steer_waits_to_be_taken(tmp_path):
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=model([]))
    s.steer("late")  # no request left to carry it: the runner sends it as the next turn
    assert s.take_steers() == ["late"] and s.take_steers() == []


async def test_cancel_discards_queued_steers(tmp_path):
    seen, ref = [], {}
    fe = SteeringFrontend(ref)
    s = ref["s"] = build_session(settings(tmp_path), fe, model=model(seen))

    async def slow(*a, **k):
        await asyncio.sleep(10)
    s.deps.authorize = slow  # the tool call hangs, so the steer is still queued when we cancel
    task = asyncio.create_task(s.send("go"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s._steers:
            break
    assert s.cancel()
    assert await task is None
    assert s.take_steers() == []
    assert any(e.kind == "command_output" and "discarded 1" in e.data["text"] for e in fe.events)


async def test_runner_steers_while_running(tmp_path):
    """A steering frontend is polled during the run; its input is delivered as a steer, not a new turn."""
    seen, gate = [], asyncio.Event()

    class Fe(ScriptedFrontend):
        supports_steering = True

        async def next_user_input(self):
            if not self.inputs:
                await gate.wait()  # nothing more to say until the run is over
                return None
            if len(self.inputs) == 1:
                await asyncio.sleep(0.05)  # typed while the run is in flight
            return self.inputs.pop(0)

        async def render(self, event):
            await super().render(event)
            if event.kind == "run_finished":
                gate.set()

    fe = Fe(["go", "steer me"])
    s = build_session(settings(tmp_path), fe, model=model(seen))
    real = s.deps.authorize

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        return await real(*a, **k)
    s.deps.authorize = slow
    await asyncio.wait_for(run_frontend(s, fe), 5)
    assert seen[-1] == ["go", "steer me"]
    assert len([e for e in fe.events if e.kind == "run_finished"]) == 1


async def test_runner_refuses_commands_during_a_run(tmp_path):
    seen, gate = [], asyncio.Event()

    class Fe(ScriptedFrontend):
        supports_steering = True

        async def next_user_input(self):
            if not self.inputs:
                await gate.wait()
                return None
            if len(self.inputs) == 1:
                await asyncio.sleep(0.05)
            return self.inputs.pop(0)

        async def render(self, event):
            await super().render(event)
            if event.kind == "run_finished":
                gate.set()

    fe = Fe(["go", "/clear"])
    s = build_session(settings(tmp_path), fe, model=model(seen))
    real = s.deps.authorize

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        return await real(*a, **k)
    s.deps.authorize = slow
    await asyncio.wait_for(run_frontend(s, fe), 5)
    assert any(e.kind == "command_output" and "can't run while" in e.data["text"] for e in fe.events)
    assert s.history  # /clear did not run


@pytest.mark.parametrize("yes", [True, False])
async def test_runner_runs_approve_during_a_run(tmp_path, yes):
    """/approve is a command, not a steer, mid-run; it asks to confirm through the frontend that is still reading."""
    seen, gate, asked = [], asyncio.Event(), []

    class Fe(ScriptedFrontend):
        supports_steering = True

        reading = False

        async def next_user_input(self):
            if not self.inputs:
                self.reading = True
                await gate.wait()
                return None
            if len(self.inputs) == 1:
                await asyncio.sleep(0.05)
            return self.inputs.pop(0)

        async def confirm(self, message):
            asked.append((message, self.reading))
            return yes

        async def render(self, event):
            await super().render(event)
            if event.kind == "run_finished":
                gate.set()

    fe = Fe(["go", "/approve yolo"])
    s = build_session(settings(tmp_path), fe, model=model(seen))
    before = list(s.approval_modes.names)
    real = s.deps.authorize

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        return await real(*a, **k)
    s.deps.authorize = slow
    await asyncio.wait_for(run_frontend(s, fe), 5)
    assert asked == [(MODE_WARNING, True)]  # the prompt was up to take the answer
    assert s.approval_modes.names == (YOLO_CHAIN if yes else before)
    assert all("/approve yolo" not in texts for texts in seen)  # never sent to the model


async def test_runner_reads_input_before_a_command_asks_to_confirm(tmp_path):
    """Like tui, a steering frontend takes a confirm answer through its prompt, so the prompt must be up first.
    Starting the command before the read opened a second prompt ("Application is already running")."""
    asked = []

    class Fe(ScriptedFrontend):
        supports_steering = True
        reading = False

        async def next_user_input(self):
            if self.inputs:
                return self.inputs.pop(0)
            self.reading = True
            await asyncio.sleep(0.1)
            return None

        async def confirm(self, message):
            asked.append(self.reading)
            return True

    fe = Fe(["/approve yolo"])
    s = build_session(settings(tmp_path), fe, model=model([]))
    await asyncio.wait_for(run_frontend(s, fe), 5)
    assert asked == [True]
    assert s.approval_modes.names == YOLO_CHAIN


async def test_denial_note_reaches_the_model(tmp_path):
    class Fe(ScriptedFrontend):
        async def request_approval(self, req):
            return ApprovalDecision(allow=False, note="use rg instead")

    results = []

    def answer(messages, info):
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart) or last.part_kind == "retry-prompt":
            results.append(str(last.content))
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(parts=[ToolCallPart("shell", {"command": "grep -r foo ."})])

    async def stream(messages, info):
        r = answer(messages, info).parts[0]
        if isinstance(r, ToolCallPart):
            yield {0: DeltaToolCall(name="shell", json_args='{"command": "grep -r foo ."}')}
        else:
            yield r.content
    fe = Fe()
    s = build_session(settings(tmp_path, tasks={"jimbo-shell-assess": {"mode": "off"}}), fe,
                      model=FunctionModel(answer, stream_function=stream))
    await s.send("find foo")
    assert results and "User denied this action: use rg instead" in results[0]


async def test_quit_finishes_the_open_prompt_before_returning(tmp_path):
    """/quit while the prompt stays up (tui): the read is cancelled and wound down before run_frontend returns, so
    what jimbo prints on exit (the cost summary) isn't sent to a prompt that is going away."""
    closed = []

    class Fe(ScriptedFrontend):
        supports_steering = True

        async def next_user_input(self):
            if self.inputs:
                return self.inputs.pop(0)
            try:
                await asyncio.Event().wait()  # the prompt, waiting for the next line
            finally:
                closed.append(True)  # tui leaves patch_stdout here

    fe = Fe(["/quit"])
    s = build_session(settings(tmp_path), fe, model=model([]))
    await asyncio.wait_for(run_frontend(s, fe), 1)
    assert closed == [True]


async def test_a_skill_typed_mid_run_steers_it(tmp_path):
    seen, gate = [], asyncio.Event()
    d = tmp_path / ".jimbo" / "skills" / "quiet"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: q\ninteractive: false\n---\nWork alone.")

    class Fe(ScriptedFrontend):
        supports_steering = True

        async def next_user_input(self):
            if not self.inputs:
                await gate.wait()
                return None
            if len(self.inputs) == 1:
                await asyncio.sleep(0.05)  # typed while the run is in flight
            return self.inputs.pop(0)

        async def render(self, event):
            await super().render(event)
            if event.kind == "run_finished":
                gate.set()

    fe = Fe(["go", "/quiet focus on tests"])
    s = build_session(settings(tmp_path), fe, model=model(seen))
    real = s.deps.authorize
    quiet_during_run = []

    async def slow(*a, **k):
        await asyncio.sleep(0.2)
        quiet_during_run.append(s.deps.no_questions)
        return await real(*a, **k)
    s.deps.authorize = slow
    await asyncio.wait_for(run_frontend(s, fe), 5)
    assert seen[-1][0] == "go" and seen[-1][1].startswith("Use the skill 'quiet'")
    assert seen[-1][1].endswith("Work alone.\n\nfocus on tests")
    assert quiet_during_run == ["quiet"]  # the skill's frontmatter applies at once
    assert len([e for e in fe.events if e.kind == "run_finished"]) == 1  # one turn: a steer, not a new run
    assert not [e for e in fe.events if e.kind == "command_output" and not e.data["ok"]]
