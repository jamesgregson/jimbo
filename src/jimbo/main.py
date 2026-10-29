import argparse
import asyncio
from pathlib import Path

from .config import Settings
from .core.runner import run_frontend
from .frontends.registry import build_frontend
from .wiring import build_session


def main() -> None:
    ap = argparse.ArgumentParser(prog="jimbo", description="A simple coding agent")
    ap.add_argument("prompt", nargs="?", help="one-shot task; omit for an interactive session")
    ap.add_argument("--model", help="<provider>:<name>, e.g. ollama:coder-qwen3.8-27b:latest")
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--ui")
    ap.add_argument("--yolo", action="store_true", help="auto-approve everything")
    args = ap.parse_args()

    overrides = {k: v for k, v in {"model": args.model, "workspace": args.workspace, "ui": args.ui}.items() if v}
    settings = Settings(**overrides)
    if args.yolo:
        settings.approval = ["yolo"]
    frontend = build_frontend(settings.ui)
    session = build_session(settings, frontend)

    async def go() -> None:
        if args.prompt:
            await session.send(args.prompt)
        else:
            await run_frontend(session, frontend)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
