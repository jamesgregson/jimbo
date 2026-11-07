"""What the web and stdio frontends share: the JSON messages a page (or the VS Code extension) exchanges with
jimbo, and the prompts waiting for its answer. Subclasses say how a message reaches the client (`_broadcast`)
and how a reply reaches the one client that asked (`_reply`).

Server -> client: {"type": "event", "event": Event}, {"type": "approval", "request": ApprovalRequest},
                  {"type": "approval_closed", "id": str}, {"type": "files", "prefix": str, "files": [str]},
                  {"type": "secret", "id": str, "message": str}, {"type": "secret_closed", "id": str},
                  {"type": "confirm", "id": str, "message": str}, {"type": "confirm_closed", "id": str},
                  {"type": "questions", "id": str, "questions": [Question]}, {"type": "questions_closed", "id": str},
                  {"type": "models", "provider": str, "models"?: [str], "error"?: str}
Client -> server: {"type": "send", "text": str, "images"?: [{"media_type": str, "data": base64}]},
                  {"type": "approve", "id": str, "decision": "yes|no|always", "note"?: str  (why, on "no")},
                  {"type": "cancel"}, {"type": "complete", "prefix": str}  (workspace files for @path),
                  {"type": "approval_mode", "mode": "default|auto|yolo"}  (allowed mid-run and during a prompt),
                  {"type": "secret_answer", "id": str, "value": str | null}  (null: cancelled),
                  {"type": "confirm_answer", "id": str, "value": bool}  (only true is yes),
                  {"type": "answers", "id": str, "answers": [str | [str] | null], "feedback"?: str}  (a list
                      for a question with `multiple`; feedback: the user
                      stopped answering to say this; a "send" while questions are open is feedback too),
                  {"type": "config_get"}  (answered with a `config` event),
                  {"type": "config_set", "op": str, ...}  (see ConfigControl.apply; refused during a run):
                      op "main": value, save?; "alias_set": name, spec; "alias_rm": name;
                      "task_set": task, value (null: back to the default); "provider_set": name, base_url?,
                      key?, key_env?, new?; "provider_rm": name; "provider_test": name
                  {"type": "models_list", "provider": str}  (the provider's model names, for pickers)

Subclasses may handle more message types in `_on_other` (see stdio.py).
"""

import asyncio
import base64
import binascii
import json
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic_ai.messages import BinaryContent

from ..core.approval import ApprovalDecision, ApprovalRequest
from ..core.attachments import MAX_IMAGE_BYTES, MAX_IMAGES, UserMessage, check_image
from ..core.events import Event
from ..core.questions import Answers, Question, check_answer
from ..tools.search import list_files

MAX_REPLAY = 5000
MAX_SHOWN = 100  # images kept for the client; older ones show as no longer available


