import json
import platform
import re
from dataclasses import dataclass, replace

from pydantic_ai import Agent, ModelRetry, RunContext, Tool
from pydantic_ai.exceptions import ToolRetryError
from pydantic_ai.capabilities import AbstractCapability, ProcessHistory
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import Model, ModelRequestContext

from .config import Settings
from .core.agents import AgentSpec, format_agent_index
from .core.deps import Deps
from .core.redact import redact, redact_value
from .core.skills import format_skill_index
from .tools.agents import AGENT_TOOLS
from .tools.ask import ASK_TOOLS
from .tools.compact import COMPACT_TOOLS
from .tools.memory import MEMORY_TOOLS
from .tools.skills import RELOAD_TOOLS, SKILL_TOOLS
from .tools.fs import FS_TOOLS
from .tools.lsp import LSP_TOOLS
from .tools.search import SEARCH_TOOLS
from .tools.plan import PLAN_TOOLS
from .tools.shell import SHELL_TOOLS
from .tools.show import SHOW_TOOLS
from .tools.todos import TODO_TOOLS
from .tools.web import WEB_TOOLS

SYSTEM_PROMPT = """\
You are jimbo, a coding agent working inside a single workspace directory on {os}.
Use the provided tools to inspect the code before answering; do not guess file contents.
Find files with glob and search text with grep; read them with read_file.
You can create and modify files with write_file (whole file), edit_file (exact replace) and multi_edit
(several replacements in one file); read a file before editing it. Delete and move files with delete_file and
move_file, not shell, so /undo can reverse it. Paths are relative to the workspace root; use an absolute path
for the scratch directory.
Use shell to run commands (tests, builds, git); it is non-interactive and runs in the workspace root. Run
servers and other long-running commands with background=true.
Use web_fetch to read documentation and other web pages.
For tasks with 3 or more steps, plan them with todo_write first and update it as you progress.
Be concise.
If a tool call is denied, do not retry the same call; explain what you wanted to do and why instead.
Work only inside the workspace and the scratch directory. Don't read or list files elsewhere (the home folder,
other projects, system folders) unless the user asks; ask the user if you need something outside.
Don't commit, push, or change branches, tags or the stash unless the user asks for it in this conversation. Leave
changes uncommitted. Never change a remote (git push, gh pr create, ...): jimbo always denies it.
"""


PLAN_PROMPT = """\
# Plan mode
You are in plan mode: only read-only tools work (read_file, list_dir, glob, grep, the lsp_ tools, web_fetch,
run_agent, todo_write); edits and shell are denied.
Investigate the code, then call exit_plan(plan) with a concrete plan as a markdown list of steps
(one "- " or "1. " item per step). The user reviews it; once approved, plan mode ends and you can execute it.
If the plan is rejected, revise it using the feedback and call exit_plan again."""


AGENT_PROMPT = """\
# Agent
You are the agent "{name}", doing one task. Nobody can answer questions while you work: where your
instructions or a skill say to ask the user, decide yourself and say what you decided in your report.
Your final message is your report, and it is the only thing returned, so put everything the reader needs in it.

{body}"""


AGENT_PLAN_PROMPT = """\
# Plan mode
The session is in plan mode: only read-only tools work; edits and shell are denied. Report what you find."""


# Tool -> when to use it. Only the tools a session or agent has are listed.
LSP_USES = {
    "lsp_symbols": "the functions and classes in a file: lsp_symbols(path)",
    "lsp_definition": "where something is defined, and its type: lsp_definition(path, line, symbol)",
    "lsp_references": "every caller or use of something: lsp_references(path, line, symbol)",
    "lsp_rename": "renaming a function, class or variable: lsp_rename(path, line, symbol, new_name), "
                  "not edit_file on each file",
    "lsp_diagnostics": "errors in a file: lsp_diagnostics(path)",
}


def lsp_prompt(deps: Deps) -> str:
    tools = deps.agent.tools if deps.agent and deps.agent.tools is not None else LSP_USES
    lines = [f"- {use}" for name, use in LSP_USES.items() if name in tools]
    text = (f"# Language servers\nFor {'; '.join(deps.lsp.languages())} files, use these instead of grep:\n"
            + "\n".join(lines) + "\nUse grep for text: strings, comments, config files and other languages.")
    if deps.lsp.diagnostics:
        text += "\nwrite_file and edit_file results list errors an edit introduced; fix them."
    return text


WRAP_UP_REQUESTS = 3  # warn the model when this many requests (or fewer) are left in the turn

WRAP_UP_PROMPT = """\
# Wrap up now
You have {left} model request(s) left in this turn, including this one; after that the turn is stopped.
Do not start new work. Leave the workspace in a sensible state: finish or undo any half-made edit,
and update the todo list. Then end with a message saying what is done, what is not, and what to do next.
{last}"""
LAST_REQUEST_NOTE = "This is the last request: tools are turned off, so reply with text only."
NO_FINAL_MESSAGE = ("[The turn reached its request limit before the model wrote a final message. "
                    "Send a message to continue.]")
