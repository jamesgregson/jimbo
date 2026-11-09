"""Seatbelt read limits, git write limits, temp files and the command environment (sandbox.py, shell.py)."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import settings
from jimbo.core.approval import ApprovalGate, Yolo
from jimbo.core.deps import Deps
from jimbo.tools import sandbox as sb
from jimbo.tools.shell import shell

macos = pytest.mark.skipif(not sb.available(), reason="sandbox-exec is macOS only")


def ctx(ws, sandbox=None, scratch=None):
    return SimpleNamespace(tool_call_id=None, deps=Deps(workspace=ws.resolve(), gate=ApprovalGate([Yolo()]),
                                                        sandbox=sandbox, scratch=scratch))


@pytest.fixture
def world(tmp_path):
    """A fake home folder with a key, a tool folder and a workspace that is a git repository."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE")
    (home / "Documents").mkdir()
    (home / "Documents" / "taxes.txt").write_text("MONEY")
    (home / ".cargo" / "bin").mkdir(parents=True)
    (home / ".cargo" / "bin" / "tool.txt").write_text("TOOL")
    ws = home / "Code" / "proj"
    ws.mkdir(parents=True)
    scratch = tmp_path / "scratch" / "s1"
    scratch.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=ws, check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "first")
    (ws / "a.txt").write_text("A")
    (ws / ".env").write_text("SECRET=1")
    return SimpleNamespace(home=home.resolve(), ws=ws.resolve(), scratch=scratch.resolve())


def seatbelt(w, **kw):
    kw.setdefault("readable", [w.ws, w.scratch, w.home / ".cargo"])
    kw.setdefault("git_write", False)
    return sb.SeatbeltSandbox([w.ws, w.scratch], home=w.home, workspace=w.ws, **kw)


def test_profile_without_read_limits_is_unchanged():
    p = sb.SeatbeltSandbox([Path("/tmp")]).profile()
    assert "file-read" not in p and "(deny file-write*)" in p


def test_describe_names_the_limits(world):
    d = seatbelt(world).describe()
    assert d.startswith("seatbelt sandbox: reads limited to the workspace") and "git read-only" in d
    assert "hooks and config read-only" in seatbelt(world, git_write=True).describe()


def test_home_readable_includes_tool_folders_and_path_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin")
    r = sb.home_readable(tmp_path)
    assert tmp_path / ".cargo" in r and tmp_path / ".gitconfig" in r and tmp_path / "bin" in r
    assert Path("/usr/bin") not in r


def test_git_dirs_follows_a_worktree_file(tmp_path):
    main = tmp_path / "main" / ".git" / "worktrees" / "wt"
    main.mkdir(parents=True)
    (main / "commondir").write_text("../..")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}\n")
    assert sb.git_dirs(wt) == [main.resolve(), (tmp_path / "main" / ".git").resolve()]
    assert sb.git_dirs(tmp_path / "main") == [tmp_path / "main" / ".git"]


def test_default_writable_leaves_out_system_temp():
    w = sb.default_writable(Path("/w"))
    assert Path("/private/tmp") not in w and Path("/private/var/folders") not in w
    assert Path("/private/tmp") in sb.default_writable(Path("/w"), system_temp=True)


def test_build_sandbox_passes_the_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "available", lambda: True)
    s = sb.build_sandbox(settings(tmp_path, sandbox="seatbelt", git_write=True, secret_files_shell=True,
                                  sandbox_readable=[tmp_path / "extra"]), tmp_path, tmp_path / "scratch")
    assert (tmp_path / "extra").resolve() in s.readable and tmp_path.resolve() in s.readable
    assert (tmp_path / "user" / "skills").resolve() in s.readable
    assert s.git_write and s.secret_files and s.workspace == tmp_path.resolve()
    s = sb.build_sandbox(settings(tmp_path, sandbox="seatbelt"), tmp_path)
    assert not s.git_write and s.secret_files == []


@macos
async def test_commands_cannot_read_the_home_folder(world):
    c = ctx(world.ws, seatbelt(world), world.scratch)
    out = await shell(c, f"cat {world.home}/.ssh/id_ed25519; cat {world.home}/Documents/taxes.txt; "
                         f"ls {world.home}; cat {world.home}/.cargo/bin/tool.txt; cat a.txt")
    assert "PRIVATE" not in out and "MONEY" not in out and "Operation not permitted" in out
    assert "TOOLA" in out  # the tool folder and the workspace are readable
    assert f"[jimbo: the sandbox blocks reading {world.home}/.ssh/id_ed25519." in out


