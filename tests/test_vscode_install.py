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
