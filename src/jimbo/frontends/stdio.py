"""Frontend for editor extensions: the web UI's messages (bridge.py) as JSON lines, read from stdin and written
to stdout. stderr is free text for logs.

The first line written is {"type": "hello", "protocol": int, "version": str, "workspace": str}. Besides the
bridge's messages, the client can send:
  {"type": "trace_get"}            -> {"type": "trace", "html": str} or {"type": "trace", "error": str}
  {"type": "image_get", "id": str} -> {"type": "image_data", "id": str, "media_type": str, "data": base64}
                                      or {"type": "image_data", "id": str, "error": str}
  {"type": "changes_get"}          -> {"type": "changes", "files": [{"path": str, "before": str | null}]}
                                      (the latest turn that changed files; before is null for a new file)
Approval requests carry `proposed` for file tools (see ApprovalRequest). End of stdin means quit.
"""

import asyncio
import base64
import json
import os
import sys
import threading
from pathlib import Path
from typing import IO, Any, Callable

from .. import __version__
from ..core.events import PROTOCOL_VERSION
from ..core.trace import trace_page
from .bridge import Bridge


def take_stdout() -> IO[str]:
    """A private copy of stdout for the protocol. File descriptor 1 and sys.stdout then point at stderr, so
    nothing else (a library, a child process) can write a line that isn't protocol."""
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return out


class StdioFrontend(Bridge):
    def __init__(self, workspace: Path | None = None, stdin: IO[str] | None = None,
                 stdout: IO[str] | None = None) -> None:
        super().__init__(workspace)
        self._in = stdin if stdin is not None else sys.stdin
        self._out = stdout if stdout is not None else take_stdout()
        self._changes: Callable[[], list[dict]] | None = None
        self._reader: threading.Thread | None = None
        self._write({"type": "hello", "protocol": PROTOCOL_VERSION, "version": __version__,
                     "workspace": str(self.workspace.resolve())})

    def bind_changes(self, changes: Callable[[], list[dict]]) -> None:
        """Called by run_frontend so `changes_get` can list what the last turn changed."""
        self._changes = changes

    # ---- lifecycle ----
    async def start(self) -> None:
        """Start reading stdin. main calls this before the risk prompt, whose answer comes on stdin."""
        loop = asyncio.get_running_loop()

        def read() -> None:
            try:
                for line in self._in:
                    if line.strip():
                        loop.call_soon_threadsafe(self._on_message, line)
            except (OSError, ValueError):
                pass
            try:
                loop.call_soon_threadsafe(self._closed)
            except RuntimeError:  # the loop is already closed
                pass

        self._reader = threading.Thread(target=read, name="jimbo-stdin", daemon=True)
        self._reader.start()

    def _closed(self) -> None:
        """stdin ended: the client is gone. Stop the run and quit."""
        self.close_prompts()
        if self._cancel:
            self._cancel()
        self._inputs.put_nowait(None)

    async def stop(self) -> None:
        try:
            self._out.flush()
        except (OSError, ValueError):
            pass

    # ---- transport ----
    def _write(self, msg: dict) -> None:
        try:
            self._out.write(json.dumps(msg) + "\n")
            self._out.flush()
        except (OSError, ValueError):  # the client closed stdout; stdin's end will stop the session
            pass

    async def _broadcast(self, msg: dict) -> None:
        self._write(msg)

    # ---- messages only this frontend has ----
    def _on_other(self, kind: str, msg: dict, client: Any) -> None:
        if kind == "trace_get":
            self._write(self._trace_msg())
        elif kind == "image_get" and isinstance(msg.get("id"), str):
            found = self._images.get(msg["id"])
            self._write({"type": "image_data", "id": msg["id"], "media_type": found[0],
                         "data": base64.b64encode(found[1]).decode("ascii")} if found else
                        {"type": "image_data", "id": msg["id"], "error": "no longer available"})
        elif kind == "changes_get":
            self._write({"type": "changes", "files": self._changes() if self._changes else []})

    def _trace_msg(self) -> dict:
        log = self._trace_log() if self._trace_log else None
        if log is None:
            return {"type": "trace", "error": "session logging is disabled (log_sessions = false)"}
        try:
            return {"type": "trace", "html": trace_page(log)}
        except OSError:
            return {"type": "trace", "error": "this session has no log yet"}
