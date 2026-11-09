import subprocess

import pytest

from jimbo import vscode_install


@pytest.fixture
def vsix(tmp_path, monkeypatch):
    path = tmp_path / "jimbo.vsix"
    monkeypatch.setattr(vscode_install, "VSIX", path)
    return path


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(vscode_install.shutil, "which", lambda cli: f"/bin/{cli}" if cli != "missing" else None)
    monkeypatch.setattr(vscode_install.subprocess, "run",
                        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    return calls


@pytest.mark.parametrize("argv,cli", [([], "code"), (["cursor"], "cursor")])
def test_installs_with_the_named_editor(vsix, runs, argv, cli):
    vsix.write_bytes(b"x")
    vscode_install.install(argv)
    assert runs == [[f"/bin/{cli}", "--install-extension", str(vsix), "--force"]]


@pytest.mark.parametrize("argv,make,error", [([], False, "no VS Code extension"),
                                             (["missing"], True, "'missing' is not on PATH"),
                                             (["code", "x"], True, "usage")])
def test_errors(vsix, runs, argv, make, error):
    if make:
        vsix.write_bytes(b"x")
    with pytest.raises(ValueError, match=error):
        vscode_install.install(argv)
    assert runs == []


@pytest.fixture
def clone(tmp_path, monkeypatch):
    folder = tmp_path / "vscode"
    folder.mkdir()
    (folder / "package.json").write_text("{}")
    monkeypatch.setattr(vscode_install, "VSCODE_DIR", folder)
    monkeypatch.setattr(vscode_install.shutil, "which", lambda name: f"/bin/{name}" if name != "missing" else None)
    return folder


def test_build_runs_bundle_in_the_vscode_folder(clone, runs, monkeypatch):
    monkeypatch.setattr(vscode_install.subprocess, "run",
                        lambda argv, cwd: runs.append((argv, cwd)) or subprocess.CompletedProcess(argv, 0))
    (clone / "node_modules").mkdir()
    vscode_install.build([])
    assert runs == [(["/bin/npm", "run", "bundle"], clone)]


def test_build_installs_dependencies_when_missing(clone, monkeypatch):
    calls = []
    monkeypatch.setattr(vscode_install.subprocess, "run",
                        lambda argv, cwd: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    vscode_install.build([])
    assert calls == [["/bin/npm", "install"], ["/bin/npm", "run", "bundle"]]


def test_build_rejects_arguments(clone, runs):
    with pytest.raises(ValueError, match="usage"):
        vscode_install.build(["x"])
    assert runs == []


def test_build_needs_npm(clone, runs, monkeypatch):
    monkeypatch.setattr(vscode_install.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="'npm' is not on PATH"):
        vscode_install.build([])
    assert runs == []


def test_build_needs_a_clone(tmp_path, runs, monkeypatch):
    monkeypatch.setattr(vscode_install, "VSCODE_DIR", tmp_path / "missing")
    with pytest.raises(ValueError, match="works in a clone"):
        vscode_install.build([])
    assert runs == []
