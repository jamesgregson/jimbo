from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied
from ..core.skills import reload_and_describe, use_skill


async def load_skill(ctx: RunContext[Deps], name: str) -> str:
    """Return the full instructions of the skill `name` (see the skill list in your instructions).

    Load a skill before starting a task its description fits, then follow it.
    """
    skill = ctx.deps.skills.get(name)
    if skill is None:
        raise ModelRetry(f"unknown skill {name!r}; available: {', '.join(ctx.deps.skills) or 'none'}")
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "load_skill", {"name": name}, f"load skill {name}", read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    quiet = ctx.deps.no_questions  # before use_skill: a skill that turns questions off doesn't need telling
    note = use_skill(ctx.deps, skill)
    warn = f"\n\n[note: this skill's model choices were not applied: {note}]" if note else ""
    if quiet and quiet != skill.name:
        warn += (f"\n\n[note: the skill {quiet!r} runs without user interaction. Where these instructions say "
                 "to ask the user, decide yourself and say what you decided.]")
    return f"Skill directory: {skill.path.parent}\n\n{skill.body}{warn}"


async def reload_skills(ctx: RunContext[Deps]) -> str:
    """Read the skill files again, so skills added, changed or deleted in this session apply now.

    Call it when the user asks you to reload skills, and after you create, edit or delete a SKILL.md, including
    jimbo's own bundled skills, before you load or use that skill. Skills also reload on their own before the
    user's next message.
    """
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "reload_skills", {}, "reload skills", read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    return await reload_and_describe(ctx.deps)


async def _only_when_reloadable(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.reload_skills is not None else None


async def _only_with_skills(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.skills else None


SKILL_TOOLS = [Tool(load_skill, prepare=_only_with_skills)]
RELOAD_TOOLS = [Tool(reload_skills, prepare=_only_when_reloadable)]  # main session only: it changes the session
