"""Controller page: chambers are connected, disconnected and configured here
(Live Monitoring is gone); their live data shows in Analysis' Live tab."""

import pytest
from PySide6.QtWidgets import QMessageBox

from app.main_window import DeepVacDesktop
from app.services import chambers_service, data_service

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    win._nav_to(5)
    return win


def _select(window, chamber_id):
    combo = window._ctrl_chamber_combo
    combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i) == chamber_id))


def _chamber(name):
    return next(c for c in chambers_service.list_chambers() if c["name"] == name)


def test_every_saved_chamber_is_listed(window):
    chambers_service.add_chamber("PLC", "10.0.0.5", 4321, protocol="plc")
    window._refresh_controller_chamber_choices()
    texts = [
        window._ctrl_chamber_combo.itemText(i) for i in range(window._ctrl_chamber_combo.count())
    ]
    assert any("Chamber 1" in t and "JSON" in t for t in texts)  # seeded default
    assert any("PLC" in t and "10.0.0.5:4321" in t for t in texts)


def test_connect_and_disconnect_from_the_controller(window, monkeypatch):
    chamber = _chamber("Chamber 1")
    _select(window, chamber["id"])
    assert window._ctrl_connect_btn.text() == "Connect"
    window._ctrl_connect_btn.click()
    session = window._chamber_sessions[chamber["id"]]
    assert window._ctrl_connect_btn.text() == "Disconnect"
    assert window._ctrl_chamber_combo.currentText().startswith("● ")
    assert window._ctrl_alarms.session is session  # its alarm rules are edited live

    # the Live tab can follow it
    live = window.editor_area.page_for("live")
    assert any(
        live.chamber_combo.itemData(i).key == ("json", chamber["id"])
        for i in range(live.chamber_combo.count())
    )

    # unsaved samples: offered to save before disconnecting
    session.mon_buffer = [{"timestamp": 1_700_000_000 + i, "temp": 20.0 + i} for i in range(5)]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Save)
    before = len(data_service.load_cached_runs())
    window._ctrl_connect_btn.click()
    assert chamber["id"] not in window._chamber_sessions
    assert len(data_service.load_cached_runs()) == before + 1
    assert window._ctrl_connect_btn.text() == "Connect"


def test_cancel_keeps_the_chamber_connected(window, monkeypatch):
    chamber = _chamber("Chamber 1")
    _select(window, chamber["id"])
    window._ctrl_connect_btn.click()
    session = window._chamber_sessions[chamber["id"]]
    session.mon_buffer = [{"timestamp": 1_700_000_000, "temp": 20.0}]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    window._ctrl_connect_btn.click()
    assert chamber["id"] in window._chamber_sessions


def test_plc_chambers_are_not_connected_here(window):
    plc = chambers_service.add_chamber("PLC", "10.0.0.5", 4321, protocol="plc")
    window._refresh_controller_chamber_choices()
    _select(window, plc["id"])
    assert not window._ctrl_connect_btn.isEnabled()
    assert "Analysis → Live" in window._ctrl_chamber_status_lbl.text()
    assert not window._ctrl_alarms.isVisible()


def test_updating_a_chamber_keeps_its_protocol_unless_given(deepvac_data_dir):
    plc = chambers_service.add_chamber("PLC", "10.0.0.5", 4321, protocol="plc")
    assert (
        chambers_service.update_chamber(plc["id"], "PLC 2", "10.0.0.6", 4321)["protocol"] == "plc"
    )
    updated = chambers_service.update_chamber(plc["id"], "PLC 2", "10.0.0.6", 5555, "json")
    assert updated["protocol"] == "json"
