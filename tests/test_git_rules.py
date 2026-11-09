import pytest
from pydantic_ai import models

from helpers import settings
from jimbo.core.git_rules import find
from jimbo.tools.paths import PathEscape
from jimbo.wiring import build_session
from test_assess import Recording, resolved, shell_agent

models.ALLOW_MODEL_REQUESTS = False


def kinds(command, ws):
    return [f.kind for f in find(command, ws)]


@pytest.mark.parametrize("command", [
    "git status", "git diff --cached", "git log --oneline -5 | head", "git show HEAD:a.py", "git blame x.py",
    "git grep foo", "git branch", "git branch -a", "git branch --list 'f*'", "git branch --show-current", "git tag",
    "git tag -l 'v*'", "git stash list", "git stash show -p", "git config user.name", "git config --get a.b",
    "git remote -v", "git remote get-url origin", "git rev-parse HEAD", "git -C sub log", "git --no-pager diff",
    "git branch 2>/dev/null", "git log 2>&1 | tail", "FOO=1 git status", "echo $(git rev-parse HEAD)",
    "gh pr view 3", "gh pr list", "gh api repos/a/b", "gh run view 1", "git --version",
])
def test_reads(tmp_path, command):
    assert set(kinds(command, tmp_path)) == {"read"}


@pytest.mark.parametrize("command", [
    "git commit -am x", "git -C sub commit -m x", "git add -A", "git checkout -- .", "git restore .", "git stash",
    "git stash pop", "git branch feat", "git branch -D feat", "git tag v1", "git tag -a v1 -m x", "git reset --hard",
    "git rebase main", "git switch main", "git merge x", "git config user.name bob", "git init", "git fetch",
    "git pull", "git clone url", "git worktree add ../x", "gh pr checkout 3", "git -c x=y commit -m z",
])
def test_writes(tmp_path, command):
    assert "write" in kinds(command, tmp_path)


@pytest.mark.parametrize("command", [
    "git push", "git push -f origin main", "git remote add o url", "git remote set-url origin x", "git send-email x",
    "git lfs push origin", "gh pr create --fill", "gh pr merge 3", "gh release create v1", "gh api -X POST repos/a",
    "gh api repos/a/issues -f title=x", "gh auth token", "gh repo delete x",
])
def test_remote_changes(tmp_path, command):
    assert "remote" in kinds(command, tmp_path)


@pytest.mark.parametrize("command", [
    'pytest -q && git add -A && git commit -qm "fix && more; done"',
    'bash -c "git commit -am x"', "sh -lc 'cd sub && git commit -m x'", "eval git commit -m x",
    "sudo git commit -m x", "env -i git commit -m x", "timeout 30 git commit -m x", "make; git commit -m x",
    "(git commit -m x)", "true || git commit -m x", "x=$(git commit -m x)", "echo `git commit -m x`",
    "git commit -m 'unbalanced",
])
def test_writes_are_found_inside_longer_commands(tmp_path, command):
    assert "write" in kinds(command, tmp_path)


def test_text_that_only_mentions_git_is_not_a_git_command(tmp_path):
    assert kinds("echo git commit && grep 'git push' notes.md", tmp_path) == []


def test_unknown_subcommands_and_aliases(tmp_path):
    assert kinds("git st", tmp_path) == ["unknown"]


def test_cd_and_dash_c_set_where_git_runs(tmp_path):
    (tmp_path / "a").mkdir()
    assert find("cd a && git commit -m x", tmp_path)[0].cwd == (tmp_path / "a").resolve()
    assert find("git -C a commit -m x", tmp_path)[0].cwd == (tmp_path / "a").resolve()


def build(tmp_path, command, **kw):
    fe = Recording()
    return build_session(settings(tmp_path, **kw), fe, model=shell_agent(command)), fe


@pytest.mark.parametrize("approval", [None, ["rules", "yolo"], ["rules", "readonly", "remembered", "auto", "ask"]])
async def test_commit_is_denied_without_asking_in_every_mode(tmp_path, approval):
    s, fe = build(tmp_path, "pytest -q && git add -A && git commit -m x",
                  **({"approval": approval} if approval else {}))
    await s.send("go")
    assert fe.requests == []
    r = resolved(fe)[0]
    assert r["action"] == "deny" and r["decided_by"] == "git" and "git is read-only" in r["reason"]


async def test_switching_to_yolo_keeps_the_git_rules(tmp_path):
    s, fe = build(tmp_path, "git push origin main")
    s.approval_modes.set("yolo")
    await s.send("go")
    r = resolved(fe)[0]
    assert r["action"] == "deny" and "never changes a remote" in r["reason"]


