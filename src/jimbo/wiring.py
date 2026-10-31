from .agent import build_agent
from .commands import register_builtins, register_skill_commands
from .config import Settings
from .core.assess import TASK as ASSESS_TASK, assess_shell
from .core.approval import (ApprovalGate, ApprovalPolicy, AskUser, PatternRules, PlanMode, ReadOnlyAllow,
                            RememberedDecisions, Yolo)
from .core.commands import load_markdown_commands
from .core.deps import Deps
from .core.events import EventSink
from .core.interaction import Frontend
from .core.models import ModelRouter
from .core.store import SessionStore
from .core.skills import load_project_instructions, load_skills
from .core.session import AgentSession
from .providers.registry import build_model_from_spec
from .tools.sandbox import build_sandbox


class FrontendSink:
    def __init__(self, frontend: Frontend) -> None:
        self._f = frontend

    async def emit(self, event) -> None:
        await self._f.render(event)


def build_session(settings: Settings, frontend: Frontend, extra_sinks: list[EventSink] | None = None,
                  *, model=None, model_builder=None) -> AgentSession:
    """Compose settings + frontend into a ready AgentSession. `model` overrides the main model and
    `model_builder(spec, settings)` the way other models are built (tests)."""
    holder: dict[str, AgentSession] = {}
    remembered = RememberedDecisions()
    router = ModelRouter(settings, model_builder or build_model_from_spec, main_model=model)

    async def ask(req):
        await holder["s"].emit("approval_requested", id=req.id, tool=req.tool,
                               summary=req.summary, detail=req.detail,
                               **({"assessment": req.assessment} if req.assessment else {}))
        return await frontend.request_approval(req)

    def assess_mode() -> str:
        mode = settings.task(ASSESS_TASK).mode
        return ("ask" if router.is_configured(ASSESS_TASK) else "off") if mode == "auto" else mode

    async def assess(call) -> None:
        """Attach a model-written summary and risk to a shell call; never blocks the call on failure."""
        if call.tool != "shell" or "command" not in call.args:
            return
        deps = holder["s"].deps
        where = deps.sandbox.describe() if deps.sandbox else "NOT sandboxed"
        a = await assess_shell(router, str(call.args["command"]), deps.workspace,
                               timeout=settings.task(ASSESS_TASK).timeout, where=f"Execution: {where}")
        call.assessment = a.model_dump() if a else None

    async def assess_asked(call) -> None:
        if assess_mode() in ("ask", "always"):
            await assess(call)

    available: dict[str, ApprovalPolicy] = {
        "plan": PlanMode(lambda: holder["s"].deps.mode == "plan", ask),
        "rules": PatternRules(settings.approval_allow, settings.approval_deny),
        "readonly": ReadOnlyAllow(), "remembered": remembered, "yolo": Yolo(),
        "ask": AskUser(ask, remembered, assess_asked),
    }
    policies = [available["plan"], *(available[n] for n in settings.approval)]

    async def observe(call, v):
        if v.action == "allow" and call.assessment is None and assess_mode() == "always":
            await assess(call)  # also describe calls a policy allowed without asking
        await holder["s"].emit("approval_resolved", tool=call.tool, action=v.action,
                               decided_by=v.decided_by, reason=v.reason,
                               **({"assessment": call.assessment} if call.assessment else {}))

    workspace = settings.workspace.resolve()
    sandbox = build_sandbox(settings.sandbox, workspace, settings.sandbox_writable, settings.sandbox_network)
    deps = Deps(workspace=workspace, gate=ApprovalGate(policies, observe), sandbox=sandbox,
                shell_timeout=settings.shell_timeout, shell_timeout_max=settings.shell_timeout_max,
                models=router)
    # Lowest to highest precedence. Also reads the cross-tool .agents/.claude skill folders; the user-level
    # ones are derived from user_dir's parent (the home dir by default).
    home = settings.user_dir.parent
    skill_dirs = [home / ".claude" / "skills", home / ".agents" / "skills", settings.user_dir / "skills",
                  workspace / ".claude" / "skills", workspace / ".agents" / "skills",
                  workspace / ".jimbo" / "skills"]
    deps.project_instructions = load_project_instructions(settings.user_dir, workspace)
    deps.skills = load_skills(skill_dirs)
    agent = build_agent(settings, router.resolve("main"))
    session = AgentSession(agent, deps, [FrontendSink(frontend), *(extra_sinks or [])],
                           request_limit=settings.request_limit,
                           context_window=settings.context_window or settings.num_ctx,
                           vision=settings.vision, compact_threshold=settings.compact_threshold,
                           compact_keep=settings.compact_keep)
    if settings.log_sessions:
        session.store = SessionStore(settings.log_dir)
    session.replay_sink = FrontendSink(frontend)
    register_builtins(session.commands, settings, available)
    load_markdown_commands(session.commands, [settings.user_dir / "commands",
                                              deps.workspace / ".jimbo" / "commands"])
    register_skill_commands(session.commands, deps.skills)
    holder["s"] = session
    return session
