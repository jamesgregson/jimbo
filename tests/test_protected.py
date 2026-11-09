import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry

from helpers import settings
from jimbo.config import Settings
from jimbo.core.approval import ApprovalGate, Yolo
from jimbo.core.deps import Deps, protected_match
from jimbo.tools import sandbox as sb
from jimbo.tools import container_sandbox as ds
from jimbo.tools.fs import delete_file, edit_file, move_file, write_file
from jimbo.tools.shell import shell

PROTECTED = Settings.model_fields["protected_paths"].default


def ctx(ws, protected=PROTECTED):
    return SimpleNamespace(tool_call_id=None, deps=Deps(workspace=ws.resolve(), gate=ApprovalGate([Yolo()]),
                                                        protected=list(protected)))


@pytest.mark.parametrize("rel", [".github/workflows/ci.yml", ".vscode/tasks.json", ".envrc", "sub/.envrc",
                                 ".gitlab-ci.yml", ".husky/pre-commit", ".claude/settings.json",
                                 ".claude/settings.local.json", ".idea/workspace.xml"])
def test_matches(tmp_path, rel):
    assert protected_match(tmp_path / rel, tmp_path, PROTECTED) == rel


@pytest.mark.parametrize("rel", ["src/app.py", "AGENTS.md", ".jimbo/skills/x/SKILL.md", "github/ci.yml",
                                 ".claude/skills/x/SKILL.md", ".envrc.example"])
def test_does_not_match(tmp_path, rel):
    assert protected_match(tmp_path / rel, tmp_path, PROTECTED) is None


def test_a_folder_holding_a_protected_file_or_a_repository_matches(tmp_path):
    (tmp_path / "pkg" / ".github").mkdir(parents=True)
    (tmp_path / "pkg" / "x.py").write_text("")
    assert protected_match(tmp_path / "pkg", tmp_path, [".github/**"]) is None  # anchored at the root
    (tmp_path / "pkg" / ".envrc").write_text("")
    assert protected_match(tmp_path / "pkg", tmp_path, PROTECTED) == "pkg/.envrc"
    (tmp_path / "lib" / ".git").mkdir(parents=True)
    assert protected_match(tmp_path / "lib", tmp_path, PROTECTED) == "lib/.git"


