"""PLC manual and program control on the Controller page (app/plc_control.py),
against the fake PLC (scripts/fake_plc.py)."""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from app.api.chambers import ChamberLinks
from app.plc_control import PlcControlPanel
from app.services import chambers_service, plc_gate
from app.services import test_profiles_service as profiles

pytestmark = pytest.mark.ui


def _load_fake_plc():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_plc.py"
    spec = importlib.util.spec_from_file_location("fake_plc_control", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def panel(deepvac_data_dir, qapp, qtbot, qsettings_isolated):
    fake = _load_fake_plc()
    chamber = fake.Chamber(24.0)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)

    def accept():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(
                target=fake.serve_client, args=(conn, chamber, 0.05), daemon=True
            ).start()

    threading.Thread(target=accept, daemon=True).start()
    plc = chambers_service.add_chamber("PLC", "127.0.0.1", server.getsockname()[1], protocol="plc")
    links = ChamberLinks(connect_timeout_s=5)
    widget = PlcControlPanel(links)
    qtbot.addWidget(widget)
    widget.show()
    widget.set_chamber(plc)
    qtbot.waitUntil(lambda: widget.manual_btn.isEnabled(), timeout=8000)
    yield widget, chamber
    links.close()
    server.close()


def test_manual_setpoint_with_ramp_and_hold(panel, qtbot):
    widget, chamber = panel
    widget.temp_spin.setValue(30.0)
    widget.ramp_check.setChecked(True)
    widget.ramp_spin.setValue(2.0)
    widget.hold_check.setChecked(True)
    widget.hold_spin.setValue(10)
    widget.manual_btn.click()
    qtbot.waitUntil(lambda: chamber.status == 2 and len(chamber.intervals) == 2, timeout=5000)
    ramp, hold = chamber.intervals
    assert chamber.work_type == plc_gate.PROG_TYPE_SETPOINT
    assert ramp["temp"] == pytest.approx(30.0) and ramp["flags"] & (1 << plc_gate.RAMP_BIT)
    assert hold["duration"] == 600 and hold["flags"] & (1 << plc_gate.AWAIT_BIT)
    qtbot.waitUntil(lambda: widget.stop_btn.isEnabled(), timeout=5000)


def test_program_from_a_test_profile_then_next_and_stop(panel, qtbot, monkeypatch):
    widget, chamber = panel
    profiles.add_profile(
        "Two steps",
        "",
        [
            {"setpoint_temp": 30.0, "setpoint_pressure": None, "duration_s": 600, "label": ""},
            {"setpoint_temp": 20.0, "setpoint_pressure": None, "duration_s": 600, "label": ""},
        ],
    )
    widget.load_profiles()
    assert widget.intervals_table.rowCount() == 2
    assert "2 interval(s) · 20:00" in widget.program_info.text()
    widget.program_btn.click()
    qtbot.waitUntil(
        lambda: chamber.status == 2 and chamber.work_type == plc_gate.PROG_TYPE_PROG, timeout=5000
    )
    assert [i["temp"] for i in chamber.intervals] == pytest.approx([30.0, 20.0])

    qtbot.waitUntil(lambda: widget.next_btn.isEnabled(), timeout=5000)
    widget.next_btn.click()
    qtbot.waitUntil(lambda: chamber.interval == 1, timeout=5000)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    widget.stop_btn.click()
    qtbot.waitUntil(lambda: chamber.status == 1, timeout=5000)


def test_finished_job_is_confirmed(panel, qtbot):
    widget, chamber = panel
    with chamber.lock:
        chamber.status = 3  # finish, as the PLC reports a completed job
    qtbot.waitUntil(lambda: widget.finish_btn.isVisible(), timeout=5000)
    widget.finish_btn.click()
    qtbot.waitUntil(lambda: chamber.status == 1, timeout=5000)
    qtbot.waitUntil(lambda: not widget.finish_btn.isVisible(), timeout=5000)