# Tool calls some models write as text when they can't make a real one, e.g. <tool_call>read_file<arg_key>...
TOOL_CALL_TEXT = re.compile(r"<\|?(tool_call|function_call)\|?>.*?(</\1>|$)", re.DOTALL)


def requests_left(ctx: RunContext[Deps]) -> int | None:
    """Model requests left in the turn, including the one about to be made."""
    limit = ctx.deps.request_limit
    return None if limit is None else limit - ctx.usage.requests


def wrap_up_notice(left: int | None) -> str | None:
    if left is None or left > WRAP_UP_REQUESTS:
        return None
    return WRAP_UP_PROMPT.format(left=left, last=LAST_REQUEST_NOTE if left <= 1 else "").rstrip()


def text_only(response: ModelResponse) -> ModelResponse:
    """`response` without tool calls, real or written as text. Says so if no text is left."""
    parts = []
    for part in response.parts:
        if isinstance(part, ToolCallPart):
            continue
        if isinstance(part, TextPart):
            if not (text := TOOL_CALL_TEXT.sub("", part.content).strip()):
                continue
            part = replace(part, content=text)
        parts.append(part)
    if not any(isinstance(p, TextPart) for p in parts):
        parts.append(TextPart(NO_FINAL_MESSAGE))
    return replace(response, parts=parts)


REPEAT_NOTICE, REPEAT_STOP = 3, 5  # the same call failing the same way: tell the model, then stop the turn
REPEAT_NOTE = "[jimbo: this call failed the same way {n} times. Try something else, or stop and tell the user what is wrong.]"
STOP_NOTE = ("jimbo stopped this turn: {tool} failed the same way {n} times. Reply with text only: tell the user "
             "what you tried and what failed.")


def _failed(tool: str, result) -> bool:
    """A tool result that reports a failure without raising: a shell command that exited non-zero or timed out."""
    return tool == "shell" and isinstance(result, str) and (
        (result.startswith("exit code ") and not result.startswith("exit code 0\n") and result != "exit code 0")
        or result.startswith("timed out"))


def _repeat(deps: Deps, call: ToolCallPart, outcome: str) -> str:
    """Count this failure. The note to add to the result at REPEAT_NOTICE, and the stop at REPEAT_STOP."""
    key = (call.tool_name, json.dumps(call.args_as_dict(), sort_keys=True, default=str), outcome[:4000])
    n = deps.failures[key] = deps.failures.get(key, 0) + 1
    if n >= REPEAT_STOP and deps.stop_reason is None:
        deps.stop_reason = STOP_NOTE.format(tool=call.tool_name, n=n)
    return "\n" + REPEAT_NOTE.format(n=n) if n == REPEAT_NOTICE else ""


@dataclass
class Guard(AbstractCapability[Deps]):
    """Every tool result and retry message has secrets hidden (core/redact.py) before the model and the session log
    see it. A call that fails the same way again and again gets a note, and then stops the turn: the next reply is
    text only."""

    def get_model_settings(self):
        def settings(ctx: RunContext[Deps]):
            return {"tool_choice": "none"} if ctx.deps.stop_reason else {}
        return settings

    async def wrap_tool_execute(self, ctx: RunContext[Deps], *, call, tool_def, args, handler):
        try:
            result = await handler(args)
        except ModelRetry as e:
            message = redact(str(e))
            raise ModelRetry(message + _repeat(ctx.deps, call, message)) from None
        except ToolRetryError as e:  # what a tool's ModelRetry arrives as: the message is in a part
            part = e.tool_retry
            message = redact(part.content) if isinstance(part.content, str) else redact_value(part.content)
            if isinstance(message, str):
                message += _repeat(ctx.deps, call, message)
            raise ToolRetryError(replace(part, content=message)) from None
        result = redact_value(result)
        if _failed(call.tool_name, result):
            result += _repeat(ctx.deps, call, result)
        return result

    async def before_model_request(self, ctx: RunContext[Deps], request_context: ModelRequestContext
                                   ) -> ModelRequestContext:
        messages = request_context.messages
        if (reason := ctx.deps.stop_reason) and messages and isinstance(last := messages[-1], ModelRequest):
            request_context.messages = [*messages[:-1], replace(last, parts=[*last.parts, UserPromptPart(reason)])]
        return request_context

    async def after_model_request(self, ctx: RunContext[Deps], *, request_context: ModelRequestContext,
                                  response: ModelResponse) -> ModelResponse:
        return text_only(response) if ctx.deps.stop_reason else response


@dataclass
class WrapUp(AbstractCapability[Deps]):
    """Near the turn's request limit, tell the model to wrap up, and make its last reply text only.

    The notice is added to the request's last message, not the instructions, because a model deep in a tool loop
    follows recent messages more than the system prompt. It is not saved in the history. The last request keeps
    its tools but sets tool_choice "none": some models that see the tools vanish write their calls as text. A
    call made there anyway, or written as text, is removed, because it would end the turn with no final message.
    """

    def get_model_settings(self):
        def settings(ctx: RunContext[Deps]):
            left = requests_left(ctx)
            return {"tool_choice": "none"} if left is not None and left <= 1 else {}
        return settings

    async def before_model_request(self, ctx: RunContext[Deps], request_context: ModelRequestContext
                                   ) -> ModelRequestContext:
        messages = request_context.messages
        notice = wrap_up_notice(left := requests_left(ctx))
        if notice and messages and isinstance(last := messages[-1], ModelRequest):
            request_context.messages = [*messages[:-1], replace(last, parts=[*last.parts, UserPromptPart(notice)])]
            ctx.deps.wrap_up_left = left
        return request_context

    async def after_model_request(self, ctx: RunContext[Deps], *, request_context: ModelRequestContext,
                                  response: ModelResponse) -> ModelResponse:
        left = requests_left(ctx)  # this request is counted only once its response is committed
        return text_only(response) if left is not None and left <= 1 else response