async def test_file_tools_refuse_protected_paths_in_yolo(tmp_path):
    c = ctx(tmp_path)
    (tmp_path / ".envrc").write_text("export A=1\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / "README.md").write_text("hi\n")
    for call in (write_file(c, ".github/workflows/ci.yml", "x"), edit_file(c, ".envrc", "A=1", "A=2"),
                 delete_file(c, ".envrc"), move_file(c, ".envrc", "envrc.bak"), delete_file(c, ".github"),
                 move_file(c, "README.md", ".vscode/README.md")):
        with pytest.raises(ModelRetry, match="protected"):
            await call
    assert (tmp_path / ".envrc").read_text() == "export A=1\n"
    await write_file(c, "src/ok.py", "x = 1\n")
    await write_file(ctx(tmp_path, protected=[]), ".github/workflows/ci.yml", "x")  # removed from the list


def test_settings_always_keep_git_entries_and_workspaces_only_add(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    (user / "config.toml").write_text('protected_paths = [".envrc"]\n')
    ws = tmp_path / "ws"
    (ws / ".jimbo").mkdir(parents=True)
    (ws / ".jimbo" / "config.toml").write_text('protected_paths = ["deploy/**"]\n')
    monkeypatch.setenv("JIMBO_USER_DIR", str(user))
    s = Settings(workspace=ws)
    assert s.protected() == [".git/hooks/**", ".git/config", ".envrc", "deploy/**"]
    (ws / ".jimbo" / "config.toml").write_text("protected_paths = []\n")
    assert Settings(workspace=ws).protected() == [".git/hooks/**", ".git/config", ".envrc"]


def test_wiring_passes_protected_paths(tmp_path):
    from pydantic_ai.models.test import TestModel
    from helpers import ScriptedFrontend
    from jimbo.wiring import build_session
    s = build_session(settings(tmp_path), ScriptedFrontend(), model=TestModel())
    assert ".github/**" in s.deps.protected and ".git/config" in s.deps.protected


def test_path_glob_regex():
    import re
    def m(glob, rel):
        return re.fullmatch(sb._path_glob_regex(glob), rel) is not None
    assert m(".github/**", ".github/workflows/ci.yml") and not m(".github/**", "x/.github/a")
    assert m(".envrc", ".envrc") and m(".envrc", "a/b/.envrc") and not m(".envrc", ".envrc.bak")
    assert m(".claude/settings*.json", ".claude/settings.local.json")
    assert m(".vscode/**", ".vscode/tasks.json")


@pytest.mark.skipif(not sb.available(), reason="sandbox-exec is macOS only")
async def test_seatbelt_commands_cannot_write_protected_paths(tmp_path):
    ws = tmp_path.resolve()
    (ws / ".github").mkdir()
    box = sb.SeatbeltSandbox([ws], workspace=ws, git_write=True, protected=PROTECTED)
    c = SimpleNamespace(tool_call_id=None, deps=Deps(workspace=ws, gate=ApprovalGate([Yolo()]), sandbox=box))
    out = await shell(c, "touch .github/ci.yml; echo a=$?; touch sub.envrc; touch .envrc; echo b=$?; "
                         "mkdir -p x && touch x/.envrc; echo c=$?; touch ok.txt; echo d=$?")
    assert "a=1" in out and "b=1" in out and "c=1" in out and "d=0" in out
    assert "protected_paths read-only" in box.describe()


def test_container_mounts_protected_paths_read_only(tmp_path):
    ws = tmp_path.resolve()
    (ws / ".github" / "workflows").mkdir(parents=True)
    (ws / ".github" / "workflows" / "ci.yml").write_text("")
    (ws / "svc").mkdir()
    (ws / "svc" / ".envrc").write_text("")
    box = ds.ContainerSandbox("img:1", ws, protected=PROTECTED)
    joined = " ".join(box.run_argv())
    assert f"-v {ws / '.github'}:{ws / '.github'}:ro" in joined
    assert f"-v {ws / 'svc' / '.envrc'}:{ws / 'svc' / '.envrc'}:ro" in joined
    assert "ci.yml:ro" not in joined  # covered by the folder
    assert "protected_paths read-only" in box.describe()


def build_ask(tmp_path, calls, approval=None, allow=True, **kw):
    import json
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
    from helpers import ScriptedFrontend
    from jimbo.wiring import build_session
    steps = iter(calls)

    async def stream(messages, info):
        if (nxt := next(steps, None)) is not None:
            yield {0: DeltaToolCall(name=nxt[0], json_args=json.dumps(nxt[1]))}
        else:
            yield "done"

    class Fe(ScriptedFrontend):
        def __init__(self):
            super().__init__(allow=allow)
            self.asked = []

        async def request_approval(self, req):
            self.asked.append(req)
            return await super().request_approval(req)
    fe = Fe()
    s = build_session(settings(tmp_path, protected_write="ask", **({"approval": approval} if approval else {}), **kw),
                      fe, model=FunctionModel(stream_function=stream))
    return s, fe


@pytest.mark.parametrize("approval", [None, ["rules", "yolo"], ["rules", "readonly", "remembered", "auto", "ask"]])
async def test_ask_mode_asks_every_time_without_always_in_every_mode(tmp_path, approval):
    calls = [("write_file", {"path": ".github/workflows/ci.yml", "content": "on: push\n"}),
             ("write_file", {"path": ".github/workflows/ci.yml", "content": "on: pull_request\n"})]
    s, fe = build_ask(tmp_path, calls, approval)
    await s.send("add CI")
    protected = [r for r in fe.asked if "protected file" in (r.detail or "")]
    assert len(protected) == 2 and all(r.always is None for r in protected)
    assert protected[0].detail.startswith("This changes a protected file: .github/workflows/ci.yml")
    assert (tmp_path / ".github" / "workflows" / "ci.yml").read_text() == "on: pull_request\n"


async def test_ask_mode_denied_leaves_the_file_alone(tmp_path):
    s, fe = build_ask(tmp_path, [("write_file", {"path": ".envrc", "content": "x"})], ["rules", "yolo"], allow=False)
    await s.send("go")
    assert len(fe.asked) == 1 and not (tmp_path / ".envrc").exists()


async def test_ask_mode_covers_move_destination_and_folder_deletes(tmp_path):
    (tmp_path / "ci.yml").write_text("x")
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "tasks.json").write_text("{}")
    s, fe = build_ask(tmp_path, [("move_file", {"path": "ci.yml", "destination": ".github/ci.yml"}),
                                 ("delete_file", {"path": ".vscode"})], ["rules", "yolo"])
    await s.send("go")
    assert [r.detail.split("\n")[0] for r in fe.asked] == [
        "This changes a protected file: .github/ci.yml (protected_paths; it can run code when the project is used later)",
        "This changes a protected file: .vscode (protected_paths; it can run code when the project is used later)"]


async def test_ask_mode_still_refuses_dot_git_and_ordinary_files_dont_ask(tmp_path):
    (tmp_path / "lib" / ".git").mkdir(parents=True)
    s, fe = build_ask(tmp_path, [("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}),
                                 ("delete_file", {"path": "lib"}),
                                 ("write_file", {"path": "src/a.py", "content": "x"})], ["rules", "yolo"])
    await s.send("go")
    assert fe.asked == [] and (tmp_path / "lib" / ".git").exists() and (tmp_path / "src" / "a.py").exists()


def test_protected_write_is_a_user_only_setting(tmp_path):
    from jimbo.config import UNTRUSTED_KEYS
    assert "protected_write" in UNTRUSTED_KEYS
    assert Settings(workspace=tmp_path).protected_write == "deny"
