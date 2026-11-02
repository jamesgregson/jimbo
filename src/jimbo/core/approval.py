"""Approval gate. Policy (what needs a human) is separate from UI (how a human is asked)."""

import asyncio
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol

from pydantic import BaseModel


class ToolCallInfo(BaseModel):
    call_id: str | None = None  # the id on the tool_call event
    tool: str
    args: dict
    summary: str
    detail: str | None = None
    read_only: bool = False
    workspace: Path
    assessment: dict | None = None  # advisory summary/risk, filled in by an Assessor


class Verdict(BaseModel):
    action: Literal["allow", "deny", "ask"]
    reason: str | None = None
    decided_by: str


class ApprovalRequest(BaseModel):
    id: str
    call_id: str | None = None  # the id on the tool_call event
    tool: str
    summary: str
    detail: str | None = None
    assessment: dict | None = None
    always: str = "this session"  # what answering "always" covers, for the prompt


class ApprovalDecision(BaseModel):
    allow: bool
    remember: Literal["no", "session"] = "no"
    note: str | None = None


def parse_answer(text: str) -> ApprovalDecision | None:
    """What a line typed at an approval prompt means. None: denied, ask why.

    Only exact answers approve. Anything else denies and becomes the reason, so a sentence ("actually, no")
    or a half-typed steering message can never allow a call, and the model still gets to read it.
    """
    answer = text.strip()
    match answer.lower():
        case "y" | "yes":
            return ApprovalDecision(allow=True)
        case "a" | "always":
            return ApprovalDecision(allow=True, remember="session")
        case "" | "n" | "no":
            return None
    return ApprovalDecision(allow=False, note=answer)


class ApprovalPolicy(Protocol):
    name: str

    async def evaluate(self, call: ToolCallInfo) -> Verdict: ...


class ReadOnlyAllow:
    name = "readonly"

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        if call.read_only:
            return Verdict(action="allow", decided_by=self.name)
        return Verdict(action="ask", decided_by=self.name)


class PlanMode:
    """Head of the chain while planning: read-only calls fall through to the normal policies, everything
    else is denied. `exit_plan` is decided here by asking the user, so yolo/remembered can't skip it."""

    name = "plan"

    def __init__(self, active: Callable[[], bool],
                 ask: Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]) -> None:
        self._active = active
        self._ask = ask
        self._n = 0

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        if not self._active():
            return Verdict(action="ask", decided_by=self.name)
        if call.tool == "exit_plan":
            self._n += 1
            d = await self._ask(ApprovalRequest(id=f"plan-{self._n}", call_id=call.call_id, tool=call.tool,
                                                summary="Approve this plan and start executing?",
                                                detail=call.detail))
            return Verdict(action="allow" if d.allow else "deny",
                           reason=None if d.allow else d.note or "plan rejected; revise it or ask what to change",
                           decided_by="user")
        if call.read_only:
            return Verdict(action="ask", decided_by=self.name)
        return Verdict(action="deny", reason="plan mode: present a plan instead (call exit_plan)",
                       decided_by=self.name)


class RememberedDecisions:
    """Session-scoped 'always allow'. Fed by AskUser when the user picks remember. Shell is remembered per
    exact command, because allowing one command must not allow every other one; other tools as a whole."""

    name = "remembered"

    def __init__(self) -> None:
        self._allowed: set[tuple[str, str]] = set()

    @staticmethod
    def _key(call: ToolCallInfo) -> tuple[str, str]:
        return (call.tool, str(call.args.get("command", "")).strip() if call.tool == "shell" else "")

    @staticmethod
    def scope(call: ToolCallInfo) -> str:
        return "this exact command" if call.tool == "shell" else f"{call.tool} this session"

    def add(self, call: ToolCallInfo) -> None:
        self._allowed.add(self._key(call))

    def allows(self, call: ToolCallInfo) -> bool:
        return self._key(call) in self._allowed

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        if self.allows(call):
            return Verdict(action="allow", decided_by=self.name, reason="remembered for session")
        return Verdict(action="ask", decided_by=self.name)


_SHELL_SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")
_SHELL_META = re.compile(r"[;&|<>`$()\n]")


class PatternRules:
    """Allow/deny rules like "shell:git status*", "write_file:docs/*" or "read_file:.env*". Deny wins.

    The part after ':' is a glob matched against the call's first argument (shell command or path); a bare
    tool name matches every call. Paths are matched relative to the workspace, and deny rules also match the
    file name, so "read_file:.env*" covers ./.env and svc/.env. Shell allow rules never match compound
    commands (pipes, ;, &&, redirects, $...), so "shell:git status*" can't approve "git status; rm -rf ~".
    Shell deny rules match if any segment does. Deny rules are best effort, not a security boundary: use the
    sandbox for that.
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
    def _relative(path: str, workspace: Path) -> str:
        """`path` as the model wrote it (./.env, an absolute path) in one spelling, so rules can't be sidestepped."""
        return Path(os.path.relpath(os.path.join(workspace, path), workspace)).as_posix()

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
        elif next(iter(call.args), None) == "path":
            allowable = [self._relative(subject, call.workspace)]
            segments = [*allowable, Path(subject).name]
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
        assess: Callable[[ToolCallInfo], Awaitable[None]] | None = None,
    ) -> None:
        self._ask = ask
        self._assess = assess  # sets call.assessment; only runs when a human is about to be asked
        self._remembered = remembered
        self._n = 0
        self._lock = asyncio.Lock()  # parallel tool calls must not prompt the user concurrently

    async def evaluate(self, call: ToolCallInfo) -> Verdict:
        async with self._lock:
            if self._remembered is not None and self._remembered.allows(call):
                return Verdict(action="allow", decided_by="remembered", reason="remembered for session")
            self._n += 1
            if self._assess is not None and call.assessment is None:
                await self._assess(call)
            d = await self._ask(ApprovalRequest(id=str(self._n), call_id=call.call_id, tool=call.tool,
                                                summary=call.summary, detail=call.detail, assessment=call.assessment,
                                                always=RememberedDecisions.scope(call)))
            if d.allow and d.remember == "session" and self._remembered is not None:
                self._remembered.add(call)
            reason = None if d.allow else f"User denied this action: {d.note}" if d.note else None
            return Verdict(action="allow" if d.allow else "deny", reason=reason, decided_by="user")


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
