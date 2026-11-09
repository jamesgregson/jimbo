import asyncio
import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from .approval import ApprovalGate, ToolCallInfo
from .checkpoints import Checkpoints
from ..tools.paths import PathEscape, resolve_in_roots

if TYPE_CHECKING:
    from .agents import AgentResult, AgentSpec
    from ..lsp.manager import LspManager
    from .models import ModelRouter
    from .questions import Answers, Question
    from .skills import Skill
    from ..tools.sandbox import Sandbox
    from ..tools.shell import BackgroundProcess
    from ..tools.todos import TodoItem


def protected_match(p: Path, workspace: Path, patterns: list[str]) -> str | None:
    """The workspace-relative path of `p`, or of a file under it if it is a folder, that matches `patterns`."""
    if not patterns or not (p == workspace or p.is_relative_to(workspace)):
        return None
    from ..tools.search import SKIP_DIRS, glob_to_regex
    rxs = [glob_to_regex(g) for g in patterns]
    rel = p.relative_to(workspace).as_posix()
    if rel != "." and any(rx.fullmatch(rel) or rx.fullmatch(f"{rel}/_") for rx in rxs):  # _: `dir/**` covers dir
        return rel
    if p.is_dir() and not p.is_symlink():
        seen = 0
        for top, dirs, files in os.walk(p):
            for name in [*files, *dirs]:
                r = Path(top, name).relative_to(workspace).as_posix()
                if name == ".git" or any(rx.fullmatch(r) for rx in rxs):  # a nested repository counts too
                    return r
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            seen += len(files)
            if seen > 20_000:
                break
    return None


class ToolDenied(Exception):
    def __init__(self, reason: str | None):
        super().__init__(reason or "denied")
        self.reason = reason


