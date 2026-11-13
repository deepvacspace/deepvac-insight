"""Tests of the run tab layout."""

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

from app.run_tab import RunTabPage
from tests.ui.test_run_export_menu import _insert_run

pytestmark = pytest.mark.ui


@pytest.fixture
def page(deepvac_data_dir, qapp, qtbot, qsettings_isolated, monkeypatch):
    _insert_run("upload/a", "step to 40", 1_780_000_000, 40.0)
    _insert_run("upload/b", "step to 45", 1_780_100_000, 45.0)
    runs = [{"key": "upload/a", "id": "step to 40"}, {"key": "upload/b", "id": "step to 45"}]
    widget = RunTabPage("upload/a", runs, current_user={"id": 1, "name": "Ana", "email": ""})
    qtbot.addWidget(widget)
    widget.resize(1200, 800)
    widget.show()
    widget.load()

    def failed(_parent, _title, text):
        raise AssertionError(text)

    monkeypatch.setattr(QMessageBox, "critical", failed)
    return widget


def test_the_rail_has_the_tools_and_panels(page):
    from PySide6.QtWidgets import QToolButton

    rail = {b.text() for b in page.side_bar.findChildren(QToolButton) if b.text()}
    for text in ("Tools", "Channels", "Options", "Statistics", "Info", "Assistant", "Export"):
        assert text in rail
    for text in ("Inspect", "Box zoom", "Fit", "Annotate", "Labels", "Tooltip"):
        assert text in rail
    assert page.title_label.text() == "step to 40"
    assert "300 samples" in page.meta_label.text()
    checks = page.channel_list.checks
    assert set(checks) == {"temp", "temp_ref", "temp_u"}
    assert {k for k, c in checks.items() if c.isChecked()} == {"temp", "temp_ref"}


def test_options_hold_the_range_display_markers_and_references(page):
    assert not page.time_start.isVisible()
    page.side_bar.open_panel("options")
    assert page.time_start.isVisible()
    points = next(b for b in page.findChildren(QPushButton) if b.text() == "Points")
    points.click()
    assert page.chart_mode.currentText() == "scatter"
    page.overlay_avg.setChecked(True)
    assert page.chart.overlay_flags["avg"]
    page.tools.set_tool("annotate")
    assert page.chart.plot.getViewBox().annotate_mode
    page.tools.set_tool("inspect")
    assert not page.chart.plot.getViewBox().annotate_mode


def test_info_saves_the_runs_details(page):
    from app.services import data_service

    page.side_bar.open_panel("info")
    page.details_form.dut.setText("FM-2")
    page.details_form.tags.setText("qualification, batch-7")
    page._save_details()
    saved = data_service.run_metadata("upload/a")
    assert saved["dut"] == "FM-2" and saved["tags"] == ["qualification", "batch-7"]


def test_statistics_window_shows_range_kpis_stages(page):
    page._open_statistics()
    assert page._statistics_dialog.isVisible()
    channels = {page._range_table.item(r, 0).text() for r in range(page._range_table.rowCount())}
    assert channels == {"temp", "temp_ref"}
    assert "MAE" in page._range_kpi_label.text()
    assert page._kpi_tiles[0].text().endswith("°C")
    assert page._stages_table.rowCount() >= 1
    page._zoom_to_stage(0)
    page._statistics_dialog.close()


def test_compare_updates_button_setpoint_and_comparison(page):
    page.set_compare_run("upload/b", True)
    assert page._compare_btn.text() == "Compare (1)"
    assert not page._setpoint_group.isHidden()
    page._open_statistics()
    assert page._comparison_table.columnCount() == 3
    assert page._comparison_table.rowCount() > 0
    page._clear_comparison()
    assert page._compare_btn.text() == "Compare" and page.compare_runs == {"upload/a"}
    page._statistics_dialog.close()


def test_rules_and_annotations_dialogs(page, monkeypatch):
    page._rule_ch_combo.setCurrentText("temp")
    page._rule_lo_ed.setText("39")
    page._rule_hi_ed.setText("41")
    page._add_var_rule()
    assert page._rules_list.count() == 1 and "[39 … 41]" in page._rules_list.item(0).text()
    page._rules_list.setCurrentRow(0)
    page._remove_selected_rule()
    assert page._rules_list.count() == 0

    page._on_annotation_committed({"x0": 10.0, "x1": 60.0, "label": "warm-up", "color": "#16a34a"})
    assert page._ann_table.rowCount() == 1
    assert page._ann_table.item(0, 1).text() == "warm-up"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page._ann_table.selectRow(0)
    remove = next(
        b for b in page._annotations_dialog.findChildren(QPushButton) if b.text() == "Remove"
    )
    remove.click()
    assert page._ann_table.rowCount() == 0