async def test_push_is_denied_even_with_git_write(tmp_path):
    s, fe = build(tmp_path, "git commit -qam x && git push", git_write=True, approval=["rules", "yolo"])
    await s.send("go")
    assert fe.requests == [] and resolved(fe)[0]["action"] == "deny"


async def test_git_write_asks_each_time_without_always_even_in_yolo(tmp_path):
    s, fe = build(tmp_path, "make && git commit -am x", git_write=True, approval=["rules", "yolo"])
    await s.send("go")
    [req] = fe.requests
    assert req.always is None and req.detail.startswith("This command changes git: git commit -am x")
    assert resolved(fe)[0]["decided_by"] == "user"


async def test_read_only_git_still_goes_through_the_normal_chain(tmp_path):
    s, fe = build(tmp_path, "git status")
    await s.send("go")
    assert len(fe.requests) == 1 and fe.requests[0].always == "this exact command"


async def test_a_repository_in_the_scratch_directory_can_be_changed(tmp_path):
    from jimbo.config import Settings
    from jimbo.core.approval import ToolCallInfo
    (tmp_path / "ws").mkdir()
    s = build_session(Settings(workspace=tmp_path / "ws", user_dir=tmp_path / "user", bundled=False, lsp=False),
                      Recording(), model=shell_agent("x"))
    scratch = s.deps.scratch
    git = next(p for p in s.deps.gate.policies if p.name == "git")
    call = ToolCallInfo(tool="shell", args={"command": f"cd {scratch} && git init && git commit -m x"},
                        summary="", workspace=s.deps.workspace)
    assert (await git.evaluate(call)).action == "ask"  # falls through to the normal chain
    call.args["command"] = f"cd {scratch} && git push"
    assert (await git.evaluate(call)).action == "deny"


def test_file_tools_never_write_into_dot_git(tmp_path):
    s, _ = build(tmp_path, "x")
    with pytest.raises(PathEscape):
        s.deps.writable(".git/hooks/pre-commit")
    with pytest.raises(PathEscape):
        s.deps.writable("sub/.git/config")
    assert s.deps.writable(".gitignore") == (tmp_path / ".gitignore").resolve()


def test_system_prompt_says_git_is_read_only(tmp_path):
    from types import SimpleNamespace
    from jimbo.agent import SYSTEM_PROMPT, project_context
    s, _ = build(tmp_path, "x")
    assert "Don't commit" in SYSTEM_PROMPT
    assert "git is read-only" in project_context(SimpleNamespace(deps=s.deps))
    s.deps.git_write = True
    assert "git is read-only" not in project_context(SimpleNamespace(deps=s.deps))


def _repo(ws):
    import subprocess
    run = lambda *a: subprocess.run(["git", *a], cwd=ws, check=True, capture_output=True)
    run("init", "-q")
    (ws / "app.py").write_text("x = 1\n")
    run("add", "app.py")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
    return run


def test_scan_commit_finds_staged_secrets(tmp_path):
    from jimbo.core.git_rules import scan_commit
    run = _repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\nkey = 'AKIAIOSFODNN7EXAMPLE'\n")
    (tmp_path / "prod.pem").write_text("cert\n")
    run("add", "app.py", "prod.pem")
    assert scan_commit(tmp_path, ["*.pem"], everything=False) == ["prod.pem (secret file)", "app.py:2 (aws access key)"]


def test_scan_commit_with_add_or_dash_a_looks_at_unstaged_and_untracked(tmp_path):
    from jimbo.core.git_rules import scan_commit
    _repo(tmp_path)
    (tmp_path / "app.py").write_text("token = 'ghp_" + "a1B2" * 9 + "'\n")
    (tmp_path / ".env").write_text("A=1\n")
    assert scan_commit(tmp_path, [".env"], everything=False) == []
    assert scan_commit(tmp_path, [".env"], everything=True) == [".env (secret file)", "app.py:1 (github token)"]


async def test_git_write_prompt_warns_about_secrets_in_the_commit(tmp_path):
    _repo(tmp_path)
    (tmp_path / "app.py").write_text("k = 'AKIAIOSFODNN7EXAMPLE'\n")
    s, fe = build(tmp_path, "git add -A && git commit -m wip", git_write=True)
    await s.send("go")
    [req] = fe.requests
    assert req.detail.startswith("Staged changes contain a secret: app.py:1 (aws access key)")
    assert "This command changes git: git add -A" in req.detail


async def test_git_write_prompt_without_secrets_has_no_warning(tmp_path):
    _repo(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n")
    s, fe = build(tmp_path, "git commit -am wip", git_write=True)
    await s.send("go")
    assert fe.requests[0].detail.startswith("This command changes git: git commit -am wip")
