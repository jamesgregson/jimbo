from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied
from ..core.questions import Answers, Question, check_questions, format_answers


async def ask_user(ctx: RunContext[Deps], questions: list[Question]) -> str:
    """Ask the user one or more questions and wait for the answers.

    Use it when a decision is the user's to make and you can't settle it from the request or the code. Each
    question is `yes_no`, or a `choice` between 2 or more options; set `multiple` to let the user pick any number
    of them. Put your recommended option first and add " (recommended)" to it. Ask related questions together, a
    few at a time.
    The user can stop answering at any point to tell you something instead. Then the result has their words:
    respond to them and ask new questions if you still need to.
    """
    if problem := check_questions(questions):
        raise ModelRetry(problem)
    n = len(questions)
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "ask_user", {"count": n}, f"ask {n} question(s)",
                                 read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    got = await ctx.deps.ask_user(questions)
    got = Answers(answers=(got.answers + [None] * n)[:n], feedback=(got.feedback or "").strip() or None)
    return format_answers(questions, got)


async def _only_with_a_person(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.can_ask else None


ASK_TOOLS = [Tool(ask_user, prepare=_only_with_a_person)]