class Bridge:
    supports_steering = True  # the client keeps sending while a run is in flight

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self._inputs: asyncio.Queue[str | UserMessage | None] = asyncio.Queue()
        self._history: list[dict] = []
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalDecision]]] = {}
        self._cancel: Callable[[], object] | None = None
        self._set_mode: Callable[[str], Awaitable[None]] | None = None
        self._config: Callable[[dict], Awaitable[None]] | None = None
        self._list_models: Callable[[str], Awaitable[list[str] | str]] | None = None
        self._trace_log: Callable[[], Path | None] | None = None
        self._secrets: dict[str, tuple[str, asyncio.Future[str | None]]] = {}
        self._confirms: dict[str, tuple[str, asyncio.Future[bool]]] = {}
        self._questions: dict[str, tuple[list[Question], asyncio.Future[Answers]]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._images: dict[str, tuple[str, bytes]] = {}  # image event id -> (media type, the file as it was shown)

    # ---- transport, provided by subclasses ----
    async def _broadcast(self, msg: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def _reply(self, client: Any, msg: dict) -> None:
        """Send `msg` to the client that asked; by default, to everyone."""
        await self._broadcast(msg)

    def _on_other(self, kind: str, msg: dict, client: Any) -> None:
        """A message type the bridge doesn't know; ignored unless a subclass handles it."""

    # ---- bindings, called by run_frontend ----
    def bind_cancel(self, cancel: Callable[[], object]) -> None:
        """So the client's stop button can interrupt the run."""
        self._cancel = cancel

    def bind_approval_mode(self, set_mode: Callable[[str], Awaitable[None]]) -> None:
        """So the client's approval mode control can switch modes."""
        self._set_mode = set_mode

    def bind_config(self, handle: Callable[[dict], Awaitable[None]]) -> None:
        """So the settings panel can read and change models and providers."""
        self._config = handle

    def bind_model_list(self, lister: Callable[[str], Awaitable[list[str] | str]]) -> None:
        """So model pickers can offer the provider's model names."""
        self._list_models = lister

    def bind_trace(self, log: Callable[[], Path | None]) -> None:
        """So the client can show the current session's log; None when logging is off."""
        self._trace_log = log

    # ---- Frontend protocol ----
    async def next_user_input(self) -> str | UserMessage | None:
        return await self._inputs.get()

    shows_images = True  # the show_image tool is offered; see _keep_image

    async def render(self, event: Event) -> None:
        if event.kind == "image":
            self._keep_image(event.data)
        msg = {"type": "event", "event": json.loads(event.model_dump_json())}
        self._history.append(msg)
        del self._history[:-MAX_REPLAY]
        await self._broadcast(msg)

    def _keep_image(self, d: dict) -> None:
        """Read the image now, so the client shows it as it was when shown, even if the file changes later. A
        resumed session's images are read as they are now; missing ones stay unavailable."""
        try:
            data = Path(d["file"]).read_bytes()
        except (OSError, KeyError):
            return
        if len(data) <= MAX_IMAGE_BYTES:
            self._images[d["id"]] = (d["media_type"], data)
            for old in list(self._images)[:-MAX_SHOWN]:
                del self._images[old]

    def _approval_msg(self, req: ApprovalRequest) -> dict:
        return {"type": "approval", "request": req.model_dump()}

    async def request_approval(self, req: ApprovalRequest) -> ApprovalDecision:
        fut: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = (req, fut)
        await self._broadcast(self._approval_msg(req))
        try:
            return await fut
        finally:
            self._pending.pop(req.id, None)
            await self._broadcast({"type": "approval_closed", "id": req.id})

    async def confirm(self, message: str) -> bool:
        """A dialog with Yes and No. Only Yes counts."""
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        cid = secrets.token_hex(8)
        self._confirms[cid] = (message, fut)
        await self._broadcast({"type": "confirm", "id": cid, "message": message})
        try:
            return await fut
        finally:
            self._confirms.pop(cid, None)
            await self._broadcast({"type": "confirm_closed", "id": cid})

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

    async def ask_questions(self, questions: list[Question]) -> Answers:
        """All the questions in one dialog, with a box to stop and say something else instead."""
        fut: asyncio.Future[Answers] = asyncio.get_running_loop().create_future()
        qid = secrets.token_hex(8)
        self._questions[qid] = (questions, fut)
        await self._broadcast(self._questions_msg(qid, questions))
        try:
            return await fut
        finally:
            self._questions.pop(qid, None)
            await self._broadcast({"type": "questions_closed", "id": qid})

    @staticmethod
    def _questions_msg(qid: str, questions: list[Question]) -> dict:
        return {"type": "questions", "id": qid, "questions": [q.model_dump() for q in questions]}

    def _answer_questions(self, qid: str, answers, feedback) -> None:
        questions, fut = self._questions[qid]
        answers = answers if isinstance(answers, list) else []
        got = [check_answer(q, a) for q, a in zip(questions, answers + [None] * len(questions))]
        if not fut.done():
            fut.set_result(Answers(answers=got, feedback=feedback.strip() or None if isinstance(feedback, str) else None))

    def submit(self, text: str) -> None:
        self._inputs.put_nowait(text)

    # ---- state for a client that (re)connects ----
    def replay(self) -> list[dict]:
        """Everything a new client needs: the event history, then the prompts still waiting for an answer."""
        out = list(self._history)
        out += [self._approval_msg(req) for req, _ in self._pending.values()]
        out += [{"type": "secret", "id": sid, "message": m} for sid, (m, _) in self._secrets.items()]
        out += [self._questions_msg(qid, qs) for qid, (qs, _) in self._questions.items()]
        out += [{"type": "confirm", "id": cid, "message": m} for cid, (m, _) in self._confirms.items()]
        return out

    def close_prompts(self) -> None:
        """The client is gone for good: deny approvals and cancel the other prompts, so the run can end."""
        for _, fut in self._pending.values():
            if not fut.done():
                fut.set_result(ApprovalDecision(allow=False, note="the client closed"))
        for _, fut in self._secrets.values():
            if not fut.done():
                fut.set_result(None)
        for _, fut in self._confirms.values():
            if not fut.done():
                fut.set_result(False)
        for qid in list(self._questions):
            self._answer_questions(qid, [], None)

    # ---- incoming messages ----
    def _on_message(self, raw: str, client: Any = None) -> None:
        try:
            msg = json.loads(raw)
            kind = msg["type"]
        except (ValueError, KeyError, TypeError):
            return
        if not isinstance(kind, str):
            return
        if kind == "send" and isinstance(msg.get("text"), str) and msg["text"].strip() and self._questions:
            for qid in list(self._questions):  # typed instead of answering: it stops the questions
                self._answer_questions(qid, [], msg["text"])
        elif kind == "send" and isinstance(msg.get("text"), str):
            images = self._decode_images(msg.get("images"))
            if msg["text"].strip() or images:
                self._inputs.put_nowait(UserMessage(msg["text"], images) if images else msg["text"])
        elif kind == "complete" and isinstance(msg.get("prefix"), str):
            self._spawn(self._send_files(client, msg["prefix"]))
        elif kind == "cancel" and self._cancel:
            self._cancel()
        elif kind == "approval_mode" and isinstance(msg.get("mode"), str) and self._set_mode:
            self._spawn(self._set_mode(msg["mode"]))
        elif kind == "config_get" and self._config:
            self._spawn(self._config({"op": "get"}))
        elif kind == "config_set" and self._config:
            self._spawn(self._config({k: v for k, v in msg.items() if k != "type"}))
        elif kind == "models_list" and isinstance(msg.get("provider"), str) and self._list_models:
            self._spawn(self._send_models(client, msg["provider"]))
        elif kind == "secret_answer" and msg.get("id") in self._secrets:
            fut = self._secrets[msg["id"]][1]
            if not fut.done():
                fut.set_result(msg["value"] if isinstance(msg.get("value"), str) else None)
        elif kind == "confirm_answer" and msg.get("id") in self._confirms:
            fut = self._confirms[msg["id"]][1]
            if not fut.done():
                fut.set_result(msg.get("value") is True)
        elif kind == "answers" and msg.get("id") in self._questions:
            self._answer_questions(msg["id"], msg.get("answers"), msg.get("feedback"))
        elif kind == "approve" and msg.get("id") in self._pending:
            _, fut = self._pending[msg["id"]]
            d, note = msg.get("decision"), msg.get("note")
            if not fut.done():
                fut.set_result(ApprovalDecision(allow=d in ("yes", "always"),
                                                remember="session" if d == "always" else "no",
                                                note=note.strip() or None if isinstance(note, str) and d == "no" else None))
        else:
            self._on_other(kind, msg, client)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)

    async def _send_models(self, client: Any, provider: str) -> None:
        found = await self._list_models(provider)
        msg = {"type": "models", "provider": provider}
        msg.update({"models": found} if isinstance(found, list) else {"error": found})
        await self._reply(client, msg)

    async def _send_files(self, client: Any, prefix: str) -> None:
        try:
            files = await asyncio.to_thread(list_files, self.workspace, self.workspace)
        except OSError:
            files = []
        want = prefix.lower()
        hits = sorted((f for f in files if want in f.lower()), key=lambda f: (not f.lower().startswith(want), len(f)))
        await self._reply(client, {"type": "files", "prefix": prefix, "files": hits[:50]})

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
