"""check-task: the check must fail on the unchanged fixture and pass with solution/ applied."""

import shutil
import tempfile
from pathlib import Path

from .grading import copy_tree, run_check
from .tasks import Task


async def check_task(task: Task) -> list[str]:
    """Problems with `task`; empty when it is fine. Tasks without a check command are only parsed."""
    if not task.check.command:
        return []
    problems = []
    for applied in (False, True):
        if applied and not task.solution.is_dir():
            break
        tmp = Path(tempfile.mkdtemp(prefix=f"jimbo-check-{task.id}-"))
        try:
            ws = tmp / "ws"
            shutil.copytree(task.repo, ws)
            if applied:
                copy_tree(task.solution, ws)
            out = await run_check(task, ws)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if out.ok != applied:
            what = "passes on the unchanged fixture" if out.ok else "fails with solution/ applied"
            problems.append(f"the check {what}:\n{out.text}")
    return problems
