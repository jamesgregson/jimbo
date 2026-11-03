"""Browser frontend: a local aiohttp server speaking JSON over one WebSocket.

Server -> client: {"type": "event", "event": Event}, {"type": "approval", "request": ApprovalRequest},
                  {"type": "approval_closed", "id": str}, {"type": "files", "prefix": str, "files": [str]}
Client -> server: {"type": "send", "text": str, "images"?: [{"media_type": str, "data": base64}]},
                  {"type": "approve", "id": str, "decision": "yes|no|always", "note"?: str  (why, on "no")},
                  {"type": "cancel"}, {"type": "complete", "prefix": str}  (workspace files for @path),
                  {"type": "approval_mode", "mode": "default|auto|yolo"}  (allowed mid-run and during a prompt)
"""

import asyncio
import base64
import binascii
import errno
import hmac
import json
import secrets
import webbrowser
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from aiohttp import WSMsgType, web
from pydantic_ai.messages import BinaryContent

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.attachments import MAX_IMAGE_BYTES, MAX_IMAGES, UserMessage, check_image
from ..core.events import Event
from ..tools.search import list_files

STATIC = Path(__file__).parent / "web_static"
MAX_REPLAY = 5000
PORT_ATTEMPTS = 20  # how many ports from the configured one to try
MAX_WS_MESSAGE = MAX_IMAGES * (MAX_IMAGE_BYTES * 4 // 3 + 1024) + 1_000_000  # base64 images plus text


class WebFrontend:
    supports_steering = True  # the page keeps sending while a run is in flight

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
                 workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self.host, self.port, self.open_browser = host, port, open_browser
        self.token = secrets.token_urlsafe(16)  # the socket can approve file writes: require it
        self._inputs: asyncio.Queue[str | UserMessage] = asyncio.Queue()
        self._clients: set[web.WebSocketResponse] = set()
        self._history: list[dict] = []
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalDecision]]] = {}
        self._runner: web.AppRunner | None = None
        self._cancel: Callable[[], object] | None = None
        self._set_mode: Callable[[str], Awaitable[None]] | None = None

    def bind_cancel(self, cancel: Callable[[], object]) -> None:
        """Called by run_frontend so the page's stop button can interrupt the run."""
        self._cancel = cancel

    def bind_approval_mode(self, set_mode: Callable[[str], Awaitable[None]]) -> None:
        """Called by run_frontend so the page's approval mode control can switch modes."""
        self._set_mode = set_mode

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
        await self._bind()
        print(f"jimbo web UI: {self.url}", flush=True)
        if self.open_browser:
            webbrowser.open(self.url)

    async def _bind(self) -> None:
        """Serve on the configured port, or the next free one if another session has it."""
        for port in range(self.port, self.port + PORT_ATTEMPTS):
            try:
                await web.TCPSite(self._runner, self.host, port).start()
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
            else:
                self.port = port
                return
        raise SystemExit(f"jimbo: no free port in {self.port}-{self.port + PORT_ATTEMPTS - 1}")

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
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)
        for msg in self._history:
            await ws.send_json(msg)
        for req, _ in self._pending.values():
            await ws.send_json({"type": "approval", "request": req.model_dump()})
        self._clients.add(ws)
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    self._on_message(m.data, ws)
        finally:
            self._clients.discard(ws)
        return ws

    def _on_message(self, raw: str, ws: web.WebSocketResponse | None = None) -> None:
        try:
            msg = json.loads(raw)
            kind = msg["type"]
        except (ValueError, KeyError, TypeError):
            return
        if kind == "send" and isinstance(msg.get("text"), str):
            images = self._decode_images(msg.get("images"))
            if msg["text"].strip() or images:
                self._inputs.put_nowait(UserMessage(msg["text"], images) if images else msg["text"])
        elif kind == "complete" and isinstance(msg.get("prefix"), str) and ws is not None:
            asyncio.get_running_loop().create_task(self._send_files(ws, msg["prefix"]))
        elif kind == "cancel" and self._cancel:
            self._cancel()
        elif kind == "approval_mode" and isinstance(msg.get("mode"), str) and self._set_mode:
            asyncio.get_running_loop().create_task(self._set_mode(msg["mode"]))
        elif kind == "approve" and msg.get("id") in self._pending:
            _, fut = self._pending[msg["id"]]
            d, note = msg.get("decision"), msg.get("note")
            if not fut.done():
                fut.set_result(ApprovalDecision(allow=d in ("yes", "always"),
                                                remember="session" if d == "always" else "no",
                                                note=note.strip() or None if isinstance(note, str) and d == "no" else None))

    async def _send_files(self, ws: web.WebSocketResponse, prefix: str) -> None:
        try:
            files = await asyncio.to_thread(list_files, self.workspace, self.workspace)
        except OSError:
            files = []
        want = prefix.lower()
        hits = sorted((f for f in files if want in f.lower()), key=lambda f: (not f.lower().startswith(want), len(f)))
        try:
            await ws.send_json({"type": "files", "prefix": prefix, "files": hits[:50]})
        except ConnectionError:
            pass

    @staticmethod
    def _decode_images(raw) -> list[BinaryContent]:
        """Valid images from a send message; malformed, oversized or surplus ones are dropped."""
        out = []
        for item in raw[:MAX_IMAGES] if isinstance(raw, list) else []:
            try:
                data = base64.b64decode(item["data"], validate=True)
                media_type = item["media_type"]
            except (KeyError, TypeError, ValueError, binascii.Error):
                continue
            if check_image(media_type, data) is None:
                out.append(BinaryContent(data, media_type=media_type))
        return out

    async def _broadcast(self, msg: dict) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except ConnectionError:
                self._clients.discard(ws)
