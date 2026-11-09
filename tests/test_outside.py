from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry

from jimbo.core.approval import ApprovalGate, AskUser, ApprovalDecision, Yolo
from jimbo.core.assess import check_command
from jimbo.core.deps import Deps
from jimbo.core.outside import STAY_INSIDE, blocked_notes, dependency_dirs, outside_paths
from jimbo.tools.fs import read_file
from jimbo.tools.shell import shell


def reach(command, ws, roots=()):
    return [str(r) for r in outside_paths(command, ws, list(roots))]


@pytest.mark.parametrize("command,expected", [
    ("find ~ -name '*.toml'", ["~ (read)"]),
    ("cat ~/.aws/credentials", ["~/.aws/credentials (read)"]),
    ("cat $HOME/.netrc", ["$HOME/.netrc (read)"]),
    ("python x.py > /tmp/out.txt", ["/tmp/out.txt (write)"]),
    ("cp a.txt /etc/x", ["/etc/x (write)"]),
    ("rm -rf /tmp/build", ["/tmp/build (write)"]),
    ("sed -i s/a/b/ ~/.zshrc", ["~/.zshrc (write)"]),
    ("cat ../other/README.md", ["../other/README.md (read)"]),
    ("uv run --directory=/opt/x foo", ["/opt/x (read)"]),
    ("ls /usr/include && /usr/bin/python3 -c 1", []),
    ("pytest -q 2>&1 | tail > /dev/null", []),
    ("cat src/a.py ./b.py", []),
])
def test_outside_paths(tmp_path, command, expected):
    assert reach(command, tmp_path) == expected


def test_roots_count_as_inside(tmp_path):
    scratch = tmp_path.parent / "scratch-x"
    assert reach(f"echo hi > {scratch}/out.txt", tmp_path, [scratch]) == []


def test_check_command_rates_outside_reads_medium_and_writes_high(tmp_path):
    assert check_command("cat ~/.aws/credentials", tmp_path) == ("medium", ["reads outside the workspace "
                                                                            "(~/.aws/credentials)"])
    assert check_command("echo x > /tmp/x", tmp_path)[0] == "high"


def test_blocked_notes(tmp_path):
    ws, scratch = tmp_path / "ws", tmp_path / "scratch"
    out = ("cat: /Users/x/.ssh/config: Operation not permitted\n"
           "bash: /tmp/out.txt: Operation not permitted\n"
           f"touch: {ws}/.git/index.lock: Read-only file system\n"
           f"cp: {ws}/build/x: Permission denied\n")
    notes = blocked_notes(out, ws, scratch)
    assert notes[0].startswith("[jimbo: the sandbox blocks reading /Users/x/.ssh/config.")
    assert notes[1] == f"[jimbo: the sandbox blocks writing /tmp/out.txt. Write temporary files to the scratch " \
                       f"directory {scratch}.]"
    assert "git is read-only" in notes[2] and len(notes) == 3  # a permission problem in the project is no note
    assert "writing /etc/x" in blocked_notes("touch: /etc/x: Permission denied", ws, None, writes={Path("/etc/x")})[0]
    assert blocked_notes("all fine", ws, scratch) == []


class Asked:
    def __init__(self, allow):
        self.allow, self.requests = allow, []

    async def __call__(self, req):
        self.requests.append(req)
        return ApprovalDecision(allow=self.allow)


def ctx(tmp_path, allow=True):
    asked = Asked(allow)
    d = Deps(workspace=tmp_path.resolve(), gate=ApprovalGate([AskUser(asked)]), scratch=tmp_path / "s")
    return SimpleNamespace(tool_call_id=None, deps=d), asked


async def test_approval_names_paths_outside_the_workspace(tmp_path):
    c, asked = ctx(tmp_path)
    await shell(c, "ls ~/Downloads")
    assert "Reaches outside the workspace: ~/Downloads (read)" in asked.requests[0].detail
    await shell(c, "ls src")
    assert "Reaches outside" not in asked.requests[1].detail


async def test_third_outside_try_in_a_run_tells_the_model_to_stay_inside(tmp_path):
    c, _ = ctx(tmp_path, allow=False)
    messages = []
    for _ in range(4):
        with pytest.raises(ModelRetry) as e:
            await shell(c, "cat ~/.zshrc")
        messages.append(str(e.value))
    assert [STAY_INSIDE in m for m in messages] == [False, False, True, False]
    c.deps.outside_attempts = 2
    with pytest.raises(ModelRetry) as e:
        await read_file(SimpleNamespace(tool_call_id=None, deps=Deps(workspace=tmp_path, gate=ApprovalGate([Yolo()]),
                                                                     outside_attempts=2)), "/etc/hosts")
    assert STAY_INSIDE in str(e.value)


def test_system_prompt_says_to_stay_inside():
    from jimbo.agent import SYSTEM_PROMPT
    assert "Work only inside the workspace and the scratch directory" in SYSTEM_PROMPT


def test_dependency_dirs(tmp_path):
    venv, home, ws = tmp_path / "venv", tmp_path / "home", tmp_path / "ws"
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (home / ".cargo" / "registry" / "src").mkdir(parents=True)
    ws.mkdir()
    (ws / ".venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    found = dependency_dirs(ws, {"HOME": str(home), "VIRTUAL_ENV": str(venv)})
    assert found == [(venv / "lib" / "python3.12" / "site-packages").resolve(),
                     (home / ".cargo" / "registry" / "src").resolve()]
    assert dependency_dirs(ws, {"HOME": str(home), "VIRTUAL_ENV": str(ws / ".venv")}) == [
        (home / ".cargo" / "registry" / "src").resolve()]  # already in the workspace


async def test_file_tools_read_library_folders(tmp_path):
    from jimbo.tools.search import glob, grep
    lib, ws = tmp_path / "lib", tmp_path / "ws"
    (lib / "pkg").mkdir(parents=True)
    (lib / "pkg" / "core.py").write_text("def frob():\n    pass\n")
    (lib / "pkg" / ".env").write_text("frob=secret\n")
    ws.mkdir()
    c = SimpleNamespace(tool_call_id=None, deps=Deps(workspace=ws.resolve(), gate=ApprovalGate([Yolo()]),
                                                     read_paths=[lib.resolve()], secret_files=[".env"]))
    assert "def frob" in await read_file(c, str(lib / "pkg" / "core.py"))
    assert await grep(c, "frob", str(lib)) == f"{lib.resolve()}/pkg/core.py:1:def frob():"
    assert await glob(c, "*.py", str(lib)) == f"{lib.resolve()}/pkg/core.py"
    with pytest.raises(ModelRetry):
        await read_file(c, str(tmp_path / "elsewhere.txt"))


def test_prompt_lists_library_folders(tmp_path):
    from jimbo.agent import project_context
    d = Deps(workspace=tmp_path, gate=ApprovalGate([]), read_paths=[Path("/lib/site-packages")])
    assert "- /lib/site-packages" in project_context(SimpleNamespace(deps=d))
