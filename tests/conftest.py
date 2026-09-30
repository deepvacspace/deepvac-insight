"""Shared pytest fixtures."""

import os

# Must happen before PySide6 is imported anywhere -- including by pytest-qt's
# own plugin machinery during collection -- so set it here, at module import
# time, rather than inside a fixture. Overridable: a developer who explicitly
# exports a different QT_QPA_PLATFORM (e.g. to watch tests run on a real
# display) is left alone.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSettings, QThread  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture
def deepvac_data_dir(tmp_path, monkeypatch):
    """Redirect every database/log/backup/report path this app writes to
    into an isolated tmp_path, so tests never touch the real
    %LOCALAPPDATA%\\DeepVac\\data (or the source-tree data/ dir in a dev
    checkout). Never point a test at the real 52 MB run database.

    Two mechanisms, both pointed at the same directory:

    1. The DEEPVAC_DATA_DIR environment variable (app/common.py's
       resolve_app_paths() reads this) -- correct for anything imported
       for the first time *after* this fixture runs, and for subprocess
       cases like the frozen --smoke-test.
    2. Directly monkeypatching the small, fixed set of already-resolved
       module-level path constants (app.common.DATA_DIR/REPORTS_DIR and
       each service's own DB_PATH-style constant) -- needed because
       app.common is almost certainly already imported by the time any
       test runs (module import is a process-wide singleton), so the env
       var alone can't retroactively change constants that were computed
       from it at import time.

    Not reset here: log_service's setup_logging()/install_excepthook() are
    explicitly idempotent (guarded by a module-level flag, safe to call
    more than once) and don't reopen their file handler for a new
    directory on a second call. No current test suite depends on
    per-test log isolation; if one starts to, that guard is the thing
    to revisit.
    """
    data_dir = tmp_path / "deepvac_data"
    monkeypatch.setenv("DEEPVAC_DATA_DIR", str(data_dir))

    import app.common as common

    monkeypatch.setattr(common, "DATA_DIR", data_dir)
    monkeypatch.setattr(common, "REPORTS_DIR", data_dir / "reports")

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

    return data_dir


@pytest.fixture
def qsettings_isolated(tmp_path):
    """Redirects every QSettings("DeepVac", "Insight") construction (theme,
    language, window geometry, open tabs, per-run channel selection, the
    remember-me token, ...) to an INI file under tmp_path instead of the
    real Windows registry key -- so a test can never read a developer's
    actual saved settings, nor leave test-written settings behind in the
    registry after the run. QSettings's format/path are process-global
    Qt statics (not per-instance), so this is set and restored around the
    test rather than passed as a constructor argument -- every call site
    in app/ just does QSettings("DeepVac", "Insight") unchanged.
    """
    # QSettings has no getter for the custom search path (only the static
    # setPath() setter), so there's nothing to save/restore there -- each
    # test using this fixture points IniFormat at its own tmp_path, which
    # is only ever consulted while defaultFormat() is IniFormat. Restoring
    # defaultFormat() back to Native afterward is what actually matters:
    # it's what makes any *other* code's QSettings("DeepVac", "Insight")
    # call go back to the real registry once this fixture's test ends.
    previous_format = QSettings.defaultFormat()

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    yield tmp_path

    QSettings("DeepVac", "Insight").clear()
    QSettings.setDefaultFormat(previous_format)


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Autouse for the whole suite: a QMessageBox.exec() with nothing to
    dismiss it would hang a test forever rather than fail it. Records what
    was shown (available as the fixture's return value) so a test can
    still assert on dialog content without a human clicking through it."""
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
    """Settings/Account are QMenu.exec() calls -- a real nested event loop
    that blocks waiting for a click, same problem as QMessageBox. Unlike
    QMessageBox's static methods, QMenu.exec() is a Shiboken/C++-backed
    instance method -- monkeypatching the class attribute does NOT
    actually override it (verified: it still hangs). The standard,
    reliable way to end a Qt modal loop from test code instead is a
    QTimer polling QApplication.activePopupWidget() and closing whatever
    it finds, which is what this does for the duration of every test
    (construction/population of the menu still runs for real; it's just
    dismissed with no selection rather than waited on)."""
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
    """Defense in depth on top of the architectural fact (verified in
    Phase 2) that nothing auto-connects on construction: makes it
    impossible for a chamber/OPC connection to actually open a socket
    during a UI test, even if some future change added an unexpected
    auto-connect path. Live Monitoring/OPC Server are only reachable via
    an explicit Connect/Start click, which no smoke test in this suite
    performs -- this exists purely as a safety net, not because a test
    currently needs it."""
    from app.services.tcp_client import ChamberConnection

    monkeypatch.setattr(
        ChamberConnection, "connect_to_host", lambda self, *a, **k: None, raising=False
    )


@pytest.fixture
def deepvac_ui(deepvac_data_dir, qsettings_isolated, qapp, tmp_path, monkeypatch):
    """Composition fixture for widget-construction tests: isolated data
    dir + isolated QSettings + the shared pytest-qt QApplication, plus
    DEEPVAC_DATA_ROOT pointed at an empty tmp_path so DeepVacDesktop.
    __init__()'s load_runs() (which runs unconditionally, for real, on
    construction) can't wander off and discover a real deepvac workspace
    folder that happens to exist on whatever machine runs this suite --
    it just sees zero source files and starts with an empty run list.
    Tears down by closing every top-level window and asserting no
    application-owned QThread (UploadWorker, SimWorker, ...) is still
    running -- a leaked worker thread here would silently keep going into
    the next test otherwise.
    """
    empty_workspace = tmp_path / "empty_workspace"
    empty_workspace.mkdir(exist_ok=True)
    monkeypatch.setenv("DEEPVAC_DATA_ROOT", str(empty_workspace))

    yield deepvac_data_dir

    app = QApplication.instance()
    for window in list(app.topLevelWidgets()):
        window.close()
    app.processEvents()

    leaked = [t for t in app.findChildren(QThread) if t.isRunning()]
    assert not leaked, f"{len(leaked)} QThread(s) still running after test teardown: {leaked}"


FAKE_USER = {"id": 0, "name": "Test User", "email": "test-user@example.com"}


@pytest.fixture
def fake_user():
    """A throwaway in-memory user dict -- bypasses real authentication for
    tests that only need *a* current_user to construct widgets with, same
    approach as app.app._run_smoke_test()'s --smoke-test mode. A fresh copy
    each time since callers may mutate it (e.g. profile-dialog tests)."""
    return dict(FAKE_USER)
