import pytest

from jimbo.config import Settings


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path_factory, monkeypatch):
    """Settings() must never read the developer's real ~/.jimbo/config.toml."""
    monkeypatch.setenv("JIMBO_USER_DIR", str(tmp_path_factory.mktemp("no-user-config")))


@pytest.fixture(autouse=True, scope="session")
def no_container_probe():
    """sandbox = "auto" tries Podman and Docker first; tests use the old default, seatbelt when it is available."""
    field = Settings.model_fields["sandbox"]
    real = field.default
    field.default = "seatbelt"
    Settings.model_rebuild(force=True)
    yield
    field.default = real
    Settings.model_rebuild(force=True)


@pytest.fixture(autouse=True, scope="session")
def isolated_session_logs(tmp_path_factory):
    """Sessions saved by tests must not land in the developer's real ~/.jimbo/sessions."""
    field = Settings.model_fields["log_dir"]
    real = field.default
    field.default = tmp_path_factory.mktemp("sessions")
    Settings.model_rebuild(force=True)
    yield
    field.default = real
    Settings.model_rebuild(force=True)