def project_context(ctx: RunContext[Deps]) -> str:
    """Dynamic instructions: AGENTS.md contents and the skill and agent indexes, read from deps on every
    request."""
    deps, parts = ctx.deps, []
    if deps.mode == "plan":
        parts.append(AGENT_PLAN_PROMPT if deps.agent else PLAN_PROMPT)
    if not deps.git_write:
        parts.append("# Git\ngit is read-only: only commands that read, such as status, diff, log, show and blame, "
                     "work. Commands that change the repository are denied, even inside a longer command.")
    if deps.read_paths:
        parts.append("# Library source\nread_file, grep, glob and list_dir can also read these folders, by absolute "
                     "path. Use them, not shell, to read a library's source:\n"
                     + "\n".join(f"- {p}" for p in deps.read_paths))
    if deps.project_instructions:
        parts.append(deps.project_instructions)
    if deps.scratch:
        parts.append(f"# Scratch directory\nPut throwaway files that don't belong in the project (one-off scripts, "
                     f"screenshots, test output) in {deps.scratch}, not /tmp. The file tools and shell can both use "
                     "it, by absolute path. It is kept for this session but deleted after a few days unused, so put "
                     "anything to keep in the workspace.")
    for name in deps.agent.skills if deps.agent else ():
        if skill := deps.skills.get(name):
            parts.append(f"# Skill {name}\nSkill directory: {skill.path.parent}\n\n{skill.body}")
    active = deps.skills.get(deps.active_skill or "") if deps.agent is None else None
    if active:  # in the instructions, not the conversation: it is never read as a new request or compacted away
        parts.append(f"# Active skill: {active.name}\nThe user started this skill. Its instructions follow. "
                     f"Follow them in every turn until the skill is finished.\n"
                     f"Skill directory: {active.path.parent}\n\n{active.body}")
    others = {n: s for n, s in deps.skills.items() if s is not active}
    if others and deps.can_load_skills:
        parts.append("# Skills\nBefore you start a task, check this list. If a skill's description fits the "
                     "task, even partly, call load_skill(name) first and follow its instructions. Check the "
                     "list again before you finish: some skills apply after a change, such as one that "
                     "records it:\n"
                     + format_skill_index(others))
    if deps.can_use_lsp:
        parts.append(lsp_prompt(deps))
    if index := format_agent_index(deps.agents):
        parts.append("# Agents\nCall run_agent(agent, task) to hand a self-contained task to an agent. It starts "
                     "with no conversation history and returns one report:\n" + index)
    return "\n\n".join(parts)


async def midrun_history(ctx: RunContext[Deps], messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor run before every model request: long tool loops compact between requests, not
    only between turns, and messages the user typed during the run (steering) join the next request."""
    if hook := ctx.deps.midrun_compact:
        messages = await hook(messages)
    if hook := ctx.deps.midrun_steer:  # after compaction, so a fresh steer is never summarized away
        messages = await hook(messages)
    return messages


def _tool_name(tool) -> str:
    return tool.name if isinstance(tool, Tool) else tool.__name__


ALL_TOOLS = [*FS_TOOLS, *SEARCH_TOOLS, *SHELL_TOOLS, *WEB_TOOLS, *TODO_TOOLS, *SKILL_TOOLS, *LSP_TOOLS]  # what agents may have
AGENT_TOOL_NAMES = {_tool_name(t) for t in ALL_TOOLS}


def build_agent(settings: Settings, model: Model, spec: AgentSpec | None = None) -> Agent[Deps, str]:
    """The main agent, or with `spec` an agent run: its own prompt, and only the tools it lists."""
    system = SYSTEM_PROMPT.format(os=platform.system())
    if spec is None:
        instructions, tools = [system], [*ALL_TOOLS, *AGENT_TOOLS, *PLAN_TOOLS, *ASK_TOOLS, *COMPACT_TOOLS,
                                         *RELOAD_TOOLS, *MEMORY_TOOLS, *SHOW_TOOLS]
    else:
        instructions = [system, AGENT_PROMPT.format(name=spec.name, body=spec.body)]
        tools = [t for t in ALL_TOOLS if spec.tools is None or _tool_name(t) in spec.tools]
    return Agent(
        model,
        deps_type=Deps,
        instructions=[*instructions, project_context],
        tools=tools,
        retries=settings.tool_retries,
        capabilities=[ProcessHistory(midrun_history), WrapUp(), Guard()],
    )
