import secrets
import sys
from dataclasses import replace
from itertools import count
from pathlib import Path

from pydantic_ai.usage import RunUsage

from .agent import AGENT_TOOL_NAMES, build_agent
from .commands import register_builtins, register_skill_commands
from .config import BUNDLED_DIR, Settings
from .config_edit import ConfigControl
from .core.agents import AgentResult, AgentSpec, ForwardSink, load_agents
from .core.assess import TASK as ASSESS_TASK, assess_shell
from .core.approval import (ApprovalGate, ApprovalModes, ApprovalPolicy, AskUser, AutoApprove, PatternRules,
                            PlanMode, ReadOnlyAllow, RememberedDecisions, Yolo)
from .core.commands import load_markdown_commands
from .core.deps import Deps
from .core.events import EventSink
from .core.interaction import Frontend
from .core.models import ModelRouter
from .core.store import SessionStore
from .core.skills import load_project_instructions, load_skills
from .core.session import AgentSession
from .lsp.manager import LspManager
from .lsp.servers import resolve_servers
from .providers.preflight import ModelFacts, inspect_model
from .providers.registry import build_model_from_spec
from .tools.sandbox import build_sandbox


ASSESS_WARN_AFTER = 3  # failed shell assessments in a row before the user is told


class FrontendSink:
    def __init__(self, frontend: Frontend) -> None:
        self._f = frontend

    async def emit(self, event) -> None:
        await self._f.render(event)


def _files(dirs: list[Path], pattern: str) -> dict[Path, int]:
    """Each file matching `pattern` in `dirs`, with its modification time: compared to see if any changed."""
    return {p: p.stat().st_mtime_ns for d in dirs if d.is_dir() for p in d.glob(pattern)}


