"""Tests of the runs drawer and the Live tab."""

import time

import pytest
from PySide6.QtCore import Qt

from app.main_window import DeepVacDesktop
from app.services import chambers_service
from tests.ui.test_runs_list import _insert_run

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    _insert_run("upload/soak", "Soak test", 601)
    _insert_run("upload/ramp", "Ramp test", 300)
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    return win


def _item(window, key):
    view = window.run_list
    return next(view.item(i) for i in range(view.count()) if view.item(i).data(Qt.UserRole) == key)


def test_the_plus_tab_opens_the_list_and_a_click_opens_the_run(window):
    group = window.editor_area.all_groups()[0]
    group.tab_bar.add_btn.click()
    assert window.runs_drawer.is_open()
    assert window.content_stack.currentWidget() is window.editor_area
    window._on_run_clicked(_item(window, "upload/soak"))
    assert window.editor_area.page_for("upload/soak") is not None
    assert "upload/soak" in group.all_keys()
    assert not window.runs_drawer.is_open()


def test_ticking_a_run_compares_it_instead_of_opening_it(window):
    window._open_run("upload/soak")
    window._open_runs_drawer()
    item = _item(window, "upload/ramp")
    item.setCheckState(Qt.Checked)
    window._on_run_clicked(item)
    assert window.editor_area.page_for("upload/ramp") is None
    assert window.editor_area.page_for("upload/soak").compare_runs == {
        "upload/soak",
        "upload/ramp",
    }


def test_analysis_opens_on_the_live_tab(window):
    assert window.editor_area.page_for("upload/soak") is not None
    assert window.editor_area.active_page().run_key == "live"


def test_the_live_rail_opens_a_panel_and_the_tools(window):
    live = window.editor_area.page_for("live")
    live.side_bar.open_panel("statistics")
    assert live.side_bar.current() == "statistics"
    live.side_bar.toggle("statistics")
    assert live.side_bar.current() == ""
    live.side_bar.show_tools(True)
    live.tools.buttons["box"].click()
    assert live.chart.plot.getViewBox().state["mouseMode"] == live.chart.plot.getViewBox().RectMode
    live.tools.tooltip_btn.setChecked(False)
    assert not live.chart.tooltip_enabled
    live.side_bar.open_panel("info")
    live.details_form.dut.setText("FM-2")
    live._save_details()
    from app.services import settings_service

    assert settings_service.load_run_metadata()["dut"] == "FM-2"
    live.calendar.setSelectedDate(live.calendar.selectedDate().addDays(-1))
    live._on_day_chosen(live.calendar.selectedDate())
    assert live.window_combo.currentData() == "day"


def test_the_live_tab_shows_the_recorded_window(window, qtbot):
    chambers_service.add_chamber("PLC 1", "127.0.0.1", 1, protocol="plc")
    live = window.editor_area.page_for("live")
    live.reload_chambers()
    now = time.time()
    for i in range(120):
        window.day_recorder.add("PLC 1", {"timestamp": now - 120 + i, "temp": 20.0 + i / 10})
    live.window_combo.setCurrentIndex(live.window_combo.findData("1h"))
    assert live.chart.datasets and len(live.chart.datasets[0]["points"]) == 120
    assert live.chart.plot.getAxis("bottom").origin == pytest.approx(now - 120)
    live.window_combo.setCurrentIndex(live.window_combo.findData("job"))
    assert live.chart.plot.getAxis("bottom").origin is None


def test_a_saved_experiment_is_announced_in_the_live_tab(window):
    window._on_experiment_saved("monitoring/x", "2026-11-08 10:00 Program Soak")
    live = window.editor_area.page_for("live")
    assert "Program Soak" in live.notice_label.text() and not live.notice_label.isHidden()
