import asyncio
import signal
from contextlib import contextmanager

from .attachments import UserMessage, build_content
from .commands import QuitSession
from .interaction import Frontend
from .session import AgentSession


@contextmanager
def _interrupt_cancels_run(session: AgentSession):
    """While a run is in flight, Ctrl+C (SIGINT) cancels it instead of quitting jimbo."""
    loop = asyncio.get_running_loop()
    try:
        prev = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(session.cancel))
    except ValueError:  # not on the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, prev)


async def run_frontend(session: AgentSession, frontend: Frontend) -> None:
    """Pump user input into the session: slash commands go to the registry, the rest to the model."""
    await session.emit("commands", commands=session.commands.describe())
    if bind := getattr(frontend, "bind_cancel", None):  # frontends with their own stop control
        bind(session.cancel)
    while (msg := await frontend.next_user_input()) is not None:
        msg = UserMessage(msg) if isinstance(msg, str) else msg
        text = msg.text
        if not text.strip() and not msg.images:
            continue
        with _interrupt_cancels_run(session):
            try:
                if msg.images or not await session.commands.dispatch(session, text):
                    await session.send(build_content(text, msg.images))
            except QuitSession:
                return
