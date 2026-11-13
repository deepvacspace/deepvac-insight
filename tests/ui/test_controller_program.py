"""Tests of the Controller page."""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from app.api.chambers import ChamberLinks
from app.program_model import AWAIT_BIT, HOLD_UNTIL_STOPPED, RAMP_BIT
from app.services import chambers_service, plc_gate
from app.services import test_profiles_service as profiles
from app.views.controller import STEP_DETAILS, STEP_INTERVALS, STEP_START, ControllerPage

pytestmark = pytest.mark.ui


def _load_fake_plc():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_plc.py"
    spec = importlib.util.spec_from_file_location("fake_plc_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def page(deepvac_data_dir, qapp, qtbot, qsettings_isolated):
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
    chambers_service.add_chamber("PLC", "127.0.0.1", server.getsockname()[1], protocol="plc")
    links = ChamberLinks(connect_timeout_s=5)
    widget = ControllerPage(links, {"name": "Ana"})
    qtbot.addWidget(widget)
    widget.resize(1400, 900)
    widget.show()
    qtbot.waitUntil(widget.online, timeout=8000)
    widget.poll()
    yield widget, chamber
    links.close()
    server.close()


def _ready(widget):
    widget.details.purpose.setText("Soak test")
    widget.details.operator.setText("Ana")


def test_starts_with_the_default_interval_and_the_steps(page):
    widget, _chamber = page
    assert widget.model.count() == 1
    temp = widget.model.value(0, "temp")
    assert widget.interval_list.card_title(0) == f"Ramp to {temp:.1f} °C".replace("-", "−")
    assert widget.stepper.current() == STEP_DETAILS
    assert widget.details.operator.text() == "Ana"
    assert widget.details.purpose.text().startswith("Untitled ")
    assert widget.stepper.next_button(STEP_DETAILS).isEnabled()
    widget.details.purpose.setText("")
    assert not widget.stepper.next_button(STEP_DETAILS).isEnabled()


def test_intervals_are_edited_in_their_cards(page):
    widget, _chamber = page
    widget.stepper.set_current(STEP_INTERVALS)
    widget.add_btn.click()
    assert widget.model.count() == 2 and widget.model.selected() == 1
    edit = widget.interval_list.editor_widget("values")[widget.model.row_of("temp")]
    edit.setText("80")
    edit.setModified(True)
    edit.editingFinished.emit()
    assert widget.model.value(1, "temp") == pytest.approx(80.0)
    assert widget.interval_list.card_title(1) == "Ramp to 80.0 °C"

    mode = widget.interval_list.editor_widget("mode")
    mode.activated.emit(0)
    flags = widget.model.flags(1)
    assert flags & (1 << AWAIT_BIT) and not flags & (1 << RAMP_BIT)
    assert widget.interval_list.card_title(1) == "Go to 80.0 °C and hold"

    widget.interval_list.editor_widget("until").click()
    assert widget.model.value(1, "duration") == HOLD_UNTIL_STOPPED
    assert "held until stopped" in widget.summary_label.text()

    widget.model.select(0)
    widget.interval_list.editor_widget("hold").click()
    assert widget.model.count() == 3
    temp = widget.model.value(0, "temp")
    assert widget.interval_list.card_title(1) == f"Go to {temp:.1f} °C and hold".replace("-", "−")
    widget.interval_list.editor_widget("delete").click()
    assert widget.model.count() == 2


def test_a_program_runs_on_the_chamber(page, qtbot, monkeypatch):
    widget, chamber = page
    _ready(widget)
    widget.add_btn.click()
    widget.model.set_value(1, "temp", 20.0)
    widget.model.emit_changed()
    widget.stepper.set_current(STEP_START)
    assert widget.start_btn.isEnabled()
    widget.start_btn.click()
    qtbot.waitUntil(
        lambda: chamber.status == 2 and chamber.work_type == plc_gate.PROG_TYPE_PROG, timeout=5000
    )
    first = widget.model.value(0, "temp")
    assert [i["temp"] for i in chamber.intervals] == pytest.approx([first, 20.0])

    qtbot.waitUntil(lambda: "interval 1 of 2" in widget.run_status.text(), timeout=5000)
    assert widget.stepper.locked()
    assert widget.stop_btn.isEnabled()
    widget.next_btn.click()
    qtbot.waitUntil(lambda: chamber.interval == 1, timeout=5000)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    widget.stop_btn.click()
    qtbot.waitUntil(lambda: chamber.status == 1, timeout=5000)
    qtbot.waitUntil(lambda: not widget.stepper.locked(), timeout=5000)


def test_one_interval_is_a_setpoint_that_start_replaces(page, qtbot, monkeypatch):
    widget, chamber = page
    _ready(widget)
    widget.start_btn.click()
    qtbot.waitUntil(
        lambda: chamber.status == 2 and chamber.work_type == plc_gate.PROG_TYPE_SETPOINT,
        timeout=5000,
    )
    qtbot.waitUntil(widget.setpoint_running, timeout=5000)
    assert not widget.stepper.locked()

    widget.model.set_value(0, "temp", 35.0)
    widget.model.emit_changed()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    widget.start_btn.click()
    qtbot.waitUntil(
        lambda: chamber.intervals and chamber.intervals[0]["temp"] == 35.0, timeout=5000
    )


def test_start_needs_the_details(page, no_modal_dialogs):
    widget, chamber = page
    widget.details.purpose.setText("")
    widget.stepper.set_current(STEP_START)
    widget.start_btn.click()
    assert widget.stepper.current() == STEP_DETAILS
    assert chamber.status == 1
    assert any("name or purpose" in str(c["args"]) for c in no_modal_dialogs)


def test_program_library_round_trip(page, monkeypatch):
    widget, _chamber = page
    widget.add_btn.click()
    widget.model.set_value(1, "temp", -20.0)
    widget.model.emit_changed()
    widget.model.set_cycle({"start": 0, "end": 1, "count": 3})
    widget._label_edited(1, "Cold soak")

    from app.program_library import ProgramSaveDialog

    dialog = ProgramSaveDialog(widget.model, "", widget._labels, "Ana", widget)
    dialog.name_edit.setText("Cycle A")
    dialog.save()
    saved = profiles.find_by_name("Cycle A")
    assert saved["program"]["cycle"] == {"start": 0, "end": 1, "count": 3}
    assert [s["label"] for s in saved["steps"]] == ["", "Cold soak"]

    widget.model.set_program(
        {"intervals": [widget.model.interval_map(0)], "cycle": {"start": 0, "end": 0, "count": 0}}
    )
    assert widget.load_profile(saved)
    assert widget.model.count() == 2
    assert widget.model.value(1, "temp") == pytest.approx(-20.0)
    assert widget.model.cycle()["count"] == 3
    assert widget.program_label.full_text() == "Cycle A"
    assert widget.library_badge.text() == "LIBRARY"
    widget.model.set_value(0, "temp", 30.0)
    widget.model.emit_changed()
    assert widget.library_badge.text() == "LIBRARY · MODIFIED"


def test_a_steps_only_profile_loads_as_holds(page):
    widget, _chamber = page
    profile = profiles.add_profile(
        "Two steps",
        "",
        [
            {"setpoint_temp": 30.0, "setpoint_pressure": None, "duration_s": 600, "label": "warm"},
            {"setpoint_temp": 10.0, "setpoint_pressure": None, "duration_s": 900, "label": ""},
        ],
    )
    assert widget.load_profile(profile)
    assert [widget.model.value(i, "temp") for i in range(2)] == pytest.approx([30.0, 10.0])
    assert widget.interval_list.card_title(0) == "Go to 30.0 °C and hold"
    assert "warm" in widget.interval_list.card_subtitle(0)


def test_a_finished_job_is_confirmed(page, qtbot):
    widget, chamber = page
    with chamber.lock:
        chamber.status = 3
    qtbot.waitUntil(lambda: widget.finish_btn.isVisible(), timeout=5000)
    widget.finish_btn.click()
    qtbot.waitUntil(lambda: chamber.status == 1, timeout=5000)
    qtbot.waitUntil(lambda: not widget.finish_btn.isVisible(), timeout=5000)
