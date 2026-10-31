from .agent import build_agent
from .commands import register_builtins
from .config import Settings
from .core.approval import (ApprovalGate, ApprovalPolicy, AskUser, PatternRules, ReadOnlyAllow,
                            RememberedDecisions, Yolo)
from .core.commands import load_markdown_commands
from .core.deps import Deps
from .core.events import EventSink
from .core.interaction import Frontend
from .core.session import AgentSession
from .providers.registry import build_model
from .tools.sandbox import build_sandbox


class FrontendSink:
    def __init__(self, frontend: Frontend) -> None:
        self._f = frontend

    async def emit(self, event) -> None:
        await self._f.render(event)


def build_session(settings: Settings, frontend: Frontend, extra_sinks: list[EventSink] | None = None,
                  *, model=None) -> AgentSession:
    """Compose settings + frontend into a ready AgentSession. `model` overrides the provider (tests)."""
    holder: dict[str, AgentSession] = {}
    remembered = RememberedDecisions()

    async def ask(req):
        await holder["s"].emit("approval_requested", id=req.id, tool=req.tool,
                               summary=req.summary, detail=req.detail)
        return await frontend.request_approval(req)

    available: dict[str, ApprovalPolicy] = {
        "rules": PatternRules(settings.approval_allow, settings.approval_deny),
        "readonly": ReadOnlyAllow(), "remembered": remembered, "yolo": Yolo(),
        "ask": AskUser(ask, remembered),
    }
    policies = [available[n] for n in settings.approval]

    async def observe(call, v):
        await holder["s"].emit("approval_resolved", tool=call.tool, action=v.action,
                               decided_by=v.decided_by, reason=v.reason)

    workspace = settings.workspace.resolve()
    sandbox = build_sandbox(settings.sandbox, workspace, settings.sandbox_writable, settings.sandbox_network)
    deps = Deps(workspace=workspace, gate=ApprovalGate(policies, observe), sandbox=sandbox,
                shell_timeout=settings.shell_timeout, shell_timeout_max=settings.shell_timeout_max)
    agent = build_agent(settings, model or build_model(settings))
    session = AgentSession(agent, deps, [FrontendSink(frontend), *(extra_sinks or [])],
                           request_limit=settings.request_limit,
                           context_window=settings.context_window or settings.num_ctx)
    register_builtins(session.commands, settings, available)
    load_markdown_commands(session.commands, [settings.user_dir / "commands",
                                              deps.workspace / ".jimbo" / "commands"])
    holder["s"] = session
    return session
