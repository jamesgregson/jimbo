"""Browser frontend: a local aiohttp server speaking JSON over one WebSocket. The messages are listed in
bridge.py."""

import errno
import hmac
import secrets
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from ..core.approval import ApprovalRequest
from ..core.attachments import MAX_IMAGE_BYTES, MAX_IMAGES
from ..core.trace import trace_page
from .bridge import Bridge

STATIC = Path(__file__).parent / "web_static"
# An SVG opened on its own runs as a page on this origin, so shown images get no scripts and no network.
IMAGE_HEADERS = {"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"}
PORT_ATTEMPTS = 20  # how many ports from the configured one to try
MAX_WS_MESSAGE = MAX_IMAGES * (MAX_IMAGE_BYTES * 4 // 3 + 1024) + 1_000_000  # base64 images plus text


class WebFrontend(Bridge):
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
                 workspace: Path | None = None) -> None:
        super().__init__(workspace)
        self.host, self.port, self.open_browser = host, port, open_browser
        self.token = secrets.token_urlsafe(16)  # the socket can approve file writes: require it
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None

    def _approval_msg(self, req: ApprovalRequest) -> dict:
        return {"type": "approval", "request": req.model_dump(exclude={"proposed"})}  # the page shows the diff

    # ---- lifecycle ----
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/trace", self._trace)
        app.router.add_get("/image/{id}", self._image)
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

    # ---- transport ----
    async def _broadcast(self, msg: dict) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except ConnectionError:
                self._clients.discard(ws)

    async def _reply(self, client, msg: dict) -> None:
        if client is None:
            return
        try:
            await client.send_json(msg)
        except ConnectionError:
            pass

    # ---- handlers ----
    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=(STATIC / "index.html").read_text("utf-8"), content_type="text/html")

    async def _trace(self, request: web.Request) -> web.Response:
        if not self._authorized(request):  # the log holds the whole conversation
            raise web.HTTPForbidden()
        log = self._trace_log() if self._trace_log else None
        if log is None:
            raise web.HTTPNotFound(text="session logging is disabled (log_sessions = false)")
        try:
            return web.Response(text=trace_page(log), content_type="text/html")
        except OSError:
            raise web.HTTPNotFound(text="this session has no log yet") from None

    async def _image(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPForbidden()
        if (found := self._images.get(request.match_info["id"])) is None:
            raise web.HTTPNotFound()
        media_type, data = found
        return web.Response(body=data, content_type=media_type, headers=IMAGE_HEADERS)

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
        for msg in self.replay():
            await ws.send_json(msg)
        self._clients.add(ws)
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    self._on_message(m.data, ws)
        finally:
            self._clients.discard(ws)
        return ws