def build_session(settings: Settings, frontend: Frontend, extra_sinks: list[EventSink] | None = None,
                  *, model=None, model_builder=None, model_inspector=None) -> AgentSession:
    """Compose settings + frontend into a ready AgentSession. `model` overrides the main model,
    `model_builder(spec, settings)` the way other models are built, and `model_inspector(spec, settings)` the
    check of a model an agent will use; there is no check with a fake model or builder (tests)."""
    holder: dict[str, AgentSession] = {}
    remembered = RememberedDecisions()
    router = ModelRouter(settings, model_builder or build_model_from_spec, main_model=model)

    async def ask(req):
        await holder["s"].emit("approval_requested", id=req.id, call_id=req.call_id, tool=req.tool,
                               summary=req.summary, detail=req.detail,
                               **({"assessment": req.assessment} if req.assessment else {}))
        return await frontend.request_approval(req)

    assess_mode = settings.task(ASSESS_TASK).mode
    failures = {"streak": 0}  # assessments in a row the model didn't answer

    async def assess(call) -> None:
        """Attach a model-written summary and risk to a shell call; never blocks the call on failure."""
        if call.tool != "shell" or "command" not in call.args:
            return
        deps = holder["s"].deps
        where = deps.sandbox.describe() if deps.sandbox else "NOT sandboxed"
        a = await assess_shell(router, str(call.args["command"]), deps.workspace,
                               timeout=settings.task(ASSESS_TASK).timeout, where=f"Execution: {where}")
        call.assessment = a.model_dump() if a else None
        if a is not None and a.model:
            failures["streak"] = 0
        elif a is not None and a.error:
            failures["streak"] += 1
            if failures["streak"] == ASSESS_WARN_AFTER:  # once per streak
                await holder["s"].emit("command_output", command=ASSESS_TASK, ok=True, text=(
                    f"warning: {ASSESS_TASK} failed {ASSESS_WARN_AFTER} times in a row (last: {a.error}). Shell "
                    "prompts show only the fixed checks, and auto mode asks before every shell command. Check "
                    f"the model with /models, or raise timeout under [tasks.{ASSESS_TASK}]."))

    async def assess_asked(call) -> None:
        if assess_mode != "off":
            await assess(call)

    available: dict[str, ApprovalPolicy] = {
        "plan": PlanMode(lambda: holder["s"].deps.mode == "plan", ask),
        "rules": PatternRules(settings.approval_allow,
                              [*settings.approval_deny, *(f"read_file:{p}" for p in settings.secret_files)]),
        "readonly": ReadOnlyAllow(), "remembered": remembered, "yolo": Yolo(),
        "auto": AutoApprove(assess if assess_mode != "off" else None),
        "ask": AskUser(ask, remembered, assess_asked),
    }
    if "auto" in settings.approval and assess_mode == "off":
        print("jimbo: auto approval is off because jimbo-shell-assess mode is off", file=sys.stderr)
    policies = [available["plan"], *(available[n] for n in settings.approval)]

    async def observe(call, v):
        if v.action == "allow" and call.assessment is None and assess_mode == "always":
            await assess(call)  # also describe calls a policy allowed without asking
        unseen = v.action == "allow" and v.decided_by != "user" and call.tool in ("write_file", "edit_file")
        await holder["s"].emit("approval_resolved", call_id=call.call_id, tool=call.tool, action=v.action,
                               decided_by=v.decided_by, reason=v.reason,
                               **({"assessment": call.assessment} if call.assessment else {}),
                               **({"detail": call.detail} if unseen else {}))  # no prompt showed this diff

    workspace = settings.workspace.resolve()
    sandbox = build_sandbox(settings.sandbox, workspace, settings.sandbox_writable, settings.sandbox_network)
    gate = ApprovalGate(policies, observe)
    # Started with --yolo or --auto: "default" means the normal chain, not that one.
    default_chain = (settings.approval if not {"yolo", "auto"} & set(settings.approval)
                     else list(Settings.model_fields["approval"].default))
    modes = ApprovalModes(gate, available, settings.approval, default_chain, auto_available=assess_mode != "off")
    deps = Deps(workspace=workspace, gate=gate, sandbox=sandbox,
                shell_timeout=settings.shell_timeout, shell_timeout_max=settings.shell_timeout_max,
                models=router, secret_files=settings.secret_files,
                ask_user=getattr(frontend, "ask_questions", None))
    if settings.lsp:
        choices, problems = resolve_servers(settings)
        for warning in problems:
            print(f"jimbo: {warning}", file=sys.stderr)
        log_dir = settings.log_dir if settings.log_sessions else None
        deps.lsp = LspManager(workspace, choices, timeout=settings.lsp_timeout, wait=settings.lsp_wait,
                              diagnostics=settings.lsp_diagnostics, sandbox=sandbox,
                              log_path=lambda name: log_dir and log_dir / f"{holder['s'].session_id}.lsp-{name}.log")
    # Lowest to highest precedence. Also reads the cross-tool .agents/.claude skill folders; the user-level
    # ones are derived from user_dir's parent (the home dir by default).
    home = settings.user_dir.parent
    bundled = [BUNDLED_DIR] if settings.bundled else []
    skill_dirs = [*(d / "skills" for d in bundled), home / ".claude" / "skills", home / ".agents" / "skills",
                  settings.user_dir / "skills", workspace / ".claude" / "skills", workspace / ".agents" / "skills",
                  workspace / ".jimbo" / "skills"]
    deps.user_dir = settings.user_dir
    deps.project_instructions = load_project_instructions(settings.user_dir, workspace)
    deps.skills = load_skills(skill_dirs)
    loaded_skills = {"files": _files(skill_dirs, "*/SKILL.md")}
    agent_dirs = [*(d / "agents" for d in bundled), settings.user_dir / "agents", workspace / ".jimbo" / "agents"]
    loaded: dict[str, list] = {}

    def load_all_agents() -> list[str]:
        loaded["files"] = _files(agent_dirs, "*.md")
        deps.agents, problems = load_agents(agent_dirs, AGENT_TOOL_NAMES)
        return problems + [f"agent {a.name}: unknown skill {s!r}" for a in deps.agents.values() for s in a.skills
                           if s not in deps.skills]

    def reload_agents(force: bool = False) -> list[str] | None:
        return load_all_agents() if force or _files(agent_dirs, "*.md") != loaded["files"] else None

    for warning in load_all_agents():
        print(f"jimbo: {warning}", file=sys.stderr)
    deps.reload_agents = reload_agents
    inspector = model_inspector or (None if model or model_builder else inspect_model)
    facts: dict[str, ModelFacts] = {}

    async def agent_model_facts(spec: str) -> ModelFacts:
        """What the provider says about a model an agent is about to use. Inconclusive answers are fine."""
        if spec in facts or inspector is None:
            return facts.get(spec) or ModelFacts()
        try:
            found = await inspector(spec, settings)
        except ValueError as e:
            found = ModelFacts(str(e))
        if not found.problem:  # a server that's down now may be up for the next run
            facts[spec] = found
            for note in found.notes:
                await holder["s"].emit("command_output", command="agent", ok=True, text=note)
        return found

    running: set[str] = set()  # names of the agent runs in progress

    async def spawn_agent(spec: AgentSpec, task: str, usage: RunUsage, *, model: str | None = None,
                          reserve: int = 0, share: int = 1) -> AgentResult:
        parent = holder["s"]
        limit = (settings.request_limit - usage.requests - reserve) // max(share, 1)
        if limit < 1:
            return AgentResult(False, f"agent {spec.name} didn't start: this turn has too few requests left")
        choice, warning = router.for_agent(spec.name, model or spec.model)
        if warning:
            await parent.emit("command_output", command="agent", ok=True, text=warning)
        try:
            built = await router.abuild(choice.spec)
            on_main = choice.spec == router.explain("main").spec
            found = ModelFacts() if on_main else await agent_model_facts(choice.spec)
        except ValueError as e:
            found, built = ModelFacts(str(e)), None
        if found.problem:
            return AgentResult(False, f"agent {spec.name} didn't start: {found.problem}")
        if spec.name in running:  # the same agent twice at once: a name of its own tells their calls apart
            spec = replace(spec, name=next(f"{spec.name}-{k}" for k in count(2) if f"{spec.name}-{k}" not in running))
        running.add(spec.name)
        sink = ForwardSink(parent, spec.name, choice.spec)
        child = AgentSession(build_agent(settings, built, spec),
                             replace(parent.deps, agent=spec, agents={}, models=router.child(), todos=[]), [sink],
                             request_limit=limit, session_id=f"{parent.session_id}.{spec.name}",
                             context_window=(parent.context_window if on_main else found.context
                                             or settings.context_window or settings.num_ctx),
                             vision=settings.vision, compact_threshold=settings.compact_threshold,
                             compact_keep=settings.compact_keep, nested=True)
        try:
            report = await child.send(task)
        finally:
            usage.incr(child.turn_usage)
            running.discard(spec.name)
        if report is None:
            why = "stopped" if sink.cancelled else sink.reason or "it failed"
            text = f"agent {spec.name} didn't finish: {why}" + (f"\n\nIts last text:\n{sink.text}" if sink.text else "")
            result = AgentResult(False, text, cancelled=sink.cancelled)
        else:
            parent.last_output = report
            result = AgentResult(True, report)
        # in full: the run_agent tool result the model gets may be cut
        await parent.emit("agent_report", ok=result.ok, text=result.text, agent={"name": spec.name, "model": choice.spec},
                          **({"cancelled": True} if result.cancelled else {}))
        return result

    deps.spawn_agent = spawn_agent

    async def show_image(file: Path, path: str, media_type: str, caption: str) -> None:
        # the event names the file, not its bytes; the frontend reads it when it renders the event
        await holder["s"].emit("image", id=secrets.token_hex(8), path=path, file=str(file), media_type=media_type,
                               bytes=file.stat().st_size, caption=caption)

    if getattr(frontend, "shows_images", False):
        deps.show_image = show_image
    agent = build_agent(settings, router.resolve("main"))
    session = AgentSession(agent, deps, [FrontendSink(frontend), *(extra_sinks or [])],
                           request_limit=settings.request_limit,
                           context_window=settings.context_window or settings.num_ctx,
                           vision=settings.vision, compact_threshold=settings.compact_threshold,
                           compact_keep=settings.compact_keep)
    if settings.log_sessions:
        session.store = SessionStore(settings.log_dir, workspace)
    session.replay_sink = FrontendSink(frontend)
    session.approval_modes = modes
    session.config = ConfigControl(settings, inspector)
    register_builtins(session.commands, settings, modes)
    load_markdown_commands(session.commands, [settings.user_dir / "commands",
                                              deps.workspace / ".jimbo" / "commands"])
    late: set[str] = set()  # skill and bundled commands: registered last, and again when skills change

    def register_late() -> None:
        for name in late:
            session.commands.remove(name)
        before = session.commands.names()
        register_skill_commands(session.commands, deps.skills)
        # Last, because this skips names already taken: a user's skill replaces a bundled command of the same name.
        load_markdown_commands(session.commands, [d / "commands" for d in bundled])
        late.clear()
        late.update(session.commands.names() - before)

    async def reload_skills(force: bool = False) -> None:
        if (files := _files(skill_dirs, "*/SKILL.md")) == loaded_skills["files"] and not force:
            return
        loaded_skills["files"] = files
        deps.skills = load_skills(skill_dirs)
        register_late()
        await session.emit("commands", commands=session.commands.describe())  # for completion

    register_late()
    deps.reload_skills = reload_skills
    holder["s"] = session
    return session
