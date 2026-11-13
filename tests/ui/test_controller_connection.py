"""Tests of the chamber connections dialog."""

import pytest
from PySide6.QtWidgets import QMessageBox

from app.chamber_connections_dialog import ChamberConnectionsDialog
from app.main_window import DeepVacDesktop
from app.services import chambers_service, data_service

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    return win


@pytest.fixture
def dialog(window, qtbot):
    dlg = ChamberConnectionsDialog(window)
    qtbot.addWidget(dlg)
    window._connections_dialog = dlg
    yield dlg
    window._connections_dialog = None


def _chamber(name):
    return next(c for c in chambers_service.list_chambers() if c["name"] == name)


def test_every_saved_chamber_is_listed(window, dialog):
    chambers_service.add_chamber("PLC", "10.0.0.5", 4321, protocol="plc")
    window._refresh_chamber_ui()
    texts = [dialog.combo.itemText(i) for i in range(dialog.combo.count())]
    assert any("Chamber 1" in t and "JSON" in t for t in texts)
    assert any("PLC" in t and "10.0.0.5:4321" in t for t in texts)
    combo = window.controller_widget.chamber_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["PLC"]


def test_connect_and_disconnect(window, dialog, monkeypatch):
    chamber = _chamber("Chamber 1")
    dialog.select(chamber["id"])
    assert dialog.connect_btn.text() == "Connect"
    dialog.connect_btn.click()
    assert chamber["id"] in window._chamber_sessions
    session = window._chamber_sessions[chamber["id"]]
    assert dialog.connect_btn.text() == "Disconnect"
    assert dialog.combo.currentText().startswith("● ")

    live = window.editor_area.page_for("live")
    assert any(
        live.chamber_combo.itemData(i).key == ("json", chamber["id"])
        for i in range(live.chamber_combo.count())
    )

    session.mon_buffer = [{"timestamp": 1_700_000_000 + i, "temp": 20.0 + i} for i in range(5)]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Save)
    before = len(data_service.load_cached_runs())
    dialog.connect_btn.click()
    assert chamber["id"] not in window._chamber_sessions
    assert len(data_service.load_cached_runs()) == before + 1
    assert dialog.connect_btn.text() == "Connect"


def test_cancel_keeps_the_chamber_connected(window, dialog, monkeypatch):
    chamber = _chamber("Chamber 1")
    dialog.select(chamber["id"])
    dialog.connect_btn.click()
    session = window._chamber_sessions[chamber["id"]]
    session.mon_buffer = [{"timestamp": 1_700_000_000, "temp": 20.0}]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    dialog.connect_btn.click()
    assert chamber["id"] in window._chamber_sessions


def test_plc_chambers_are_not_connected_here(window, dialog):
    plc = chambers_service.add_chamber("PLC", "10.0.0.5", 4321, protocol="plc")
    window._refresh_chamber_ui()
    dialog.select(plc["id"])
    assert not dialog.connect_btn.isEnabled()
    assert "Controller page" in dialog.status_lbl.text()


def test_updating_a_chamber_keeps_its_protocol_unless_given(deepvac_data_dir):
    plc = chambers_service.add_chamber("PLC", "10.0.0.5", 4321, protocol="plc")
    assert (
        chambers_service.update_chamber(plc["id"], "PLC 2", "10.0.0.6", 4321)["protocol"] == "plc"
    )
    updated = chambers_service.update_chamber(plc["id"], "PLC 2", "10.0.0.6", 5555, "json")
    assert updated["protocol"] == "json"
