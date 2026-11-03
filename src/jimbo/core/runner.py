import asyncio
import signal
import time
from collections.abc import Callable
from contextlib import contextmanager

from .approval import MODE_WARNING
from .attachments import UserMessage, build_content
from .commands import QuitSession, agent_call
from .interaction import Frontend
from .session import AgentSession


ESCALATE_WITHIN = 2.0  # seconds: a second Ctrl+C this soon after one that cancelled a run quits jimbo


def make_sigint_handler(session: AgentSession, loop: asyncio.AbstractEventLoop, *,
                        clock: Callable[[], float] = time.monotonic):
    """Ctrl+C cancels the run in flight; with nothing running (or a second press right after a cancel
    that did not take effect) it behaves as usual and raises KeyboardInterrupt, which quits jimbo."""
    last_cancel = -ESCALATE_WITHIN

    def handler(signum, frame):
        nonlocal last_cancel
        now = clock()
        if session.is_running and now - last_cancel > ESCALATE_WITHIN:
            last_cancel = now
            loop.call_soon_threadsafe(session.cancel)
        else:
            signal.default_int_handler(signum, frame)

    return handler


@contextmanager
def _interrupt_cancels_run(session: AgentSession):
    loop = asyncio.get_running_loop()
    try:
        prev = signal.signal(signal.SIGINT, make_sigint_handler(session, loop))
    except ValueError:  # not on the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, prev)


async def _handle(session: AgentSession, msg: str | UserMessage) -> bool:
    """Run one user input to completion, then any steers it left undelivered. False means quit."""
    msg = UserMessage(msg) if isinstance(msg, str) else msg
    if not msg.text.strip() and not msg.images:
        return True
    await session.refresh_agents()
    try:
        if msg.images or not await session.commands.dispatch(session, msg.text):
            await session.send(build_content(msg.text, msg.images))
        while pending := session.take_steers():  # typed too late for the run to see them
            await session.send(_join(pending))
    except QuitSession:
        return False
    return True


def _join(items: list) -> str | list:
    """Several queued messages become one turn: texts joined, images kept."""
    texts = [t for i in items for t in ([i] if isinstance(i, str) else [p for p in i if isinstance(p, str)])]
    images = [p for i in items if not isinstance(i, str) for p in i if not isinstance(p, str)]
    return build_content("\n\n".join(texts), images)


async def run_frontend(session: AgentSession, frontend: Frontend) -> None:
    """Pump user input into the session: slash commands go to the registry, the rest to the model.

    A frontend that sets `supports_steering` keeps being polled for input while a run is in flight; what
    it returns is queued with `session.steer()` and reaches the model with its next request. Others
    (the line-based one, whose approval prompts share stdin) are read only between runs.
    """
    await session.emit("commands", commands=session.commands.describe())
    await session.emit_status(initial=True)
    await session.emit_context()
    if bind := getattr(frontend, "bind_cancel", None):  # frontends with their own stop control
        bind(session.cancel)
    if bind := getattr(frontend, "bind_approval_mode", None):  # ... and their own approval mode control
        bind(lambda mode: set_approval_mode(session, mode))
    if ask := getattr(frontend, "ask_secret", None):
        session.ask_secret = ask
    if session.config is not None:
        await session.config.emit(session)
        if bind := getattr(frontend, "bind_config", None):  # a settings panel
            bind(lambda msg: config_request(session, msg))
        if bind := getattr(frontend, "bind_model_list", None):  # model name completion
            bind(session.config.list_models)
    if confirm := getattr(frontend, "confirm", None):
        session.confirm = confirm
        modes = session.approval_modes
        if modes is not None and modes.risky and not await confirm(MODE_WARNING):  # started with --auto/--yolo
            modes.set("default")
            await session.emit("command_output", command="approve", ok=True,
                               text="approval: " + " → ".join(modes.names))
            await session.emit_status()
    steering = getattr(frontend, "supports_steering", False)
    with _interrupt_cancels_run(session):
        run: asyncio.Task | None = None
        read: asyncio.Task | None = None
        try:
            while True:
                if read is None:
                    read = asyncio.ensure_future(frontend.next_user_input())
                waiting = {read, *([run] if run else [])}
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if run in done:
                    alive = run.result()
                    run = None
                    if not alive:
                        return
                if read in done:
                    msg, read = read.result(), None
                    if msg is None:
                        return
                    if run is not None:  # only reachable with steering: a run is in flight
                        await _steer_or_refuse(session, msg)
                    elif steering:
                        run = asyncio.ensure_future(_handle(session, msg))
                    elif not await _handle(session, msg):
                        return
        finally:
            for t in (run, read):
                if t is not None and not t.done():
                    t.cancel()
            if run is not None:
                await asyncio.gather(run, return_exceptions=True)


async def set_approval_mode(session: AgentSession, mode: str) -> None:
    """Switch the approval mode from a frontend control. Unlike /approve this works mid-run, so it can be
    used from an approval prompt: the call being asked about keeps its chain, later calls use the new one."""
    try:
        if session.approval_modes is None:
            raise ValueError("this session has no approval modes")
        session.approval_modes.set(mode)
    except ValueError as e:
        await session.emit("command_output", command="approve", ok=False, text=str(e))
    else:
        await session.emit_status()


async def config_request(session: AgentSession, msg: dict) -> None:
    """A request from a frontend's settings panel (see config_edit.ConfigControl.apply). Changes are refused
    during a run, like slash commands."""
    try:
        if msg.get("op") != "get" and session.is_running:
            raise ValueError("a run is in progress; wait for it or cancel first")
        text = await session.config.apply(session, msg)
    except ValueError as e:
        await session.emit("command_output", command="config", ok=False, text=str(e))
        return
    if text:
        await session.emit("command_output", command="config", ok=True, text=text)
    await session.emit_status()


async def _steer_or_refuse(session: AgentSession, msg: str | UserMessage) -> None:
    msg = UserMessage(msg) if isinstance(msg, str) else msg
    if not msg.text.strip() and not msg.images:
        return
    if (msg.text.lstrip().startswith("/") or agent_call(session, msg.text)) and not msg.images:
        await session.emit("command_output", command="steer", ok=False,  # they change session state mid-run
                           text="commands can't run while the agent is working; Esc stops it")
    else:
        session.steer(build_content(msg.text, msg.images))
