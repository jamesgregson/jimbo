import argparse
import asyncio
import os
import sys
from pathlib import Path

from .config import Settings
from .core.jsonl import JsonlSink
from .core.models import MAIN
from .core.runner import run_frontend
from .frontends.registry import build_frontend
from .wiring import build_session


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # banner garbles streaming output
    ap = argparse.ArgumentParser(prog="jimbo", description="A simple coding agent")
    ap.add_argument("prompt", nargs="?", help="one-shot task; omit for an interactive session")
    ap.add_argument("--model", help="main model for this run: an alias or <provider>:<name>")
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--ui")
    ap.add_argument("--yolo", action="store_true", help="auto-approve everything")
    ap.add_argument("--resume", nargs="?", const="", metavar="ID",
                    help="continue a saved session (the most recent if no id/prefix is given)")
    args = ap.parse_args()

    overrides = {k: v for k, v in {"workspace": args.workspace, "ui": args.ui}.items() if v}
    sink = None
    try:
        settings = Settings(**overrides)
        if args.model:
            settings.model_aliases[MAIN] = settings.model_aliases.get(args.model, args.model)
        if args.yolo:
            settings.approval = ["rules", "yolo"]  # deny rules still apply
        frontend = build_frontend(settings)
        sink = JsonlSink(settings.log_dir) if settings.log_sessions else None
        session = build_session(settings, frontend, [sink] if sink else None)
    except ValueError as e:  # bad config: no main model, unknown provider, missing key, ...
        if sink:
            sink.close()
        raise SystemExit(f"jimbo: {e}") from None

    async def go() -> None:
        start = getattr(frontend, "start", None)
        if start:
            await start()
        try:
            if args.resume is not None:
                if session.store is None:
                    raise SystemExit("jimbo: --resume needs log_sessions = true")
                try:
                    ids = session.store.ids()
                    print(await session.resume(args.resume or (ids[0] if ids else "")), file=sys.stderr)
                except ValueError as e:
                    raise SystemExit(f"jimbo: {e}") from None
            if args.prompt and hasattr(frontend, "submit"):
                frontend.submit(args.prompt)  # server frontends stay up after the first prompt
                await run_frontend(session, frontend)
            elif args.prompt:
                await session.send(args.prompt)
            else:
                await run_frontend(session, frontend)
        finally:
            if stop := getattr(frontend, "stop", None):
                await stop()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        if sink:
            sink.close()


if __name__ == "__main__":
    main()
