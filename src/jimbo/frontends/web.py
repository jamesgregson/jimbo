"""Browser frontend: a local aiohttp server speaking JSON over one WebSocket.

Server -> client: {"type": "event", "event": Event}, {"type": "approval", "request": ApprovalRequest},
                  {"type": "approval_closed", "id": str}, {"type": "files", "prefix": str, "files": [str]},
                  {"type": "secret", "id": str, "message": str}, {"type": "secret_closed", "id": str},
                  {"type": "models", "provider": str, "models"?: [str], "error"?: str}
Client -> server: {"type": "send", "text": str, "images"?: [{"media_type": str, "data": base64}]},
                  {"type": "approve", "id": str, "decision": "yes|no|always", "note"?: str  (why, on "no")},
                  {"type": "cancel"}, {"type": "complete", "prefix": str}  (workspace files for @path),
                  {"type": "approval_mode", "mode": "default|auto|yolo"}  (allowed mid-run and during a prompt),
                  {"type": "secret_answer", "id": str, "value": str | null}  (null: cancelled),
                  {"type": "config_get"}  (answered with a `config` event),
                  {"type": "config_set", "op": str, ...}  (see ConfigControl.apply; refused during a run):
                      op "main": value, save?; "alias_set": name, spec; "alias_rm": name;
                      "task_set": task, value (null: back to the default); "provider_set": name, base_url?,
                      key?, key_env?, new?; "provider_rm": name; "provider_test": name
                  {"type": "models_list", "provider": str}  (the provider's model names, for pickers)
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
from ..core.trace import trace_page
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
        self._config: Callable[[dict], Awaitable[None]] | None = None
        self._list_models: Callable[[str], Awaitable[list[str] | str]] | None = None
        self._trace_log: Callable[[], Path | None] | None = None
        self._secrets: dict[str, tuple[str, asyncio.Future[str | None]]] = {}
        self._tasks: set[asyncio.Task] = set()

    def bind_cancel(self, cancel: Callable[[], object]) -> None:
        """Called by run_frontend so the page's stop button can interrupt the run."""
        self._cancel = cancel

    def bind_approval_mode(self, set_mode: Callable[[str], Awaitable[None]]) -> None:
        """Called by run_frontend so the page's approval mode control can switch modes."""
        self._set_mode = set_mode

    def bind_config(self, handle: Callable[[dict], Awaitable[None]]) -> None:
        """Called by run_frontend so the settings panel can read and change models and providers."""
        self._config = handle

    def bind_model_list(self, lister: Callable[[str], Awaitable[list[str] | str]]) -> None:
        """Called by run_frontend so model pickers can offer the provider's model names."""
        self._list_models = lister

    def bind_trace(self, log: Callable[[], Path | None]) -> None:
        """Called by run_frontend so /trace can show the current session's log; None when logging is off."""
        self._trace_log = log

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

    async def ask_secret(self, message: str) -> str | None:
        """A password field in a dialog. The answer is never added to the replayed history."""
        fut: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        sid = secrets.token_hex(8)
        self._secrets[sid] = (message, fut)
        await self._broadcast({"type": "secret", "id": sid, "message": message})
        try:
            return await fut
        finally:
            self._secrets.pop(sid, None)
            await self._broadcast({"type": "secret_closed", "id": sid})

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
        app.router.add_get("/trace", self._trace)
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
        for sid, (message, _) in self._secrets.items():
            await ws.send_json({"type": "secret", "id": sid, "message": message})
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
            self._spawn(self._send_files(ws, msg["prefix"]))
        elif kind == "cancel" and self._cancel:
            self._cancel()
        elif kind == "approval_mode" and isinstance(msg.get("mode"), str) and self._set_mode:
            self._spawn(self._set_mode(msg["mode"]))
        elif kind == "config_get" and self._config:
            self._spawn(self._config({"op": "get"}))
        elif kind == "config_set" and self._config:
            self._spawn(self._config({k: v for k, v in msg.items() if k != "type"}))
        elif kind == "models_list" and isinstance(msg.get("provider"), str) and self._list_models and ws is not None:
            self._spawn(self._send_models(ws, msg["provider"]))
        elif kind == "secret_answer" and msg.get("id") in self._secrets:
            fut = self._secrets[msg["id"]][1]
            if not fut.done():
                fut.set_result(msg["value"] if isinstance(msg.get("value"), str) else None)
        elif kind == "approve" and msg.get("id") in self._pending:
            _, fut = self._pending[msg["id"]]
            d, note = msg.get("decision"), msg.get("note")
            if not fut.done():
                fut.set_result(ApprovalDecision(allow=d in ("yes", "always"),
                                                remember="session" if d == "always" else "no",
                                                note=note.strip() or None if isinstance(note, str) and d == "no" else None))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)

    async def _send_models(self, ws: web.WebSocketResponse, provider: str) -> None:
        found = await self._list_models(provider)
        msg = {"type": "models", "provider": provider}
        msg.update({"models": found} if isinstance(found, list) else {"error": found})
        try:
            await ws.send_json(msg)
        except ConnectionError:
            pass

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
