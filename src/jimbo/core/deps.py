from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .approval import ApprovalGate, ToolCallInfo

if TYPE_CHECKING:
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
    todos: "list[TodoItem]" = field(default_factory=list)

    async def authorize(self, tool: str, args: dict, summary: str, *, detail: str | None = None,
                        read_only: bool = False) -> None:
        """Single choke point for tools. Raises ToolDenied if no policy allows the call."""
        v = await self.gate.check(ToolCallInfo(tool=tool, args=args, summary=summary, detail=detail,
                                               read_only=read_only, workspace=self.workspace))
        if v.action != "allow":
            raise ToolDenied(v.reason)
