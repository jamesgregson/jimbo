from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from .approval import ApprovalGate, ToolCallInfo

if TYPE_CHECKING:
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
    # Set by the session: compacts a message list mid-run when the context is nearly full.
    midrun_compact: "Callable[[list[Any]], Awaitable[list[Any]]] | None" = None
    # Set by the session: appends queued steering messages to the request about to be sent.
    midrun_steer: "Callable[[list[Any]], Awaitable[list[Any]]] | None" = None

    def read_roots(self) -> list[Path]:
        """Directories outside the workspace that read-only file tools may access: loaded skills' folders."""
        return [s.path.parent for s in self.skills.values()]

    async def authorize(self, tool: str, args: dict, summary: str, *, detail: str | None = None,
                        read_only: bool = False) -> None:
        """Single choke point for tools. Raises ToolDenied if no policy allows the call."""
        v = await self.gate.check(ToolCallInfo(tool=tool, args=args, summary=summary, detail=detail,
                                               read_only=read_only, workspace=self.workspace))
        if v.action != "allow":
            raise ToolDenied(v.reason)
