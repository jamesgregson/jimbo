from .interaction import Frontend
from .session import AgentSession


async def run_frontend(session: AgentSession, frontend: Frontend) -> None:
    """Pump user input into the session. Pull-style frontends (e.g. web) can skip this."""
    while (text := await frontend.next_user_input()) is not None:
        if text.strip():
            await session.send(text)
