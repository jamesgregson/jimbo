"""ask_user: questions from the model, the answers, and the user stopping a round to say something else."""

import asyncio
import json
from io import StringIO

from aiohttp.test_utils import TestClient, TestServer
from pydantic_ai.messages import ModelResponse, TextPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from helpers import ScriptedFrontend, settings
from jimbo.core.questions import Answers, Question, check_answer, check_questions, format_answers, parse_reply
from jimbo.core.skills import load_skills
from jimbo.frontends.tui import TuiFrontend
from jimbo.frontends.web import WebFrontend
from jimbo.wiring import build_session
from test_plan import model

DB = Question(question="Which database?", options=["SQLite (recommended)", "Postgres"])
CACHE = Question(question="Add a cache?", kind="yes_no")
QUESTIONS = [DB, CACHE]
ARGS = {"questions": [q.model_dump() for q in QUESTIONS]}


class Asked(ScriptedFrontend):
    def __init__(self, *replies: Answers):
        super().__init__()
        self.replies, self.asked = list(replies), []

    async def ask_questions(self, questions):
        self.asked.append(questions)
        return self.replies.pop(0)


def results(s):
    return [str(p.content) for m in s.history for p in m.parts if isinstance(p, ToolReturnPart)]


# ---- replies ----

def test_a_reply_is_a_number_an_option_or_yes_no():
    assert parse_reply(DB, "2") == parse_reply(DB, " postgres ") == "Postgres"
    assert parse_reply(DB, "3") is None and parse_reply(DB, "0") is None
    assert parse_reply(CACHE, "Y") == "yes" and parse_reply(CACHE, "no") == "no"
    assert parse_reply(CACHE, "yes, but only in prod") is None


def test_questions_that_cant_be_asked():
    assert check_questions([]) and check_questions([Question(question=" ", kind="yes_no")])
    assert "2 or more" in check_questions([Question(question="q", options=["a", "A"])])
    assert "no options" in check_questions([Question(question="q", kind="yes_no", options=["a", "b"])])
    assert check_questions(QUESTIONS) is None


def test_feedback_tells_the_model_to_ask_again():
    text = format_answers(QUESTIONS, Answers(answers=["Postgres", None], feedback="this is a CLI, no server"))
    assert "Answer: Postgres" in text and "Answer: (not answered)" in text
    assert '"this is a CLI, no server"' in text and "call ask_user again" in text
    assert "ask_user" not in format_answers(QUESTIONS, Answers(answers=["Postgres", "no"]))


# ---- the tool ----

async def test_the_model_gets_the_answers(tmp_path):
    fe = Asked(Answers(answers=["Postgres", "no"]))
    s = build_session(settings(tmp_path), fe, model=model([("ask_user", ARGS)]))
    await s.send("plan it")
    assert fe.asked == [QUESTIONS]
    assert "Which database?\n   Answer: Postgres" in results(s)[0] and "Add a cache?\n   Answer: no" in results(s)[0]


async def test_feedback_and_missing_answers_reach_the_model(tmp_path):
    fe = Asked(Answers(answers=["Postgres"], feedback="  wrong direction  "))  # short list: the rest unanswered
    s = build_session(settings(tmp_path), fe, model=model([("ask_user", ARGS)]))
    await s.send("plan it")
    assert '"wrong direction"' in results(s)[0] and "(not answered)" in results(s)[0]


async def test_bad_questions_are_retried_without_asking(tmp_path):
    fe = Asked()
    s = build_session(settings(tmp_path), fe, model=model([("ask_user", {"questions": []})]))
    await s.send("go")
    assert fe.asked == [] and "at least one" in results(s)[0]


async def test_no_tool_when_nobody_can_answer(tmp_path):
    seen = []

    async def stream(messages, info):
        seen.append({t.name for t in info.function_tools})
        yield "ok"

    for fe in (ScriptedFrontend(), Asked()):
        s = build_session(settings(tmp_path), fe, model=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")]),
                                                                       stream_function=stream))
        await s.send("hi")
    assert "ask_user" not in seen[0] and "ask_user" in seen[1]


# ---- tui ----

def tui(lines):
    async def ask(_msg):
        return await lines.get()

    out = StringIO()
    return TuiFrontend(Console(file=out, force_terminal=False, width=100), ask), out


async def test_tui_asks_each_question_in_turn():
    lines: asyncio.Queue[str] = asyncio.Queue()
    for line in ["", "1", "y"]:  # an empty line asks again
        lines.put_nowait(line)
    fe, out = tui(lines)
    got = await asyncio.wait_for(fe.ask_questions(QUESTIONS), 1)
    assert got == Answers(answers=["SQLite (recommended)", "yes"])
    assert "Question 1 of 2" in out.getvalue() and "2. Postgres" in out.getvalue()


async def test_tui_typing_instead_of_answering_stops_the_round():
    lines: asyncio.Queue[str] = asyncio.Queue()
    fe, out = tui(lines)
    reading = asyncio.create_task(fe.next_user_input())  # the main prompt, up during a run
    await asyncio.sleep(0.01)
    asked = asyncio.create_task(fe.ask_questions(QUESTIONS))
    await asyncio.sleep(0.01)
    await lines.put("2")
    await asyncio.sleep(0.01)
    await lines.put("forget the database, it's a CLI")
    got = await asyncio.wait_for(asked, 1)
    assert got == Answers(answers=["Postgres", None], feedback="forget the database, it's a CLI")
    assert "Question 2 of 2" in out.getvalue() and not reading.done()
    await lines.put("next")  # the questions are closed: this is a steer again
    assert await asyncio.wait_for(reading, 1) == "next"


