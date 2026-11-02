from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied
from ..core.skills import apply_skill_models


async def load_skill(ctx: RunContext[Deps], name: str) -> str:
    """Return the full instructions of the skill `name` (see the skill list in your instructions).

    Load a skill when its description matches the task, then follow it.
    """
    skill = ctx.deps.skills.get(name)
    if skill is None:
        raise ModelRetry(f"unknown skill {name!r}; available: {', '.join(ctx.deps.skills) or 'none'}")
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "load_skill", {"name": name}, f"load skill {name}", read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    note = apply_skill_models(ctx.deps, skill)
    warn = f"\n\n[note: this skill's model choices were not applied: {note}]" if note else ""
    return f"Skill directory: {skill.path.parent}\n\n{skill.body}{warn}"


async def _only_with_skills(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.skills else None


SKILL_TOOLS = [Tool(load_skill, prepare=_only_with_skills)]
