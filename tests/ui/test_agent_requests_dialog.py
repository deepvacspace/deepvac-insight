"""Tests of the agent requests dialog."""

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox, QPushButton

from app.agent_requests_dialog import AgentRequestsDialog
from app.services import agent_requests_service as svc

pytestmark = pytest.mark.ui


def _buttons(dialog, row):
    cell = dialog._table.cellWidget(row, 7)
    return {b.text(): b for b in cell.findChildren(QPushButton)} if cell else {}


def test_approve_and_reject(deepvac_data_dir, qapp, qtbot, monkeypatch):
    chamber = {"id": 1, "name": "Testa chamber"}
    first = svc.create_request("setpoint", chamber, {"temperature_c": 30}, "Set to 30 °C", "soak")
    second = svc.create_request("stop", chamber, {}, "Stop the job", "done")
    dialog = AgentRequestsDialog({"name": "Ana"})
    qtbot.addWidget(dialog)
    assert dialog._table.rowCount() == 2

    rows = {dialog._table.item(r, 0).text(): r for r in range(2)}
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    _buttons(dialog, rows[str(first["id"])])["Approve"].click()
    assert svc.get_request(first["id"])["status"] == svc.APPROVED
    assert svc.get_request(first["id"])["decided_by"] == "Ana"

    rows = {dialog._table.item(r, 0).text(): r for r in range(2)}
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("not now", True))
    _buttons(dialog, rows[str(second["id"])])["Reject"].click()
    rejected = svc.get_request(second["id"])
    assert (rejected["status"], rejected["decision_comment"]) == (svc.REJECTED, "not now")
    assert all(not _buttons(dialog, r) for r in range(2))
