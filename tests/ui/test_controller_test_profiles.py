"""Tests for the Test Profile step-sequencer, now living on
app.services.chamber_session.ChamberSession (start_test/_test_send_current_step/
_test_advance_step/stop_test) and driven from app/views/controller.py's
Chamber-scoped Start/Stop Test buttons.

session.tcp.is_connected()/send_command() are monkeypatched so this
exercises the sequencing logic in isolation from real sockets (see
tests/integration/test_tcp_client.py for the real wire-protocol
verification). Step durations are kept tiny (real seconds, not faked) so
the real QTimer machinery drives the test at normal speed.
"""

import pytest

from app.main_window import DeepVacDesktop
from app.services import test_profiles_service as profiles

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    win._nav_to(6)
    return win


@pytest.fixture
def connected_chamber(window, monkeypatch):
    sent = []
    chamber = {"id": 1, "name": "Chamber 1", "host": "127.0.0.1", "port": 5555}
    session = window._connect_chamber(chamber)
    monkeypatch.setattr(session.tcp, "is_connected", lambda: True)
    monkeypatch.setattr(session.tcp, "send_command", lambda payload: sent.append(payload))
    window._refresh_controller_page()
    return sent, session


def _select_profile(window, profile_id):
    window._load_test_profiles()
    idx = next(
        i
        for i in range(window._test_profile_combo.count())
        if window._test_profile_combo.itemData(i)["id"] == profile_id
    )
    window._test_profile_combo.setCurrentIndex(idx)


def test_start_test_sends_first_step_immediately(deepvac_data_dir, window, connected_chamber):
    sent, session = connected_chamber
    profile = profiles.add_profile(
        "Quick Test",
        "",
        [
            {
                "setpoint_temp": 50.0,
                "setpoint_pressure": None,
                "duration_s": 5.0,
                "label": "Step 1",
            },
            {
                "setpoint_temp": 80.0,
                "setpoint_pressure": None,
                "duration_s": 5.0,
                "label": "Step 2",
            },
        ],
    )
    _select_profile(window, profile["id"])

    window._test_start()

    assert len(sent) == 1
    assert sent[0]["cmd"] == "set_point"
    assert sent[0]["temperature"] == 50.0
    assert sent[0]["step_index"] == 0
    assert sent[0]["profile_name"] == "Quick Test"
    assert session.test_running_profile is not None
    assert window._test_stop_btn.isEnabled() is True
    assert window._test_start_btn.isEnabled() is False


def test_sequencer_advances_through_all_steps_and_completes(
    deepvac_data_dir, window, connected_chamber, qtbot
):
    sent, session = connected_chamber
    profile = profiles.add_profile(
        "Quick Test",
        "",
        [
            {
                "setpoint_temp": 50.0,
                "setpoint_pressure": None,
                "duration_s": 0.05,
                "label": "Step 1",
            },
            {
                "setpoint_temp": 80.0,
                "setpoint_pressure": None,
                "duration_s": 0.05,
                "label": "Step 2",
            },
        ],
    )
    _select_profile(window, profile["id"])

    window._test_start()
    qtbot.waitUntil(lambda: len(sent) == 2, timeout=2000)
    assert sent[1]["temperature"] == 80.0
    assert sent[1]["step_index"] == 1

    qtbot.waitUntil(lambda: session.test_running_profile is None, timeout=2000)
    window._refresh_controller_page()
    assert "complete" in window._test_status_lbl.text().lower()
    assert window._test_stop_btn.isEnabled() is False


def test_stop_test_cancels_a_running_sequence(deepvac_data_dir, window, connected_chamber):
    sent, session = connected_chamber
    profile = profiles.add_profile(
        "Long Test",
        "",
        [{"setpoint_temp": 50.0, "setpoint_pressure": None, "duration_s": 30.0, "label": ""}],
    )
    _select_profile(window, profile["id"])

    window._test_start()
    assert session.test_running_profile is not None

    window._test_stop_clicked()

    assert session.test_running_profile is None
    assert window._test_stop_btn.isEnabled() is False
    assert "stopped" in window._test_status_lbl.text().lower()


def test_start_test_disabled_without_a_connected_chamber(deepvac_data_dir, window):
    profile = profiles.add_profile(
        "Quick Test",
        "",
        [{"setpoint_temp": 50.0, "setpoint_pressure": None, "duration_s": 5.0, "label": ""}],
    )
    _select_profile(window, profile["id"])
    assert window._chamber_sessions == {}
    assert window._test_start_btn.isEnabled() is False


def test_send_command_failure_mid_test_stops_and_reports_error(
    deepvac_data_dir, window, monkeypatch
):
    profile = profiles.add_profile(
        "Quick Test",
        "",
        [{"setpoint_temp": 50.0, "setpoint_pressure": None, "duration_s": 30.0, "label": ""}],
    )
    chamber = {"id": 1, "name": "Chamber 1", "host": "127.0.0.1", "port": 5555}
    session = window._connect_chamber(chamber)
    monkeypatch.setattr(session.tcp, "is_connected", lambda: True)

    def _raise(payload):
        raise RuntimeError("chamber not connected")

    monkeypatch.setattr(session.tcp, "send_command", _raise)
    _select_profile(window, profile["id"])

    window._test_start()

    assert session.test_running_profile is None
    assert "chamber not connected" in window._test_status_lbl.text()


def test_saving_session_after_running_a_test_tags_the_run_with_its_profile(
    deepvac_data_dir, window, connected_chamber, monkeypatch
):
    from PySide6.QtWidgets import QInputDialog

    sent, session = connected_chamber
    profile = profiles.add_profile(
        "Quick Test",
        "",
        [{"setpoint_temp": 50.0, "setpoint_pressure": None, "duration_s": 30.0, "label": ""}],
    )
    _select_profile(window, profile["id"])
    window._test_start()

    session.mon_buffer = [{"timestamp": "1700000000", "temp": "20.0"}]
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("live-test", True)))

    page = window.monitor_editor_area.active_page()
    page._save_monitoring_session()

    from app.services import data_service

    runs = data_service.load_cached_runs()
    assert runs[0]["chamber"] == "Chamber 1"
    assert runs[0]["test_profile"] == "Quick Test"
