"""Questions the model asks the user with ask_user, and what the user's replies mean."""

import re
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _require_options(schema: dict) -> None:
    # Required in the schema the model sees, so it can't leave the field out (some providers decode against
    # it), while code that builds a yes_no Question can still omit it.
    schema["required"] = [*schema.get("required", []), "options"]


class Question(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_options)

    question: str
    kind: Literal["yes_no", "choice"] = "choice"
    options: list[str] = Field(default_factory=list,
                               description="for kind 'choice': 2 or more options; for kind 'yes_no': []")
    multiple: bool = Field(False, description="for kind 'choice': the user may pick any number of options")


Answer = str | list[str]  # a list for a question with `multiple`, in the order of the options


class Answers(BaseModel):
    answers: list[Answer | None]  # one per question; None: not answered
    feedback: str | None = None  # the user stopped answering to say this


def check_questions(questions: list[Question]) -> str | None:
    """Why these questions can't be asked, or None."""
    if not questions:
        return "ask at least one question"
    for i, q in enumerate(questions, 1):
        if not q.question.strip():
            return f"question {i} is empty"
        if q.kind == "choice" and not any(o.strip() for o in q.options):
            return (f"question {i} has no `options`. List 2 or more in `options`, or, if it has no short list "
                    "of answers, ask it in a message instead of with ask_user")
        if q.kind == "choice" and len({o.strip().lower() for o in q.options if o.strip()}) < 2:
            return f"question {i}: a choice needs 2 or more different options in `options`"
        if q.kind == "yes_no" and (q.options or q.multiple):
            return f"question {i}: a yes_no question takes no options"
    return None


def choices(q: Question) -> list[str]:
    return ["yes", "no"] if q.kind == "yes_no" else q.options


def check_answer(q: Question, raw) -> Answer | None:
    """`raw` (from a frontend's form) as an answer to `q`, or None if it isn't one."""
    if q.multiple:
        return [o for o in q.options if o in raw] if isinstance(raw, list) else None
    return raw if isinstance(raw, str) and raw in choices(q) else None


def parse_reply(q: Question, text: str) -> Answer | None:
    """The answer a typed line gives, or None if it isn't one: y/yes/n/no, a choice's number or text, or for
    `multiple` the numbers of the picks ("1,3" or "1 3") or "none"."""
    t = text.strip().lower()
    if q.kind == "yes_no":
        return {"y": "yes", "yes": "yes", "n": "no", "no": "no"}.get(t)
    option = next((o for o in q.options if o.strip().lower() == t), None)
    if q.multiple:
        if option is not None:
            return [option]
        if t == "none":
            return []
        picks = re.split(r"[\s,]+", t)
        if not all(n.isdigit() and 1 <= int(n) <= len(q.options) for n in picks):
            return None
        wanted = {int(n) for n in picks}
        return [o for n, o in enumerate(q.options, 1) if n in wanted]
    if t.isdigit() and 1 <= int(t) <= len(q.options):
        return q.options[int(t) - 1]
    return option


def reply_prompt(q: Question) -> str:
    n = len(q.options)
    keys = ("[y]es / [n]o" if q.kind == "yes_no" else f"[1-{n}], several like 1,3, or none" if q.multiple
            else f"[1-{n}]")
    return f"{keys}, or type anything else to stop and tell jimbo: "


async def ask_by_line(questions: list[Question], show: Callable[[int, Question], None],
                      line: Callable[[str], Awaitable[str]]) -> Answers:
    """Ask each question in turn, reading answers with `line`. A line that isn't an answer stops the round and
    becomes the feedback; an empty line asks again. A closed input (EOF) or Ctrl+C stops with what was answered."""
    answers: list[Answer | None] = [None] * len(questions)
    for i, q in enumerate(questions):
        show(i, q)
        while True:
            try:
                text = await line(reply_prompt(q))
            except (EOFError, KeyboardInterrupt):
                return Answers(answers=answers)
            if not text.strip():
                continue
            if (answer := parse_reply(q, text)) is None:
                return Answers(answers=answers, feedback=text.strip())
            answers[i] = answer
            break
    return Answers(answers=answers)


def _show(a: Answer | None) -> str:
    if a is None:
        return "(not answered)"
    return (", ".join(a) or "(none)") if isinstance(a, list) else a


def format_answers(questions: list[Question], got: Answers) -> str:
    """The ask_user result the model reads."""
    lines = [f"{i}. {q.question}\n   Answer: {_show(a)}" for i, (q, a) in enumerate(zip(questions, got.answers), 1)]
    if got.feedback:
        lines.append(f'\nThe user stopped answering to say: "{got.feedback}"\n'
                     "The answers above stand unless this changes them. Respond to what the user said. If you still "
                     "need answers, call ask_user again with new questions that take it into account; don't repeat "
                     "the unanswered ones unchanged.")
    elif any(a is None for a in got.answers):
        lines.append("\nThe user didn't answer every question.")
    return "\n".join(lines)
