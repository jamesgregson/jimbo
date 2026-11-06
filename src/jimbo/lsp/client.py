"""A small LSP client: JSON-RPC over a server process's stdin and stdout, with plain dicts for messages."""

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from ..tools.sandbox import SandboxError, kill, spawn

if TYPE_CHECKING:
    from ..tools.sandbox import Sandbox

SHUTDOWN_WAIT = 2.0  # seconds a server gets to exit after `exit` before it is killed
PROGRESS_GRACE = 1.0  # seconds after start in which a server may still begin reporting work, such as indexing


class LspError(Exception):
    pass


def path_uri(path: Path) -> str:
    return path.as_uri()


def uri_path(uri: str) -> Path | None:
    """The file a `file:` URI names; None for other schemes."""
    u = urlparse(uri)
    return Path(unquote(u.path)) if u.scheme == "file" else None


def utf16_col(line: str, index: int) -> int:
    """LSP columns count UTF-16 code units, not characters."""
    return len(line[:index].encode("utf-16-le")) // 2


def str_index(line: str, col: int) -> int:
    """The string index of UTF-16 column `col` in `line`."""
    units = 0
    for i, ch in enumerate(line):
        if units >= col:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def capabilities() -> dict:
    """What jimbo tells a server it can do: only what the tools in tools/lsp.py use."""
    return {
        "general": {"positionEncodings": ["utf-16"]},
        "window": {"workDoneProgress": True},
        "workspace": {"configuration": True, "workspaceFolders": True, "symbol": {}},
        "textDocument": {
            "synchronization": {"didSave": True},
            "publishDiagnostics": {"versionSupport": True},
            "definition": {"linkSupport": True},
            "references": {},
            "hover": {"contentFormat": ["plaintext", "markdown"]},
            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
            "rename": {},
        },
    }


