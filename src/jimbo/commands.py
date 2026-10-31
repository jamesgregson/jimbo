"""Built-in slash commands. They live outside core because they rebuild agents and policies."""

from .agent import build_agent
from .config import Settings
from .core.approval import ApprovalPolicy
from .core.commands import CommandError, CommandRegistry, QuitSession
from .core.session import AgentSession
from .core.skills import Skill, format_skill_index
from .providers.registry import build_model
from .tools.todos import format_todos


def register_builtins(registry: CommandRegistry, settings: Settings,
                      policies: dict[str, ApprovalPolicy]) -> None:
    current = list(settings.approval)
    # Started with --yolo: "default" means the normal chain, not the yolo one.
    default_chain = current if "yolo" not in current else list(Settings.model_fields["approval"].default)
    chain = {"names": current}

    async def help_(session: AgentSession, args: str) -> str:
        return "\n".join(f"/{c['name']} — {c['help']}" for c in registry.describe())

    async def clear(session: AgentSession, args: str) -> str:
        session.history = []
        session.context_tokens = 0
        session.deps.todos = []
        await session._emit_todos()
        return "Conversation cleared."

    async def model(session: AgentSession, args: str) -> str:
        if not args:
            return f"model: {settings.model}"
        new = settings.model_copy(update={"model": args})
        try:
            session.agent = build_agent(new, build_model(new))
        except ValueError as e:
            raise CommandError(str(e)) from None
        settings.model = args
        return f"model: {args}"

    async def usage(session: AgentSession, args: str) -> str:
        u = session.total_usage
        lines = [f"{u.requests} requests · {u.input_tokens} in · {u.output_tokens} out"]
        if session.context_window:
            pct = 100 * session.context_tokens / session.context_window
            lines.append(f"context: {session.context_tokens} / {session.context_window} tokens ({pct:.0f}%)")
        else:
            lines.append(f"context: {session.context_tokens} tokens (window unknown)")
        return "\n".join(lines)

    async def approve(session: AgentSession, args: str) -> str:
        if args in ("yolo", "default"):
            chain["names"] = ["rules", "yolo"] if args == "yolo" else default_chain
            session.deps.gate.policies = [policies[n] for n in chain["names"]]
        elif args:
            raise CommandError("usage: /approve [yolo|default]")
        return "approval: " + " → ".join(chain["names"])

    async def todos(session: AgentSession, args: str) -> str:
        return format_todos(session.deps.todos)

    async def skills(session: AgentSession, args: str) -> str:
        return format_skill_index(session.deps.skills) or "No skills found."

    async def quit_(session: AgentSession, args: str) -> None:
        raise QuitSession

    registry.add("help", "list commands", help_)
    registry.add("clear", "forget the conversation so far", clear)
    registry.add("model", "show or switch model: /model <provider>:<name>", model)
    registry.add("usage", "token usage and context fill for this session", usage)
    registry.add("approve", "show or set approval chain: /approve [yolo|default]", approve)
    registry.add("todos", "show the agent's task list", todos)
    registry.add("skills", "list available skills", skills)
    registry.add("quit", "exit jimbo", quit_)


def register_skill_commands(registry: CommandRegistry, skills: dict[str, Skill]) -> None:
    """/<skill> [args] starts a turn that applies the skill. Existing commands are never overridden."""
    for skill in skills.values():
        if skill.name in registry:
            continue

        async def run(session: AgentSession, args: str, skill: Skill = skill) -> None:
            prompt = f"Use the skill '{skill.name}'. Its instructions:\n\n{skill.body}"
            await session.send(f"{prompt}\n\n{args}" if args else prompt)

        registry.add(skill.name, skill.description or f"skill from {skill.path}", run)