@dataclass
class Deps:
    workspace: Path
    gate: ApprovalGate
    sandbox: "Sandbox | None" = None
    sandbox_skipped: list[dict] = field(default_factory=list)  # sandboxes auto tried first: {"kind", "reason"}
    shell_timeout: int = 120
    shell_timeout_max: int = 600
    models: "ModelRouter | None" = None
    todos: "list[TodoItem]" = field(default_factory=list)
    mode: Literal["normal", "plan"] = "normal"
    project_instructions: str = ""
    vision: bool = True  # False: the model can't read images, so none are sent to it
    user_dir: Path | None = None  # ~/.jimbo, set by wiring: where user memory lives
    scratch_root: Path | None = None  # set by wiring: <user_dir>/scratch, which holds a folder per session
    scratch: Path | None = None  # set by the session: <scratch_root>/<session id>; agents use their parent's
    skills: "dict[str, Skill]" = field(default_factory=dict)
    secret_files: list[str] = field(default_factory=list)  # file-name globs the model must not read
    git_write: bool = False  # shell commands may change the local repository (settings.git_write)
    protected: list[str] = field(default_factory=list)  # workspace-relative globs file tools can't change
    protected_write: str = "deny"  # "ask": file tools may change protected paths, asking each time
    read_paths: list[Path] = field(default_factory=list)  # read-only folders outside: settings.read_paths, libraries
    # File -> hash of its contents when the model last read or wrote it. A write to a file that changed since is
    # refused. Agents share it with their parent.
    seen_files: dict[Path, str] = field(default_factory=dict)
    failures: dict = field(default_factory=dict)  # this run's failed calls: (tool, args, outcome) -> count
    stop_reason: str | None = None  # set when a call kept failing: the turn ends with a text-only reply
    outside_attempts: int = 0  # this run's tries to reach outside the workspace; the session resets it
    checkpoints: Checkpoints = field(default_factory=Checkpoints)
    request_limit: int | None = None  # set by the session: model requests allowed per turn
    wrap_up_left: int | None = None  # set by the prompt when it tells the model to wrap up; the session logs it
    # Set by the session: compacts a message list mid-run when the context is nearly full.
    midrun_compact: "Callable[[list[Any]], Awaitable[list[Any]]] | None" = None
    # Set by the session: appends queued steering messages to the request about to be sent.
    midrun_steer: "Callable[[list[Any]], Awaitable[list[Any]]] | None" = None
    agent: "AgentSpec | None" = None  # set while running as an agent
    agents: "dict[str, AgentSpec]" = field(default_factory=dict)  # what this session can start; empty in an agent
    # Set by wiring, which can build agents: (spec, task, usage to add to, model, requests to leave unused, number of
    # agents started together, which share the requests left).
    spawn_agent: "Callable[..., Awaitable[AgentResult]] | None" = None
    agent_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # held by an agent run that can change things
    # Set by wiring from the frontend's ask_questions: asks the user questions. None: nobody can answer.
    ask_user: "Callable[[list[Question]], Awaitable[Answers]] | None" = None
    # Set by wiring when the frontend can display images: shows (file, path as given, media type, caption).
    show_image: "Callable[[Path, str, str, str], Awaitable[None]] | None" = None
    compact_requested: str | None = None  # set by the compact tool: compact before the next request, with this focus
    no_questions: str | None = None  # a skill marked `interactive: false` in use this turn: ask_user is hidden
    # Set by wiring: reloads agents if their files changed. Returns warnings, or None if nothing changed.
    reload_agents: "Callable[..., list[str] | None] | None" = None  # force=True: reload even if unchanged
    # Set by wiring: reloads skills and their /commands if any SKILL.md changed, or always with force=True.
    reload_skills: "Callable[..., Awaitable[None]] | None" = None
    lsp: "LspManager | None" = None  # set by wiring when lsp = true; agents share it
    # shell(background=true) processes by id, kept after they exit so their output can be read; agents share it
    processes: "dict[str, BackgroundProcess]" = field(default_factory=dict)

    @property
    def can_use_lsp(self) -> bool:
        """Whether the lsp_* tools are shown: a server was found, and an agent's tool list doesn't leave them out."""
        if self.lsp is None or not self.lsp.available:
            return False
        return self.agent is None or self.agent.tools is None or any(t.startswith("lsp_") for t in self.agent.tools)

    @property
    def can_load_skills(self) -> bool:
        return self.agent is None or self.agent.tools is None or "load_skill" in self.agent.tools

    @property
    def can_ask(self) -> bool:
        """Whether the model may ask the user questions: a frontend can ask, and no agent or quiet skill runs."""
        return self.ask_user is not None and self.agent is None and self.no_questions is None

    def write_roots(self) -> list[Path]:
        """Directories outside the workspace that file tools may change: the scratch directory."""
        return [self.scratch] if self.scratch else []

    def read_roots(self) -> list[Path]:
        """Directories outside the workspace that read-only file tools may access: the scratch directory and
        loaded skills' folders."""
        return [*self.write_roots(), *(s.path.parent for s in self.skills.values()), *self.read_paths]

    def readable(self, path: str) -> Path:
        """`path` resolved for a read-only file tool. Raises PathEscape."""
        return self._resolve(path, self.read_roots())

    def writable(self, path: str) -> Path:
        """`path` resolved for a file tool that changes it. Raises PathEscape."""
        p = self._resolve(path, self.write_roots())
        if p.is_relative_to(ws := self.workspace.resolve()) and ".git" in p.relative_to(ws).parts:
            raise PathEscape(f"{path!r} is in .git, which file tools never change")
        if hit := protected_match(p, ws, self.protected):
            if ".git" in Path(hit).parts:  # a nested repository: never, whatever protected_write says
                raise PathEscape(f"{hit} is a git folder, which file tools never change")
            if self.protected_write != "ask":
                raise PathEscape(f"{hit} is protected (protected_paths): it can run code when the project is used "
                                 "later. Ask the user to change it")
        return p

    FILE_CHANGES = ("write_file", "edit_file", "delete_file", "move_file")

    def _protected_in(self, tool: str, args: dict) -> str | None:
        """The protected file a file tool call changes, for the prompt (protected_write = "ask")."""
        if tool not in self.FILE_CHANGES or not self.protected:
            return None
        ws = self.workspace.resolve()
        for key in ("path", "destination"):
            if isinstance(v := args.get(key), str):
                p = Path(os.path.normpath(ws / os.path.expanduser(v)))
                if hit := protected_match(p, ws, self.protected):
                    return hit
        return None

    def _resolve(self, path: str, roots: list[Path]) -> Path:
        try:
            return resolve_in_roots(self.workspace, path, roots)
        except PathEscape as e:
            msg = str(e)
            if self.scratch:  # models reach for /tmp; say where temporary files go instead
                msg += f"; use the scratch directory {self.scratch} for temporary files"
            raise PathEscape(msg + self.went_outside()) from None

    def went_outside(self) -> str:
        """Count a try to reach outside the workspace. The third in a run returns a notice to add to the tool
        result; otherwise ""."""
        from .outside import STAY_INSIDE
        self.outside_attempts += 1
        return f"\n[jimbo: {STAY_INSIDE}]" if self.outside_attempts == 3 else ""

    def is_secret(self, path: str) -> bool:
        return any(fnmatchcase(Path(path).name, p) for p in self.secret_files)

    async def authorize(self, call_id: str | None, tool: str, args: dict, summary: str, *,
                        detail: str | None = None, read_only: bool = False, wording: dict | None = None,
                        proposed: dict | None = None) -> None:
        """Single choke point for tools. Raises ToolDenied if no policy allows the call. `call_id` ties the
        decision to the tool_call event, under the id ForwardSink gives an agent's calls. `wording` words the
        prompt, if one is shown (see ApprovalRequest)."""
        if self.agent:
            summary = f"[{self.agent.name}] {summary}"
            call_id = f"{self.agent.name}.{call_id}"
        from .redact import redact
        summary, detail = redact(summary), detail and redact(detail)
        v = await self.gate.check(ToolCallInfo(call_id=call_id, tool=tool, args=args, summary=summary, detail=detail,
                                               read_only=read_only, workspace=self.workspace,
                                               wording=wording or {}, proposed=proposed,
                                               protected=self._protected_in(tool, args)))
        if v.action != "allow":
            raise ToolDenied(v.reason)
