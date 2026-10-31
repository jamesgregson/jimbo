"""Built-in slash commands. They live outside core because they rebuild agents and policies."""

from .agent import build_agent
from .config import Settings
from .core.approval import ApprovalPolicy
from .core.commands import CommandError, CommandRegistry, QuitSession
from .core.session import AgentSession
from .providers.registry import build_model


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

    async def quit_(session: AgentSession, args: str) -> None:
        raise QuitSession

    registry.add("help", "list commands", help_)
    registry.add("clear", "forget the conversation so far", clear)
    registry.add("model", "show or switch model: /model <provider>:<name>", model)
    registry.add("usage", "token usage and context fill for this session", usage)
    registry.add("approve", "show or set approval chain: /approve [yolo|default]", approve)
    registry.add("quit", "exit jimbo", quit_)
