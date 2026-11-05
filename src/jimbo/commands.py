"""Built-in slash commands. They live outside core because they rebuild agents and policies."""

import tempfile
import webbrowser
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from .config import BUNDLED_DIR, Settings
from .config_edit import ENV_RE
from .core.agents import RESERVED_PREFIX
from .core.approval import MODE_WARNING, RISKY_MODES, ApprovalModes
from .core.commands import NAME_RE, CommandError, CommandRegistry, QuitSession
from .core.session import AgentSession
from .core.trace import trace_page
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


def register_builtins(registry: CommandRegistry, settings: Settings, modes: ApprovalModes) -> None:

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
        return f"Conversation cleared. New session {session.session_id}."

    async def compact_(session: AgentSession, args: str) -> str:
        if session.is_running:
            raise CommandError("a run is in progress; wait for it or cancel first")
        try:
            async with session.busy("compacting"):
                result = await session.compact(args)
        except Exception as e:
            raise CommandError(f"compaction failed: {e}") from None
        return result or "Nothing to compact."

    def save_flag(args: str) -> tuple[list[str], bool]:
        words = args.split()
        return [w for w in words if w != "--save"], "--save" in words

    async def model(session: AgentSession, args: str) -> str:
        router = session.deps.models
        words, save = save_flag(args)
        if len(words) > 1:
            raise CommandError("usage: /model [<alias|provider:name>] [--save]")
        if not words and not save:
            return f"model: {router.explain('main').spec}"
        try:
            return await session.config.switch_main(session, words[0] if words else router.explain("main").spec,
                                                    save=save)
        except ValueError as e:
            raise CommandError(str(e)) from None

    async def models(session: AgentSession, args: str) -> str:
        router = session.deps.models
        words, save = save_flag(args)
        try:
            match words:
                case [] if not save:
                    return describe_models(session)
                case ["set", task, value]:
                    return await session.config.set_task(session, task, value, save=save)
                case ["reset"] if not save:
                    router.clear_override()
                    return "Session model choices cleared."
                case ["reset", task]:
                    return await session.config.clear_task(session, task, save=save)
                case ["alias", name, "--rm"] if not save:
                    return await session.config.remove_alias(session, name)
                case ["alias", name, spec] if not save:
                    return await session.config.set_alias(session, name, spec)
        except ValueError as e:
            raise CommandError(str(e)) from None
        raise CommandError("usage: /models [set <task> <alias|provider:name> [--save] | reset [task [--save]] | "
                           "alias <name> <provider:name> | alias <name> --rm]")

    async def ask_key(session: AgentSession, name: str) -> dict:
        """The key for provider `name`, typed at a hidden prompt: a key, $ENV_VAR, or nothing."""
        if session.ask_secret is None:
            raise CommandError(f"this UI can't ask for a key without showing it; use /providers key {name} $ENV_VAR")
        answer = await session.ask_secret(f"API key for {name} (empty for none, $NAME for an environment variable): ")
        if answer is None:
            raise CommandError("cancelled")
        answer = answer.strip()
        if m := ENV_RE.fullmatch(answer):
            return {"key_env": m.group(1)}
        return {"key": answer} if answer else {}

    async def providers(session: AgentSession, args: str) -> str:
        control = session.config
        try:
            match args.split():
                case []:
                    return control.describe_providers()
                case ["add", name, url]:
                    control.check_new_provider(name, url)  # before asking for a key
                    key = await ask_key(session, name) if session.ask_secret else {}
                    return await control.set_provider(session, name, base_url=url, new=True, **key)
                case ["url", name, url]:
                    return await control.set_provider(session, name, base_url=url)
                case ["key", name]:
                    if name not in all_providers(settings):
                        raise ValueError(f"unknown provider {name!r}")
                    key = await ask_key(session, name)
                    if not key:
                        return "No key given; nothing changed."
                    return await control.set_provider(session, name, **key)
                case ["key", name, env] if (m := ENV_RE.fullmatch(env)):
                    return await control.set_provider(session, name, key_env=m.group(1))
                case ["key", name, _]:
                    raise CommandError(f"type /providers key {name} and paste the key at the hidden prompt, so it "
                                       "isn't kept in your input history")
                case ["rm", name]:
                    return await control.remove_provider(session, name)
                case ["test", name]:
                    if name not in all_providers(settings):
                        raise ValueError(f"unknown provider {name!r}")
                    return await control.test_provider(name)
        except ValueError as e:
            raise CommandError(str(e)) from None
        raise CommandError("usage: /providers [add <name> <url> | url <name> <url> | key <name> [$ENV_VAR] | "
                           "rm <name> | test <name>]")

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
        lines = [session.spend.summary()]  # this session id, including before a resume
        if session.context_window:
            pct = 100 * session.context_tokens / session.context_window
            lines.append(f"context: {session.context_tokens} / {session.context_window} tokens ({pct:.0f}%)")
        else:
            lines.append(f"context: {session.context_tokens} tokens (window unknown)")
        return "\n".join(lines)

    async def approve(session: AgentSession, args: str) -> str:
        if args and args not in modes.chains:
            raise CommandError("usage: /approve [auto|yolo|default]")
        if args:
            try:
                modes.check(args)
            except ValueError as e:
                raise CommandError(str(e)) from None
            if args in RISKY_MODES and session.confirm and not await session.confirm(MODE_WARNING):
                return "approval unchanged: " + " → ".join(modes.names)
            modes.set(args)
        return "approval: " + " → ".join(modes.names)

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

    async def bundled(session: AgentSession, args: str) -> str:
        if not settings.bundled:
            return "Bundled files are off (bundled = false)."
        rows = [("agent", p.stem, p) for p in sorted((BUNDLED_DIR / "agents").glob("*.md"))]
        rows += [("command", p.stem, p) for p in sorted((BUNDLED_DIR / "commands").glob("*.md"))]
        rows += [("skill", p.parent.name, p) for p in sorted((BUNDLED_DIR / "skills").glob("*/SKILL.md"))]
        return "\n".join(f"{kind:<9}{name:<14}{path}" for kind, name, path in rows)

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

    async def trace(session: AgentSession, args: str) -> str:
        if session.store is None:
            raise CommandError("session logging is disabled (log_sessions = false)")
        try:
            sid = session.store.resolve(args) if args else session.session_id
            page = trace_page(session.store.log_path(sid))
        except (ValueError, OSError) as e:
            raise CommandError(str(e)) from None
        out = Path(tempfile.gettempdir()) / "jimbo-traces" / f"{sid}.html"
        out.parent.mkdir(exist_ok=True)
        out.write_text(page, "utf-8")
        if webbrowser.open(out.as_uri()):
            return f"Opened the trace of session {sid} in your browser ({out}). Run /trace again to refresh it."
        return f"Wrote the trace of session {sid} to {out}"

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
    registry.add("model", "show or switch model: /model <alias|provider:name> [--save]", model)
    registry.add("models", "which model runs each task: /models [set <task> <alias|spec> [--save] | reset [task] "
                 "| alias <name> <spec> | alias <name> --rm]", models)
    registry.add("providers", "list or change model providers: /providers [add|url|key|rm|test <name> ...]", providers)
    registry.add("usage", "token usage and context fill for this session", usage)
    registry.add("approve", "show or set approval chain: /approve [auto|yolo|default]", approve, midrun=True)
    registry.add("plan", "toggle plan mode (read-only until you approve a plan): /plan [on|off]", plan)
    registry.add("todos", "show the agent's task list", todos)
    registry.add("skills", "list available skills", skills)
    registry.add("agents", "list agents, or create one: /agents [new <name> [--user]]", agents)
    registry.add("bundled", "list the agents, skills and commands shipped with jimbo, with their paths", bundled)
    registry.add("resume", "list saved sessions, or load one: /resume [id]", resume)
    registry.add("trace", "open a session's trace in the browser: /trace [id]", trace)
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
