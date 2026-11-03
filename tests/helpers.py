"""Shared test doubles (tests/ is on sys.path under pytest's default rootdir import mode)."""

from jimbo.config import Settings
from jimbo.core.approval import ApprovalDecision


def settings(tmp_path, **kw):
    kw.setdefault("bundled", False)
    return Settings(workspace=tmp_path, user_dir=tmp_path / "user", **kw)


class ScriptedFrontend:
    def __init__(self, inputs=(), allow=True):
        self.inputs, self.allow, self.events = list(inputs), allow, []

    async def next_user_input(self):
        return self.inputs.pop(0) if self.inputs else None

    async def render(self, event):
        self.events.append(event)

    async def request_approval(self, req):
        return ApprovalDecision(allow=self.allow)