@macos
async def test_git_reads_work_but_git_writes_fail_however_they_are_run(world):
    c = ctx(world.ws, seatbelt(world), world.scratch)
    out = await shell(c, "git status --short; git log --oneline | wc -l")
    assert "?? a.txt" in out and out.rstrip().endswith("1")
    out = await shell(c, "bash -c 'git add a.txt' ; echo rc=$?; python3 -c \"import subprocess; "
                         "print(subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'x']).returncode)\"")
    assert "rc=128" in out
    assert subprocess.run(["git", "log", "--oneline"], cwd=world.ws, capture_output=True, text=True).stdout.count(
        "\n") == 1
    out = await shell(c, "touch .git/hooks/pre-commit; echo rc=$?")
    assert "rc=1" in out


@macos
async def test_git_write_allows_commits_but_not_hooks_or_config(world):
    c = ctx(world.ws, seatbelt(world, git_write=True), world.scratch)
    out = await shell(c, "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m two; echo rc=$?; "
                         "touch .git/hooks/x; echo hooks=$?; echo x >> .git/config; echo config=$?")
    assert "rc=0" in out and "hooks=1" in out and "config=1" in out


@macos
async def test_temporary_files_go_to_the_scratch_directory(world):
    c = ctx(world.ws, seatbelt(world), world.scratch)
    out = await shell(c, 'python3 -c "import tempfile; print(\'made=\' + tempfile.mkdtemp())"; '
                         'touch /tmp/jimbo-probe-x; echo tmp=$?')
    assert f"made={world.scratch}/tmp/" in out and "tmp=1" in out
    assert not Path("/tmp/jimbo-probe-x").exists()


@macos
async def test_secret_files_are_unreadable_only_when_turned_on(world):
    c = ctx(world.ws, seatbelt(world), world.scratch)
    assert "SECRET=1" in await shell(c, "cat .env")
    c = ctx(world.ws, seatbelt(world, secret_files=[".env", "*.pem"]), world.scratch)
    out = await shell(c, "cat .env; cat a.txt")
    assert "SECRET=1" not in out and "Operation not permitted" in out and "\nA" in out


async def test_commands_do_not_get_secret_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xyz")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    monkeypatch.setenv("MY_PROVIDER", "tok")
    monkeypatch.setenv("KEEP_TOKEN", "kept")
    monkeypatch.setenv("JIMBO_PLAIN", "plain")
    sb.configure_env(keep=["KEEP_TOKEN"], drop=["MY_PROVIDER"])
    try:
        out = await shell(ctx(tmp_path, scratch=tmp_path / "s"), "env")
    finally:
        sb.configure_env()
    assert "sk-or-xyz" not in out and "ghp_abc" not in out and "MY_PROVIDER" not in out
    assert "KEEP_TOKEN=kept" in out and "JIMBO_PLAIN=plain" in out
    assert "GIT_CONFIG_KEY_0=credential.helper" in out and "GIT_OPTIONAL_LOCKS=0" in out
    assert f"TMPDIR={(tmp_path / 's' / 'tmp').resolve()}" in out


def test_is_secret_env():
    for name in ("AWS_SECRET_ACCESS_KEY", "anthropic_api_key", "NPM_TOKEN", "DB_PASSWORD", "SSH_AUTH_SOCK",
                 "GOOGLE_APPLICATION_CREDENTIALS", "HF_AUTH"):
        assert sb.is_secret_env(name), name
    for name in ("PATH", "HOME", "LANG", "VIRTUAL_ENV", "XAUTHORITY", "TERM"):
        assert not sb.is_secret_env(name), name


@macos
async def test_blocked_writes_say_where_to_write_and_git_writes_say_why(world):
    c = ctx(world.ws, seatbelt(world), world.scratch)
    out = await shell(c, "echo x > /tmp/jimbo-probe-y; touch .git/x")
    assert f"[jimbo: the sandbox blocks writing /tmp/jimbo-probe-y. Write temporary files to the scratch directory " \
           f"{world.scratch}.]" in out
    assert "git is read-only" in out
