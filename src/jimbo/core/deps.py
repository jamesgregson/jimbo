from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from .approval import ApprovalGate, ToolCallInfo
from .checkpoints import Checkpoints

if TYPE_CHECKING:
    from .agents import AgentResult, AgentSpec
    from .models import ModelRouter
    from .skills import Skill
    from ..tools.sandbox import Sandbox
    from ..tools.todos import TodoItem


class ToolDenied(Exception):
    def __init__(self, reason: str | None):
        super().__init__(reason or "denied")
        self.reason = reason


@dataclass
class Deps:
    workspace: Path
    gate: ApprovalGate
    sandbox: "Sandbox | None" = None
    shell_timeout: int = 120
    shell_timeout_max: int = 600
    models: "ModelRouter | None" = None
    todos: "list[TodoItem]" = field(default_factory=list)
    mode: Literal["normal", "plan"] = "normal"
    project_instructions: str = ""
    skills: "dict[str, Skill]" = field(default_factory=dict)
    secret_files: list[str] = field(default_factory=list)  # file-name globs the model must not read
    checkpoints: Checkpoints = field(default_factory=Checkpoints)
    request_limit: int | None = None  # set by the session: model requests allowed per turn
    wrap_up_left: int | None = None  # set by the prompt when it tells the model to wrap up; the session logs it
    # Set by the session: compacts a message list mid-run when the context is nearly full.
    midrun_compact: "Callable[[list[Any]], Awaitable[list[Any]]] | None" = None
    # Set by the session: appends queued steering messages to the request about to be sent.
    midrun_steer: "Callable[[list[Any]], Awaitable[list[Any]]] | None" = None
    agent: "AgentSpec | None" = None  # set while running as an agent
    agents: "dict[str, AgentSpec]" = field(default_factory=dict)  # what this session can start; empty in an agent
    # Set by wiring, which can build agents: (spec, task, usage to add to, model, requests to leave unused).
    spawn_agent: "Callable[..., Awaitable[AgentResult]] | None" = None
    # Set by wiring: reloads agents if their files changed. Returns warnings, or None if nothing changed.
    reload_agents: "Callable[[], list[str] | None] | None" = None

    @property
    def can_load_skills(self) -> bool:
        return self.agent is None or self.agent.tools is None or "load_skill" in self.agent.tools

    def read_roots(self) -> list[Path]:
        """Directories outside the workspace that read-only file tools may access: loaded skills' folders."""
        return [s.path.parent for s in self.skills.values()]

    def is_secret(self, path: str) -> bool:
        return any(fnmatchcase(Path(path).name, p) for p in self.secret_files)

    async def authorize(self, call_id: str | None, tool: str, args: dict, summary: str, *,
                        detail: str | None = None, read_only: bool = False) -> None:
        """Single choke point for tools. Raises ToolDenied if no policy allows the call. `call_id` ties the
        decision to the tool_call event, under the id ForwardSink gives an agent's calls."""
        if self.agent:
            summary = f"[{self.agent.name}] {summary}"
            call_id = f"{self.agent.name}.{call_id}"
        v = await self.gate.check(ToolCallInfo(call_id=call_id, tool=tool, args=args, summary=summary, detail=detail,
                                               read_only=read_only, workspace=self.workspace))
        if v.action != "allow":
            raise ToolDenied(v.reason)
