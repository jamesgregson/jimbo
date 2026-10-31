"""Approval gate. Policy (what needs a human) is separate from UI (how a human is asked)."""

import asyncio
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol

from pydantic import BaseModel


class ToolCallInfo(BaseModel):
    tool: str
    args: dict
    summary: str
    detail: str | None = None
    read_only: bool = False
    workspace: Path


class Verdict(BaseModel):
    action: Literal["allow", "deny", "ask"]
    reason: str | None = None
    decided_by: str


class ApprovalRequest(BaseModel):
    id: str
    tool: str
    summary: str
    detail: str | None = None


class ApprovalDecision(BaseModel):
    allow: bool
    remember: Literal["no", "session"] = "no"
    note: str | None = None


class ApprovalPolicy(Protocol):
    name: str

    async def evaluate(self, call: ToolCallInfo) -> Verdict: ...


class ReadOnlyAllow:
    name = "readonly"

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        if call.read_only:
            return Verdict(action="allow", decided_by=self.name)
        return Verdict(action="ask", decided_by=self.name)


class RememberedDecisions:
    """Session-scoped 'always allow this tool'. Fed by AskUser when the user picks remember."""

    name = "remembered"

    def __init__(self) -> None:
        self.allowed_tools: set[str] = set()

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        if call.tool in self.allowed_tools:
            return Verdict(action="allow", decided_by=self.name, reason="remembered for session")
        return Verdict(action="ask", decided_by=self.name)


_SHELL_SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")
_SHELL_META = re.compile(r"[;&|<>`$()\n]")


class PatternRules:
    """Allow/deny rules like "shell:git status*", "write_file:docs/*" or "read_file:.env*". Deny wins.

    The part after ':' is a glob matched against the call's first argument (shell command or path); a bare
    tool name matches every call. Shell allow rules never match compound commands (pipes, ;, &&,
    redirects, $...), so "shell:git status*" can't approve "git status; rm -rf ~". Shell deny rules match
    if any segment does. Deny rules are best effort, not a security boundary: use the sandbox for that.
    """

    name = "rules"

    def __init__(self, allow: list[str], deny: list[str]) -> None:
        self.allow = [self._parse(r) for r in allow]
        self.deny = [self._parse(r) for r in deny]

    @staticmethod
    def _parse(rule: str) -> tuple[str, str]:
        tool, _, pattern = rule.partition(":")
        return tool.strip(), pattern.strip() or "*"

    @staticmethod
    def _match(rules: list[tuple[str, str]], tool: str, subjects: list[str]) -> str | None:
        for t, p in rules:
            if fnmatchcase(tool, t) and any(fnmatchcase(s, p) for s in subjects):
                return f"{t}:{p}"
        return None

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        subject = str(next(iter(call.args.values()), "")).strip()
        if call.tool == "shell":
            segments = [s.strip() for s in _SHELL_SEGMENTS.split(subject) if s.strip()] or [subject]
            allowable = [] if _SHELL_META.search(subject) else [subject]
        else:
            segments = allowable = [subject]
        if rule := self._match(self.deny, call.tool, segments):
            return Verdict(action="deny", reason=f"denied by rule {rule!r}", decided_by=self.name)
        if rule := self._match(self.allow, call.tool, allowable):
            return Verdict(action="allow", reason=f"allowed by rule {rule!r}", decided_by=self.name)
        return Verdict(action="ask", decided_by=self.name)


class Yolo:
    name = "yolo"

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        return Verdict(action="allow", decided_by=self.name)


class AskUser:
    """Terminal policy: delegates to the frontend. Add smarter policies (e.g. an LLM guard) before it."""

    name = "ask"

    def __init__(
        self,
        ask: Callable[[ApprovalRequest], Awaitable[ApprovalDecision]],
        remembered: RememberedDecisions | None = None,
    ) -> None:
        self._ask = ask
        self._remembered = remembered
        self._n = 0
        self._lock = asyncio.Lock()  # parallel tool calls must not prompt the user concurrently

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        async with self._lock:
            if self._remembered is not None and call.tool in self._remembered.allowed_tools:
                return Verdict(action="allow", decided_by="remembered", reason="remembered for session")
            self._n += 1
            d = await self._ask(
                ApprovalRequest(id=str(self._n), tool=call.tool, summary=call.summary, detail=call.detail)
            )
            if d.allow and d.remember == "session" and self._remembered is not None:
                self._remembered.allowed_tools.add(call.tool)
            return Verdict(action="allow" if d.allow else "deny", reason=d.note, decided_by="user")


class ApprovalGate:
    """Runs the policy chain; the first non-'ask' verdict wins. Unresolved means deny (fail closed)."""

    def __init__(self, policies: list[ApprovalPolicy],
                 observer: Callable[[ToolCallInfo, Verdict], Awaitable[None]] | None = None) -> None:
        self.policies = policies
        self.observer = observer  # lets the session emit approval_resolved events

    async def check(self, call: ToolCallInfo) -> Verdict:
        verdict = Verdict(action="deny", reason="no policy approved", decided_by="gate")
        for p in self.policies:
            v = await p.evaluate(call)
            if v.action != "ask":
                verdict = v
                break
        if self.observer:
            await self.observer(call, verdict)
        return verdict
