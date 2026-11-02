"""Built-in slash commands. They live outside core because they rebuild agents and policies."""

from .agent import build_agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from .config import Settings
from .core.agents import RESERVED_PREFIX
from .core.approval import ApprovalPolicy
from .core.commands import NAME_RE, CommandError, CommandRegistry, QuitSession
from .core.session import AgentSession
from .core.skills import Skill, apply_skill_models, format_skill_index
from .providers.credentials import describe_key_source
from .providers.registry import all_providers
from .tools.todos import format_todos

AGENT_TEMPLATE = """\
---
# Tells the main model when to use this agent. Leave it empty if only you should run it.
description:
# model: fast                     # one of your aliases, or main
# tools: [read_file, glob, grep]  # default: all tools
# skills: [skill-name]            # loaded when the agent starts
---
Describe the agent's role here, and what its report should contain.
"""


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
        session.turn_usage = RunUsage()
        session.deps.todos = []
        session.new_session()  # the cleared conversation stays resumable
        await session._emit_todos()
        await session.emit_context()
        return "Conversation cleared."

    async def compact_(session: AgentSession, args: str) -> str:
        if session.is_running:
            raise CommandError("a run is in progress; wait for it or cancel first")
        try:
            result = await session.compact(args)
        except Exception as e:
            raise CommandError(f"compaction failed: {e}") from None
        return result or "Nothing to compact."

    async def model(session: AgentSession, args: str) -> str:
        router = session.deps.models
        if not args:
            return f"model: {router.explain('main').spec}"
        spec = router.explain_value(args)
        try:
            session.agent = build_agent(settings, router.set_main(spec))
        except ValueError as e:
            raise CommandError(str(e)) from None
        return f"model: {spec}"

    async def models(session: AgentSession, args: str) -> str:
        router = session.deps.models
        words = args.split()
        try:
            match words:
                case []:
                    return describe_models(session)
                case ["set", "main", value]:
                    return await model(session, value)
                case ["set", task, value]:
                    r = router.set_override(task, value)
                    return f"{task}: {r.spec}" + (f" [{r.via}]" if r.via else "")
                case ["reset"]:
                    router.clear_override()
                    return "Session model choices cleared."
                case ["reset", task]:
                    router.clear_override(task)
                    return f"{task}: " + router.explain(task).spec
        except ValueError as e:
            raise CommandError(str(e)) from None
        raise CommandError("usage: /models [set <task> <alias|provider:name> | reset [task]]")

    def describe_models(session: AgentSession) -> str:
        router = session.deps.models
        rows = [f"  {r.task:<20}{r.spec}" + (f" [{r.via}]" if r.via else "") + f"  ({r.source})"
                for r in router.describe()]
        rows.append(f"jimbo-shell-assess mode: {settings.task('jimbo-shell-assess').mode}")
        aliases = [f"  {k} = {v}" for k, v in settings.model_aliases.items()]
        provs = [f"  {n:<12}{c.base_url or 'default url'}  key: {describe_key_source(c)}"
                 for n, c in all_providers(settings).items()]
        return "\n".join(["Tasks:", *rows, *(["Aliases:", *aliases] if aliases else []), "Providers:", *provs])

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
            session.deps.gate.policies = [policies["plan"], *(policies[n] for n in chain["names"])]
        elif args:
            raise CommandError("usage: /approve [yolo|default]")
        return "approval: " + " → ".join(chain["names"])

    async def plan(session: AgentSession, args: str) -> str:
        if args not in ("", "on", "off"):
            raise CommandError("usage: /plan [on|off]")
        on = args == "on" or (args == "" and session.deps.mode != "plan")
        session.deps.mode = "plan" if on else "normal"
        return ("plan mode on: read-only tools only; the agent presents a plan with exit_plan" if on
                else "plan mode off")

    async def todos(session: AgentSession, args: str) -> str:
        return format_todos(session.deps.todos)

    async def skills(session: AgentSession, args: str) -> str:
        return format_skill_index(session.deps.skills) or "No skills found."

    async def agents(session: AgentSession, args: str) -> str:
        match args.split():
            case []:
                return "\n".join(f"{a.name}{' (built in)' if a.path is None else ''}"
                                 f"{': ' + a.description if a.description else ''}"
                                 for a in session.deps.agents.values())
            case ["new", name] | ["new", name, "--user"] as words:
                if not NAME_RE.fullmatch(name) or name == "main" or name.startswith(RESERVED_PREFIX):
                    raise CommandError(f"{name!r} can't be an agent name")
                base = settings.user_dir if len(words) == 3 else session.deps.workspace / ".jimbo"
                path = base / "agents" / f"{name}.md"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("x", encoding="utf-8") as f:
                        f.write(AGENT_TEMPLATE)
                except FileExistsError:
                    raise CommandError(f"{path} already exists") from None
                return f"Created {path}. Edit it, then use @{name} <task>."
        raise CommandError("usage: /agents [new <name> [--user]]")

    async def resume(session: AgentSession, args: str) -> str:
        if session.is_running:
            raise CommandError("a run is in progress; wait for it or cancel first")
        if session.store is None:
            raise CommandError("session saving is disabled (log_sessions = false)")
        if not args:
            store = session.store
            rows = [f"{i}  {store.saved_at(i)}  {store.summary(i)}" for i in store.recent()[:15]
                    if i != session.session_id]
            return "Recent sessions here (/resume <id>):\n" + "\n".join(rows) if rows else "No saved sessions here."
        try:
            return await session.resume(args)
        except ValueError as e:
            raise CommandError(str(e)) from None

    async def diff(session: AgentSession, args: str) -> str:
        return session.deps.checkpoints.diff(session.deps.workspace) or "No file changes to show."

    async def undo(session: AgentSession, args: str) -> str:
        if session.is_running:
            raise CommandError("a run is in progress; wait for it or cancel first")
        restored = session.deps.checkpoints.undo()
        if not restored:
            return "Nothing to undo."
        names = ", ".join(p.relative_to(session.deps.workspace).as_posix() for p in restored)
        session.history = [*session.history, ModelRequest(parts=[UserPromptPart(
            f"I undid your latest file changes, so these are back as they were: {names}")]),
            ModelResponse(parts=[TextPart("Understood.")])]  # otherwise the model trusts its memory of them
        session.persist()
        return f"Restored {names}. Shell commands aren't undone."

    async def output(session: AgentSession, args: str) -> str:
        return session.last_output or "No output yet."

    async def quit_(session: AgentSession, args: str) -> None:
        raise QuitSession

    registry.add("help", "list commands", help_)
    registry.add("clear", "forget the conversation so far", clear)
    registry.add("compact", "summarize older history to free context: /compact [focus]", compact_)
    registry.add("model", "show or switch model: /model <alias|provider:name>", model)
    registry.add("models", "which model runs each task: /models [set <task> <alias|spec> | reset [task]]", models)
    registry.add("usage", "token usage and context fill for this session", usage)
    registry.add("approve", "show or set approval chain: /approve [yolo|default]", approve)
    registry.add("plan", "toggle plan mode (read-only until you approve a plan): /plan [on|off]", plan)
    registry.add("todos", "show the agent's task list", todos)
    registry.add("skills", "list available skills", skills)
    registry.add("agents", "list agents, or create one: /agents [new <name> [--user]]", agents)
    registry.add("resume", "list saved sessions, or load one: /resume [id]", resume)
    registry.add("diff", "show the file changes from the latest turn that made any", diff)
    registry.add("undo", "restore the files changed by the latest turn that changed any", undo)
    registry.add("output", "show the latest shell output or agent report in full", output)
    registry.add("quit", "exit jimbo", quit_)


def register_skill_commands(registry: CommandRegistry, skills: dict[str, Skill]) -> None:
    """/<skill> [args] starts a turn that applies the skill. Existing commands are never overridden."""
    for skill in skills.values():
        if skill.name in registry:
            continue

        async def run(session: AgentSession, args: str, skill: Skill = skill) -> None:
            apply_skill_models(session.deps, skill)  # a bad alias just leaves the user's models in place
            prompt = f"Use the skill '{skill.name}'. Its instructions:\n\n{skill.body}"
            await session.send(f"{prompt}\n\n{args}" if args else prompt)

        registry.add(skill.name, skill.description or f"skill from {skill.path}", run)
