"""The Export menu of an Analysis run tab (app/run_tab.py): DeepVac Control's
export options, written through app/services/run_export.py."""

import csv
import json

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton

from app.run_tab import RunTabPage
from app.services import data_service

pytestmark = pytest.mark.ui

COLUMNS = ["timestamp", "temp", "temp_ref", "temp_u"]


def _insert_run(key, name, start, setpoint):
    rows, temp = [], 20.0
    for i in range(300):
        ref = 20.0 if i < 10 else setpoint
        u = max(-100.0, min(100.0, 10 * (ref - temp)))
        temp += u * 0.004
        rows.append({"timestamp": start + 2 * i, "temp": temp, "temp_ref": ref, "temp_u": u})
    conn = data_service.connect_cache()
    try:
        conn.execute(
            "INSERT INTO runs (key, id, group_name, root_path, run_path, samples_path, source_mtime, samples, "
            "duration_s, start_time, end_time, columns_json, numeric_columns_json, summary_json, bands_json, "
            "annotations_json, samples_json, cached_at, source, start_epoch) VALUES (?, ?, 'tests', '', '', '', 0, "
            "?, 598, ?, '', ?, ?, '{}', '[]', '[]', ?, datetime('now'), 'upload', ?)",
            (
                key,
                name,
                len(rows),
                data_service.format_ts(start),
                json.dumps(COLUMNS),
                json.dumps(COLUMNS[1:]),
                json.dumps(rows),
                start,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def page(deepvac_data_dir, qapp, qtbot, qsettings_isolated, tmp_path, monkeypatch):
    _insert_run("upload/a", "step to 40", 1_780_000_000, 40.0)
    _insert_run("upload/b", "step to 45", 1_780_100_000, 45.0)
    runs = [{"key": "upload/a", "id": "step to 40"}, {"key": "upload/b", "id": "step to 45"}]
    widget = RunTabPage("upload/a", runs, current_user={"id": 1, "name": "Ana", "email": ""})
    qtbot.addWidget(widget)
    widget.resize(1200, 800)
    widget.show()
    widget.load()
    saved = []

    def save_as(_parent, _title, suggested, _filter):
        path = tmp_path / ("out_" + suggested.replace("\\", "/").rsplit("/", 1)[-1])
        saved.append(path)
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", save_as)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)

    def failed(_parent, _title, text):
        raise AssertionError(text)

    monkeypatch.setattr(QMessageBox, "critical", failed)
    return widget, saved


def _actions(menu):
    found = {}
    for action in menu.actions():
        if action.menu():
            found.update({f"{action.text()} / {k}": v for k, v in _actions(action.menu()).items()})
        elif action.text():
            found[action.text()] = action
    return found


def test_the_menu_has_deepvac_controls_options(page):
    widget, _ = page
    export = next(b for b in widget.findChildren(QPushButton) if b.text() == "Export")
    assert sorted(_actions(export.menu())) == sorted(
        [
            "Chart image / PNG…",
            "Chart image / BMP…",
            "Chart image / JPEG…",
            "Data / Whole experiment (CSV)…",
            "Data / Whole experiment (TXT)…",
            "Data / Visible range (CSV)…",
            "Data / Visible range (TXT)…",
            "Comparison CSV…",
            "Experiment report (HTML)…",
            "Experiment report (PDF)…",
            "Experiment bundle (JSON)…",
        ]
    )


def test_exports_write_the_files(page):
    widget, saved = page
    image = widget.export_chart_image("png")
    assert not QImage(str(image)).isNull() and image.name == "out_step_to_40.png"
    assert not QImage(str(widget.export_chart_image("jpg"))).isNull()

    whole = widget.export_run_data()
    with open(whole, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][:2] == ["Time", "Elapsed, s"] and len(rows) == 301
    widget.chart.set_time_range(0, 100)
    visible = widget.export_run_data(visible_only=True, tab_separated=True)
    assert (
        visible.suffix == ".txt" and len(visible.read_text(encoding="utf-8-sig").splitlines()) < 60
    )

    pdf = widget.export_report("pdf")
    assert pdf.read_bytes()[:5] == b"%PDF-"
    html = widget.export_report("html").read_text(encoding="utf-8")
    assert "Chart as shown" in html and "for Ana" in html

    bundle = json.loads(widget.export_bundle().read_text(encoding="utf-8"))
    assert len(bundle["samples"]) == 300


def test_comparison_exports_use_the_compared_runs(page):
    widget, _ = page
    widget.set_compare_run("upload/b", True)
    comparison = widget.export_comparison_csv()
    with open(comparison, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert [r[0] for r in rows[1:]] == ["step to 40", "step to 45"]
    html = widget.export_report("html").read_text(encoding="utf-8")
    assert "Comparison" in html and "aligned on the run start" in html
