"""Shared pytest fixtures."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DEEPVAC_HOLD_ADVISOR"] = "rules"

import pytest  # noqa: E402
import shiboken6  # noqa: E402
from PySide6.QtCore import QSettings, QThread  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture
def deepvac_data_dir(tmp_path, monkeypatch):
    """Redirects the app's data paths to a temporary directory."""
    data_dir = tmp_path / "deepvac_data"
    monkeypatch.setenv("DEEPVAC_DATA_DIR", str(data_dir))

    import app.common as common

    monkeypatch.setattr(common, "DATA_DIR", data_dir)
    monkeypatch.setattr(common, "REPORTS_DIR", data_dir / "reports")

    import app.services.agent_requests_service as agent_requests_service
    import app.services.alarms_service as alarms_service
    import app.services.annotations_service as annotations_service
    import app.services.auth_service as auth_service
    import app.services.backup_service as backup_service
    import app.services.chambers_service as chambers_service
    import app.services.collab_host_service as collab_host_service
    import app.services.collab_tls as collab_tls
    import app.services.data_service as data_service
    import app.services.derived_variables_service as derived_variables_service
    import app.services.shared_runs_service as shared_runs_service
    import app.services.test_profiles_service as test_profiles_service

    monkeypatch.setattr(auth_service, "AUTH_DB", data_dir / "deepvac_users.sqlite3")
    monkeypatch.setattr(data_service, "CACHE_DB", data_dir / "deepvac_runs.sqlite3")
    monkeypatch.setattr(
        annotations_service, "ANNOTATIONS_DB", data_dir / "deepvac_annotations.sqlite3"
    )
    monkeypatch.setattr(backup_service, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_service, "BACKUPS_DIR", data_dir / "backups")
    monkeypatch.setattr(
        derived_variables_service,
        "DERIVED_VARIABLES_DB",
        data_dir / "deepvac_derived_variables.sqlite3",
    )
    monkeypatch.setattr(alarms_service, "ALARMS_DB", data_dir / "deepvac_alarms.sqlite3")
    monkeypatch.setattr(chambers_service, "CHAMBERS_DB", data_dir / "deepvac_chambers.sqlite3")
    monkeypatch.setattr(
        collab_host_service, "COLLAB_HOST_DB", data_dir / "deepvac_collab_host.sqlite3"
    )
    monkeypatch.setattr(collab_tls, "HOST_TLS_DIR", data_dir / "collab")
    monkeypatch.setattr(
        shared_runs_service, "SHARED_RUNS_DB", data_dir / "deepvac_shared_runs.sqlite3"
    )
    monkeypatch.setattr(
        test_profiles_service, "TEST_PROFILES_DB", data_dir / "deepvac_test_profiles.sqlite3"
    )
    monkeypatch.setattr(
        agent_requests_service,
        "AGENT_REQUESTS_DB",
        data_dir / "deepvac_agent_requests.sqlite3",
    )

    return data_dir


@pytest.fixture
def qsettings_isolated(tmp_path):
    """Redirects QSettings to a temporary INI file."""
    previous_format = QSettings.defaultFormat()

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    yield tmp_path

    QSettings("DeepVac", "Insight").clear()
    QSettings.setDefaultFormat(previous_format)


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Makes message boxes return immediately."""
    calls = []

    def _record(kind):
        def _fn(*args, **kwargs):
            calls.append({"kind": kind, "args": args, "kwargs": kwargs})
            return QMessageBox.StandardButton.Ok

        return _fn

    monkeypatch.setattr(QMessageBox, "critical", staticmethod(_record("critical")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(_record("information")))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_record("warning")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(_record("question")))
    return calls


@pytest.fixture(autouse=True)
def no_blocking_menus(qapp):
    """Makes menus return immediately."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def _close_any_popup():
        popup = QApplication.instance().activePopupWidget()
        if popup is not None:
            popup.close()

    timer = QTimer()
    timer.timeout.connect(_close_any_popup)
    timer.start(10)
    yield
    timer.stop()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Blocks real chamber and OPC connections."""
    from app.services.tcp_client import ChamberConnection

    monkeypatch.setattr(
        ChamberConnection, "connect_to_host", lambda self, *a, **k: None, raising=False
    )


@pytest.fixture
def deepvac_ui(deepvac_data_dir, qsettings_isolated, qapp, tmp_path, monkeypatch):
    """Isolated environment for widget tests."""
    empty_workspace = tmp_path / "empty_workspace"
    empty_workspace.mkdir(exist_ok=True)
    monkeypatch.setenv("DEEPVAC_DATA_ROOT", str(empty_workspace))
    QSettings("DeepVac", "Insight").setValue("agent_api/enabled", False)
    from app.services.demo_launcher import DemoLauncher

    monkeypatch.setattr(DemoLauncher, "start", lambda self, mode, **kw: self._starts.append(mode))
    monkeypatch.setattr(DemoLauncher, "_starts", [], raising=False)

    yield deepvac_data_dir

    app = QApplication.instance()
    for window in list(app.topLevelWidgets()):
        if shiboken6.isValid(window):
            window.close()
    app.processEvents()

    leaked = [t for t in app.findChildren(QThread) if t.isRunning()]
    assert not leaked, f"{len(leaked)} QThread(s) still running after test teardown: {leaked}"


FAKE_USER = {"id": 0, "name": "Test User", "email": "test-user@example.com"}


@pytest.fixture
def fake_user():
    """A test user."""
    return dict(FAKE_USER)
