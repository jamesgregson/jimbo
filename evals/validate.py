"""check-task: the check must fail on the unchanged fixture and pass with solution/ applied."""

import shutil
import tempfile
from functools import cache
from pathlib import Path

from jimbo.tools import container_sandbox
from jimbo.tools.sandbox import Sandbox, SeatbeltSandbox, default_writable

from .grading import copy_tree, run_check
from .tasks import Task


@cache
def _runtime(cli: str) -> container_sandbox.Runtime:
    return container_sandbox.probe(cli)


def make_sandbox(mode: str, ws: Path) -> Sandbox | None:
    """The sandbox a trial with `--sandbox mode` runs its check in."""
    if mode == "seatbelt":
        return SeatbeltSandbox(default_writable(ws))
    if mode in ("docker", "podman"):
        return container_sandbox.ContainerSandbox(container_sandbox.bundled_image(), ws, runtime=_runtime(mode))
    return None


async def check_task(task: Task, sandbox: str = "off") -> list[str]:
    """Problems with `task`; empty when it is fine. Tasks without a check command are only parsed."""
    if not task.check.command:
        return []
    problems = []
    for applied in (False, True):
        if applied and not task.solution.is_dir():
            break
        tmp = Path(tempfile.mkdtemp(prefix=f"jimbo-check-{task.id}-")).resolve()
        box = None
        try:
            ws = tmp / "ws"
            shutil.copytree(task.repo, ws)
            if applied:
                copy_tree(task.solution, ws)
            box = make_sandbox(sandbox, ws)
            out = await run_check(task, ws, box)
        finally:
            if box:
                await box.close()
            shutil.rmtree(tmp, ignore_errors=True)
        if out.ok != applied:
            what = "passes on the unchanged fixture" if out.ok else "fails with solution/ applied"
            problems.append(f"the check {what}:\n{out.text}")
    return problems