async def test_tui_closed_input_returns_what_was_answered():
    async def ask(_msg):
        raise EOFError

    fe = TuiFrontend(Console(file=StringIO()), ask)
    assert await fe.ask_questions(QUESTIONS) == Answers(answers=[None, None])


# ---- web ----

async def web():
    fe = WebFrontend(open_browser=False)
    c = TestClient(TestServer(fe.build_app()))
    await c.start_server()
    return fe, c, await c.ws_connect(f"/ws?token={fe.token}")


async def test_web_answers_roundtrip():
    fe, c, ws = await web()
    try:
        asked = asyncio.create_task(fe.ask_questions(QUESTIONS))
        msg = await ws.receive_json()
        assert msg["type"] == "questions" and msg["questions"][1]["kind"] == "yes_no"
        await ws.send_json({"type": "answers", "id": msg["id"], "answers": ["Postgres", "maybe"]})  # not a choice
        assert await asyncio.wait_for(asked, 1) == Answers(answers=["Postgres", None])
        assert await ws.receive_json() == {"type": "questions_closed", "id": msg["id"]}
    finally:
        await c.close()


async def test_web_feedback_stops_the_round_from_the_dialog_or_the_input():
    fe, c, ws = await web()
    try:
        asked = asyncio.create_task(fe.ask_questions(QUESTIONS))
        msg = await ws.receive_json()
        await ws.send_json({"type": "answers", "id": msg["id"], "answers": ["Postgres", None], "feedback": " no db "})
        assert await asyncio.wait_for(asked, 1) == Answers(answers=["Postgres", None], feedback="no db")
        await ws.receive_json()
        asked = asyncio.create_task(fe.ask_questions(QUESTIONS))
        await ws.receive_json()
        await ws.send_json({"type": "send", "text": "stop, wrong feature"})
        assert (await asyncio.wait_for(asked, 1)).feedback == "stop, wrong feature"
        assert fe._inputs.empty()  # it answered the questions; it isn't also a message
        assert "stop, wrong feature" not in json.dumps(fe._history)
    finally:
        await c.close()


async def test_web_page_has_the_questions_dialog():
    fe, c, _ = await web()
    try:
        text = await (await c.get("/")).text()
        assert "function showQuestions" in text and '"answers"' in text
    finally:
        await c.close()


# ---- multi-select ----

TOOLS = Question(question="Which tools?", options=["ruff", "mypy", "black"], multiple=True)


def test_multi_select_replies():
    assert parse_reply(TOOLS, "1,3") == parse_reply(TOOLS, "3 1") == ["ruff", "black"]
    assert parse_reply(TOOLS, "mypy") == ["mypy"] and parse_reply(TOOLS, "none") == []
    assert parse_reply(TOOLS, "1,4") is None and parse_reply(TOOLS, "ruff and mypy") is None
    assert check_answer(TOOLS, ["black", "ruff", "nope"]) == ["ruff", "black"] and check_answer(TOOLS, "ruff") is None
    assert "Answer: ruff, black" in format_answers([TOOLS], Answers(answers=[["ruff", "black"]]))
    assert "Answer: (none)" in format_answers([TOOLS], Answers(answers=[[]]))
    assert check_questions([Question(question="q", kind="yes_no", multiple=True)])


async def test_web_multi_select():
    fe, c, ws = await web()
    try:
        asked = asyncio.create_task(fe.ask_questions([TOOLS]))
        await ws.send_json({"type": "answers", "id": (await ws.receive_json())["id"], "answers": [["mypy", "x"]]})
        assert await asyncio.wait_for(asked, 1) == Answers(answers=[["mypy"]])
    finally:
        await c.close()


# ---- skills that run without user interaction ----

def skill(tmp_path, name, front=""):
    d = tmp_path / ".jimbo" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\ndescription: {name}\n{front}---\nask the user")


async def tools_seen(tmp_path, calls, prompt):
    seen, it = [], iter(calls)

    async def stream(messages, info):
        seen.append({t.name for t in info.function_tools})
        if (nxt := next(it, None)) is not None:
            yield {0: DeltaToolCall(name=nxt[0], json_args=json.dumps(nxt[1]))}
        else:
            yield "done"

    s = build_session(settings(tmp_path), Asked(), model=FunctionModel(stream_function=stream))
    await (s.commands.dispatch(s, prompt) if prompt.startswith("/") else s.send(prompt))
    return s, seen


async def test_a_quiet_skill_hides_questions_for_the_turn(tmp_path):
    skill(tmp_path, "nightly", "interactive: false\n")
    skill(tmp_path, "chatty")
    s, seen = await tools_seen(tmp_path, [("load_skill", {"name": "chatty"}), ("load_skill", {"name": "nightly"}),
                                          ("load_skill", {"name": "chatty"})], "go")
    assert "ask_user" in seen[0] and "ask_user" in seen[1] and "ask_user" not in seen[2]
    assert "decide yourself" not in results(s)[0] and "'nightly' runs without user interaction" in results(s)[2]
    assert s.deps.no_questions is None  # only for that turn
    s, seen = await tools_seen(tmp_path, [], "/nightly")
    assert "ask_user" not in seen[0]


async def test_skill_interactive_flag_parses(tmp_path):
    skill(tmp_path, "a", "interactive: false\n")
    skill(tmp_path, "b")
    skills = load_skills([tmp_path / ".jimbo" / "skills"])
    assert skills["a"].interactive is False and skills["b"].interactive is True
