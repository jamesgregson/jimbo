"""Browser frontend: a local aiohttp server speaking JSON over one WebSocket.

Server -> client: {"type": "event", "event": Event}, {"type": "approval", "request": ApprovalRequest},
                  {"type": "approval_closed", "id": str}
Client -> server: {"type": "send", "text": str}, {"type": "approve", "id": str, "decision": "yes|no|always"},
                  {"type": "cancel"}
"""

import asyncio
import hmac
import json
import secrets
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.events import Event

STATIC = Path(__file__).parent / "web_static"
MAX_REPLAY = 5000


class WebFrontend:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
        self.host, self.port, self.open_browser = host, port, open_browser
        self.token = secrets.token_urlsafe(16)  # the socket can approve file writes: require it
        self._inputs: asyncio.Queue[str] = asyncio.Queue()
        self._clients: set[web.WebSocketResponse] = set()
        self._history: list[dict] = []
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalDecision]]] = {}
        self._runner: web.AppRunner | None = None
        self._cancel: Callable[[], object] | None = None

    def bind_cancel(self, cancel: Callable[[], object]) -> None:
        """Called by run_frontend so the page's stop button can interrupt the run."""
        self._cancel = cancel

    # ---- Frontend protocol ----
    async def next_user_input(self) -> str | None:
        return await self._inputs.get()

    async def render(self, event: Event) -> None:
        msg = {"type": "event", "event": json.loads(event.model_dump_json())}
        self._history.append(msg)
        del self._history[:-MAX_REPLAY]
        await self._broadcast(msg)

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        fut: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = (req, fut)
        await self._broadcast({"type": "approval", "request": req.model_dump()})
        try:
            return await fut
        finally:
            self._pending.pop(req.id, None)
            await self._broadcast({"type": "approval_closed", "id": req.id})

    # ---- lifecycle ----
    def submit(self, text: str) -> None:
        self._inputs.put_nowait(text)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        print(f"jimbo web UI: {self.url}", flush=True)
        if self.open_browser:
            webbrowser.open(self.url)

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    # ---- handlers ----
    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=(STATIC / "index.html").read_text("utf-8"), content_type="text/html")

    def _authorized(self, request: web.Request) -> bool:
        if not hmac.compare_digest(request.query.get("token", ""), self.token):
            return False
        origin = request.headers.get("Origin")  # browsers always send it on WebSocket upgrades
        return origin is None or urlparse(origin).netloc == request.host

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        for msg in self._history:
            await ws.send_json(msg)
        for req, _ in self._pending.values():
            await ws.send_json({"type": "approval", "request": req.model_dump()})
        self._clients.add(ws)
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    self._on_message(m.data)
        finally:
            self._clients.discard(ws)
        return ws

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            kind = msg["type"]
        except (ValueError, KeyError, TypeError):
            return
        if kind == "send" and isinstance(msg.get("text"), str) and msg["text"].strip():
            self._inputs.put_nowait(msg["text"])
        elif kind == "cancel" and self._cancel:
            self._cancel()
        elif kind == "approve" and msg.get("id") in self._pending:
            _, fut = self._pending[msg["id"]]
            d = msg.get("decision")
            if not fut.done():
                fut.set_result(ApprovalDecision(allow=d in ("yes", "always"),
                                                remember="session" if d == "always" else "no"))

    async def _broadcast(self, msg: dict) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except ConnectionError:
                self._clients.discard(ws)