class LspClient:
    """One running server. `diagnostics` holds the latest publishDiagnostics per URI."""

    def __init__(self, name: str, argv: list[str], root: Path, *, env: dict[str, str] | None = None,
                 settings: dict | None = None, init_options: dict | None = None, log_path: Path | None = None,
                 sandbox: "Sandbox | None" = None) -> None:
        self.name = name
        self.argv = argv
        self.sandbox = sandbox
        self.root = root
        self.env = env or {}
        self.settings = settings or {}
        self.init_options = init_options or {}
        self.log_path = log_path
        self.capabilities: dict = {}
        self.diagnostics: dict[str, list[dict]] = {}
        self.published: dict[str, int] = {}  # URI -> how many times diagnostics arrived for it
        self.versions: dict[str, int | None] = {}  # URI -> the document version the latest diagnostics are for
        self.docs: dict[str, dict] = {}  # open documents, kept by the manager: URI -> version, text, stat
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._changed = asyncio.Condition()  # notified when diagnostics arrive or progress ends
        self._progress: set = set()  # work-done progress tokens that began and haven't ended
        self._seen_progress = False
        self._started_at = 0.0

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._reader is not None \
            and not self._reader.done()

    async def start(self, timeout: float) -> None:
        log = None
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                log = open(self.log_path, "ab")  # noqa: SIM115 - the server writes its stderr here
            except OSError:
                pass
        try:
            self._proc = await spawn(self.sandbox, self.argv, cwd=self.root, env=self.env,
                                     stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                     stderr=log or asyncio.subprocess.DEVNULL)
        except (OSError, SandboxError) as e:
            raise LspError(f"language server {self.name} didn't start: {e}") from None
        finally:
            if log:
                log.close()
        self._reader = asyncio.create_task(self._read_loop())
        self._started_at = asyncio.get_running_loop().time()
        root = path_uri(self.root)
        # A server may exit when this process is gone; in a container, jimbo's pid means nothing.
        pid = None if self.sandbox and self.sandbox.container else os.getpid()
        try:
            result = await self.request("initialize", {
                "processId": pid, "rootUri": root, "rootPath": str(self.root),
                "workspaceFolders": [{"uri": root, "name": self.root.name}],
                "clientInfo": {"name": "jimbo"}, "capabilities": capabilities(),
                "initializationOptions": self.init_options}, timeout)
        except LspError:
            await self.shutdown()
            raise
        self.capabilities = (result or {}).get("capabilities", {})
        await self.notify("initialized", {})
        if self.settings:
            await self.notify("workspace/didChangeConfiguration", {"settings": self.settings})

    async def request(self, method: str, params: Any, timeout: float) -> Any:
        if not self.alive:
            raise LspError(f"language server {self.name} is not running")
        self._next_id += 1
        rid = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            await self.notify("$/cancelRequest", {"id": rid})
            raise LspError(f"language server {self.name} didn't answer {method} in {timeout:g}s") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any) -> None:
        if self.alive:
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def wait_diagnostics(self, uri: str, after: int, timeout: float) -> bool:
        """Wait until diagnostics for `uri` arrive more than `after` times, for the version jimbo sent last if
        the server says which version they are for. False on timeout or exit."""
        def fresh() -> bool:
            sent = (self.docs.get(uri) or {}).get("version")
            got = self.versions.get(uri)
            return self.published.get(uri, 0) > after and (got is None or sent is None or got >= sent)

        async def ready() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: fresh() or not self.alive)
        try:
            await asyncio.wait_for(ready(), timeout)
        except TimeoutError:
            return False
        return fresh()

    async def wait_idle(self, timeout: float) -> None:
        """Wait until the server reports no work in progress, such as indexing. Gives up quietly.

        Just after start a server may not have begun its work yet (clangd starts indexing when the first file is
        opened), so the first PROGRESS_GRACE seconds also wait for work to begin."""
        async def begun() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: self._seen_progress or not self.alive)

        async def idle() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: not self._progress or not self.alive)
        grace = self._started_at + PROGRESS_GRACE - asyncio.get_running_loop().time()
        try:
            if not self._seen_progress and grace > 0:
                try:
                    await asyncio.wait_for(begun(), grace)
                except TimeoutError:
                    pass
            await asyncio.wait_for(idle(), timeout)
        except TimeoutError:
            pass

    async def shutdown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if self.alive:
            try:
                await self.request("shutdown", None, SHUTDOWN_WAIT)
                await self.notify("exit", None)
            except (LspError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), SHUTDOWN_WAIT)
        except TimeoutError:
            pass
        await kill(self.sandbox, proc)  # whatever it started, too
        await proc.wait()
        if self._reader:
            self._reader.cancel()
        self._fail_pending("stopped")

    async def _send(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps(message).encode()
        async with self._write_lock:
            try:
                self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                await self._proc.stdin.drain()
            except (ConnectionError, RuntimeError) as e:
                raise LspError(f"language server {self.name} exited ({e})") from None

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        out = self._proc.stdout
        try:
            while True:
                length = None
                while (line := await out.readline()) not in (b"\r\n", b"\n"):
                    if not line:
                        return
                    name, _, value = line.decode("ascii", "replace").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                try:
                    message = json.loads(await out.readexactly(length))
                except json.JSONDecodeError:
                    continue
                await self._dispatch(message)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            return
        finally:
            self._fail_pending("exited")
            async with self._changed:
                self._changed.notify_all()

    def _fail_pending(self, how: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LspError(f"language server {self.name} {how}"))
        self._pending.clear()

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:  # a response to one of our requests
            future = self._pending.get(message.get("id"))
            if future and not future.done():
                if err := message.get("error"):
                    future.set_exception(LspError(f"{self.name}: {err.get('message', err)}"))
                else:
                    future.set_result(message.get("result"))
        elif "id" in message:
            await self._answer(message["id"], method, message.get("params") or {})
        elif method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = params.get("uri", "")
            async with self._changed:
                self.diagnostics[uri] = params.get("diagnostics") or []
                self.versions[uri] = params.get("version")
                self.published[uri] = self.published.get(uri, 0) + 1
                self._changed.notify_all()
        elif method == "$/progress":
            params = message.get("params") or {}
            kind = (params.get("value") or {}).get("kind")
            async with self._changed:
                if kind == "begin":
                    self._progress.add(params.get("token"))
                    self._seen_progress = True
                    self._changed.notify_all()
                elif kind == "end":
                    self._progress.discard(params.get("token"))
                    self._changed.notify_all()

    async def _answer(self, rid: Any, method: str, params: dict) -> None:
        """Requests from the server. jimbo has no UI for them, so most get an empty answer."""
        result: Any = None
        match method:
            case "workspace/configuration":
                result = [self._setting(item.get("section")) for item in params.get("items", [])]
            case "workspace/workspaceFolders":
                result = [{"uri": path_uri(self.root), "name": self.root.name}]
            case "workspace/applyEdit":
                result = {"applied": False, "failureReason": "jimbo applies edits itself"}
            case ("client/registerCapability" | "client/unregisterCapability" | "window/workDoneProgress/create"
                  | "window/showMessageRequest" | "window/showDocument" | "workspace/semanticTokens/refresh"
                  | "workspace/inlayHint/refresh" | "workspace/codeLens/refresh" | "workspace/diagnostic/refresh"):
                pass
            case _:
                await self._send({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601, "message": f"jimbo doesn't handle {method}"}})
                return
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _setting(self, section: str | None) -> Any:
        value: Any = self.settings
        for key in section.split(".") if section else ():
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]
        return value
