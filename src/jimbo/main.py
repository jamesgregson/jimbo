import argparse
import asyncio
import os
import sys
from pathlib import Path

from .config import Settings
from .core.approval import AUTO_CHAIN, YOLO_CHAIN
from .core.commands import QuitSession
from .core.jsonl import JsonlSink
from .core.models import MAIN
from .core.runner import run_frontend
from .frontends.registry import build_frontend, pick_ui
from .providers.preflight import inspect_model
from .tools.shell import kill_background
from .wiring import build_session


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # banner garbles streaming output
    ap = argparse.ArgumentParser(prog="jimbo", description="A simple coding agent")
    ap.add_argument("prompt", nargs="?", help="one-shot task; omit for an interactive session")
    ap.add_argument("--model", help="main model for this run: an alias or <provider>:<name>")
    ap.add_argument("--workspace", type=Path, help="directory to work on (default: the current one)")
    ap.add_argument("--ui", help="tui, plain, web, or json for scripts (default: tui in a terminal, else plain)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--yolo", action="store_true", help="auto-approve everything")
    mode.add_argument("--auto", action="store_true",
                      help="auto-approve file edits, and shell commands the assessment model rates medium risk or lower")
    ap.add_argument("--resume", nargs="?", const="", metavar="ID",
                    help="continue a saved session (the most recent if no id/prefix is given)")
    args = ap.parse_args()
    if args.resume and not args.prompt and len(args.resume.split()) > 1:  # jimbo --resume "fix the tests"
        args.prompt, args.resume = args.resume, ""

    overrides = {k: v for k, v in {"workspace": args.workspace, "ui": args.ui}.items() if v}
    sink = None
    try:
        settings = Settings(**overrides)
        if args.model:
            settings.model_aliases[MAIN] = settings.model_aliases.get(args.model, args.model)
        if args.yolo:
            settings.approval = list(YOLO_CHAIN)
        if args.auto:
            if settings.task("jimbo-shell-assess").mode == "off":
                raise ValueError("--auto needs the shell assessment model; [tasks.jimbo-shell-assess] mode is off")
            settings.approval = list(AUTO_CHAIN)
        if settings.ui == "auto":
            settings.ui = pick_ui(args.prompt, sys.stdin.isatty(), sys.stdout.isatty())
        if settings.ui in ("plain", "json") and not sys.stdin.isatty():  # cat log | jimbo "explain this"
            args.prompt = "\n\n".join(filter(None, [args.prompt, sys.stdin.read().strip()])) or None
        frontend = build_frontend(settings)
        sink = JsonlSink(settings.log_dir) if settings.log_sessions else None
        session = build_session(settings, frontend, [sink] if sink else None)
    except ValueError as e:  # bad config: no main model, unknown provider, missing key, ...
        if sink:
            sink.close()
        raise SystemExit(f"jimbo: {e}") from None

    async def preflight() -> None:
        facts = await inspect_model(settings.model_aliases[MAIN], settings)
        if facts.problem:
            raise SystemExit(f"jimbo: {facts.problem}")
        for note in facts.notes:
            print(f"jimbo: {note}", file=sys.stderr)
        if facts.context and settings.context_window is None:
            session.context_window = facts.context
        if facts.vision is False:
            session.vision = False
        session.prices = facts.prices

    async def one_shot(prompt: str) -> None:
        await session.emit_status(initial=True)
        try:
            if not await session.commands.dispatch(session, prompt):
                await session.send(prompt)
        except QuitSession:
            pass
        if session.failed:
            raise SystemExit(1)

    async def go() -> None:
        await preflight()
        start = getattr(frontend, "start", None)
        if start:
            await start()
        try:
            if args.resume is not None:
                if session.store is None:
                    raise SystemExit("jimbo: --resume needs log_sessions = true")
                try:
                    recent = session.store.recent()
                    if not args.resume and not recent:
                        raise ValueError(f"no saved sessions for {session.deps.workspace}")
                    print(await session.resume(args.resume or recent[0]), file=sys.stderr)
                except ValueError as e:
                    raise SystemExit(f"jimbo: {e}") from None
            if args.prompt and hasattr(frontend, "submit"):
                frontend.submit(args.prompt)  # server frontends stay up after the first prompt
                await run_frontend(session, frontend)
            elif args.prompt:
                await one_shot(args.prompt)
            else:
                await run_frontend(session, frontend)
        finally:
            spent = session.run_spend
            if spent.requests:
                await session.close()
            await kill_background(session.deps)
            if session.deps.lsp:
                await session.deps.lsp.close()
            if stop := getattr(frontend, "stop", None):
                await stop()
            if spent.requests and settings.ui != "json":  # json: the session_end event says it
                print(f"jimbo: this run: {spent.summary()}", file=sys.stderr)
                if session.spend.requests > spent.requests:  # resumed: the session cost more before this run
                    print(f"jimbo: session {session.session_id}: {session.spend.summary()}", file=sys.stderr)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        if args.prompt:
            raise SystemExit(130) from None
    finally:
        if sink:
            sink.close()


if __name__ == "__main__":
    main()
