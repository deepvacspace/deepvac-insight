"""Runs page: each run shows its duration and sample count and has a report
download button; Reports, Monitor and OPC Server are hidden from the sidebar."""

import json

import openpyxl
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFileDialog, QStyleOptionViewItem

from app.main_window import DeepVacDesktop
from app.services import data_service
from app.views.runs import RUN_DETAILS_ROLE, _duration_text

pytestmark = pytest.mark.ui


def _insert_run(key, name, samples):
    rows = [
        {"timestamp": 1_780_000_000 + 2 * i, "temp": 20.0 + i * 0.01, "temp_ref": 25.0}
        for i in range(samples)
    ]
    conn = data_service.connect_cache()
    try:
        conn.execute(
            "INSERT INTO runs (key, id, group_name, root_path, run_path, samples_path, source_mtime, samples, "
            "duration_s, start_time, end_time, columns_json, numeric_columns_json, summary_json, bands_json, "
            "annotations_json, samples_json, cached_at, source) VALUES (?, ?, 'tests', '', '', '', 0, ?, ?, '', '', "
            "?, ?, '{}', '[]', '[]', ?, datetime('now'), 'upload')",
            (
                key,
                name,
                samples,
                2.0 * (samples - 1),
                json.dumps(["timestamp", "temp", "temp_ref"]),
                json.dumps(["temp", "temp_ref"]),
                json.dumps(rows),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    _insert_run("upload/soak", "Soak test", 601)
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    win._nav_to(1)
    return win


def test_only_the_main_pages_are_in_the_sidebar(window):
    assert not window.act_reports_btn.isVisible()
    assert not hasattr(window, "act_monitor_btn")  # Live Monitoring is gone
    assert not window.act_opc_btn.isVisible()
    for btn in (
        window.act_dashboard_btn,
        window.act_runs_btn,
        window.act_analysis_btn,
        window.act_sim_btn,
    ):
        assert btn.isVisible()


def test_each_run_shows_duration_and_samples(window):
    item = next(
        window.run_list.item(i)
        for i in range(window.run_list.count())
        if window.run_list.item(i).data(Qt.UserRole) == "upload/soak"
    )
    assert item.data(RUN_DETAILS_ROLE) == "20 min · 601 samples"


def test_duration_text():
    assert _duration_text(None) == "—"
    assert _duration_text(42) == "42 s"
    assert _duration_text(95) == "1 min 35 s"
    assert _duration_text(1200) == "20 min"
    assert _duration_text(3 * 3600 + 5 * 60) == "3 h 05 min"


def test_download_button_writes_the_report(window, tmp_path, monkeypatch):
    target = tmp_path / "soak_report.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    view = window.run_list
    row = next(i for i in range(view.count()) if view.item(i).data(Qt.UserRole) == "upload/soak")
    rect = view.visualItemRect(view.item(row))
    point = QPointF(window._run_list_delegate.icon_rect(rect).center())
    option = QStyleOptionViewItem()
    option.rect = rect
    index = view.model().index(row, 0)
    for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
        event = QMouseEvent(kind, point, point, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        assert window._run_list_delegate.editorEvent(
            event, view.model(), option, index
        )  # the button takes the click
    assert target.exists()
    sheet = openpyxl.load_workbook(target).active
    assert sheet.max_row == 602  # header + every sample
