import pytest


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path_factory, monkeypatch):
    """Settings() must never read the developer's real ~/.jimbo/config.toml."""
    monkeypatch.setenv("JIMBO_USER_DIR", str(tmp_path_factory.mktemp("no-user-config")))
