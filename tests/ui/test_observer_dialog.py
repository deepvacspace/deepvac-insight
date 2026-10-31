"""ObserverDialog: acknowledging events and saving settings."""

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

from app.observer_dialog import ObserverDialog
from app.services import observer_service as obs

pytestmark = pytest.mark.ui


def test_acknowledge_and_settings(deepvac_data_dir, qapp, qtbot, monkeypatch):
    chamber = {"id": 1, "name": "DV-1"}
    alarm = obs.record(obs.ALARM, obs.CRITICAL, "Alarm on DV-1", {}, chamber, "a1")
    obs.record(obs.JOB_ENDED, obs.INFO, "The job on DV-1 finished", {}, chamber, "j1")
    obs.report(alarm["id"], "Over-temperature alarm.", True, "Check DV-1.")
    dialog = ObserverDialog({"name": "Ana"})
    qtbot.addWidget(dialog)
    assert dialog._table.rowCount() == 1
    assert "notified: Check DV-1." in dialog._table.item(0, 5).text()
    assert not dialog._push.isChecked()

    dialog._show_info.setChecked(True)
    assert dialog._table.rowCount() == 2
    row = next(r for r in range(2) if dialog._table.item(r, 0).text() == str(alarm["id"]))
    dialog._table.cellWidget(row, ObserverDialog.COLUMNS - 1).click()
    assert obs.get_event(alarm["id"])["acknowledged_by"] == "Ana"
    assert obs.unacknowledged_count() == 0

    dialog._drift_c.setValue(4.5)
    dialog._save_settings()
    assert obs.load_config()["drift_c"] == 4.5 and obs.load_config()["push"] is False

    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    next(b for b in dialog.findChildren(QPushButton) if b.text() == "Send Test Event").click()
    test_event = obs.list_events()[0]
    assert test_event["kind"] == obs.TEST and test_event["push_state"] == obs.PUSH_OFF
    assert "Sending to n8n is off" in shown[0]
