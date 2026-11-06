"""LspManager: one client per server for the session, started on first use with the user's approval."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.deps import Deps, ToolDenied
from .client import LspClient, LspError, path_uri, uri_path
from .servers import RESTART_ON, Choice, Server

if TYPE_CHECKING:
    from ..tools.sandbox import Sandbox

MAX_EXITS = 3  # a server that exits this often is not started again until /lsp restart


class LspUnavailable(Exception):
    """No server can answer for a file. The message is for the model."""


class LspManager:
    def __init__(self, workspace: Path, choices: list[Choice], *, timeout: float = 60.0, wait: float = 3.0,
                 diagnostics: bool = True, sandbox: "Sandbox | None" = None,
                 log_path: Callable[[str], Path | None] | None = None) -> None:
        self.workspace = workspace
        self.choices = choices
        self.timeout = timeout
        self.wait = wait
        self.diagnostics = diagnostics  # report errors after edits
        self.sandbox = sandbox
        self.log_path = log_path
        self.by_ext = {e: c.server for c in choices if c.server for e in c.server.ids}
        self.servers = {c.server.name: c.server for c in choices if c.server}
        self.clients: dict[str, LspClient] = {}
        self.approved: set[str] = set()  # started once this session: restarts don't ask again
        self.denied: dict[str, str] = {}  # name -> why; not asked about again until /lsp start
        self.exits: dict[str, int] = {}
        self._stamps: dict[str, tuple] = {}  # name -> RESTART_ON files as the running server found them
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return bool(self.servers)

    def languages(self) -> list[str]:
        return [c.language for c in self.choices if c.server]

    def server_for(self, path: Path) -> Server | None:
        return self.by_ext.get(path.suffix.lower())

    def running(self, name: str) -> LspClient | None:
        client = self.clients.get(name)
        return client if client and client.alive else None

    async def client_for(self, server: Server, deps: Deps, call_id: str | None) -> LspClient:
        """A running client for `server`. The first start in a session asks through the approval gate."""
        async with self._locks.setdefault(server.name, asyncio.Lock()):  # parallel calls start it once
            if client := self.running(server.name):
                if self._stamp(server.name) == self._stamps.get(server.name):
                    return client
                await self.stop(server.name)  # its config changed: start it again, without asking
            if server.name in self.clients:  # it was running and exited
                self.exits[server.name] = self.exits.get(server.name, 0) + 1
                del self.clients[server.name]
            if self.exits.get(server.name, 0) >= MAX_EXITS:
                raise LspUnavailable(f"language server {server.name} exited {MAX_EXITS} times and is off; "
                                     f"the user can turn it on with /lsp restart {server.name}")
            if server.name in self.denied:
                raise LspUnavailable(f"language server {server.name} is off: the user declined it this session "
                                     f"({self.denied[server.name]})")
            if server.name not in self.approved:
                try:
                    where = "in the sandbox" if self.sandbox else "not sandboxed"
                    await deps.authorize(call_id, "lsp_start", {"server": server.name},
                                         f"start language server {server.name} for {server.language}",
                                         detail=f"$ {' '.join(server.command)}\n\nIt runs {where} until jimbo exits. "
                                                "jimbo uses it to find definitions and references, rename symbols "
                                                "and report errors after edits. Yes lasts for this session.",
                                         wording={"title": "Start a language server?", "always": None,
                                                  "allow_label": "Start it", "deny_label": "Don't start"})
                except ToolDenied as e:
                    self.denied[server.name] = str(e)
                    raise LspUnavailable(f"language server {server.name} was not started: {e}") from None
                self.approved.add(server.name)
            return await self._start(server)

    def _stamp(self, name: str) -> tuple:
        def one(path: Path):
            try:
                st = path.stat()
            except OSError:
                return None
            return st.st_mtime_ns, st.st_size
        return tuple(one(self.workspace / f) for f in RESTART_ON.get(name, ()))

    async def _start(self, server: Server) -> LspClient:
        client = LspClient(server.name, list(server.command), self.workspace, env=server.env,
                           settings=server.settings, init_options=server.init_options,
                           log_path=self.log_path(server.name) if self.log_path else None,
                           wrap=self.sandbox.wrap if self.sandbox else None)
        stamp = self._stamp(server.name)
        try:
            await client.start(self.timeout)
        except LspError as e:
            raise LspUnavailable(str(e)) from None
        self.clients[server.name] = client
        self._stamps[server.name] = stamp
        return client

    async def start(self, name: str) -> None:
        """/lsp start and restart: the user asked, so no approval and no limit on exits."""
        if name not in self.servers:
            raise LspUnavailable(f"no language server {name!r} (see /lsp)")
        await self.stop(name)
        self.denied.pop(name, None)
        self.exits.pop(name, None)
        self.approved.add(name)
        async with self._locks.setdefault(name, asyncio.Lock()):
            await self._start(self.servers[name])

    async def stop(self, name: str) -> bool:
        if client := self.clients.pop(name, None):
            await client.shutdown()
            return True
        return False

    async def close(self) -> None:
        await asyncio.gather(*(c.shutdown() for c in self.clients.values()), return_exceptions=True)
        self.clients.clear()

    def language_id(self, client: LspClient, path: Path) -> str:
        server = self.servers.get(client.name)
        return server.ids.get(path.suffix.lower(), "plaintext") if server else "plaintext"

    async def sync(self, client: LspClient, path: Path, text: str | None = None) -> int | None:
        """Send the file's contents to the server if it hasn't seen them, or `text` in their place. Returns how
        many times diagnostics had arrived for the file before, for wait_diagnostics; None if nothing was sent."""
        uri = path_uri(path)
        mark = client.published.get(uri, 0)
        if text is None:
            try:
                st = path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                return None
            doc = client.docs.get(uri)
            if doc is not None and doc["stamp"] == stamp:
                return None
            text = path.read_bytes().decode("utf-8", "replace")
        else:
            stamp = None  # not what's on disk: the next sync sends the file
        doc = client.docs.get(uri)
        if doc is None:
            client.docs[uri] = {"version": 1, "stamp": stamp}
            await client.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": self.language_id(client, path), "version": 1, "text": text}})
        else:
            doc["version"] += 1
            doc["stamp"] = stamp
            await client.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": doc["version"]},
                                                           "contentChanges": [{"text": text}]})
            await client.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        return mark

    async def sync_open(self, client: LspClient) -> None:
        """Send files the server has open that changed on disk, for example through shell."""
        for uri in list(client.docs):
            if (p := uri_path(uri)) is not None:
                await self.sync(client, p)
