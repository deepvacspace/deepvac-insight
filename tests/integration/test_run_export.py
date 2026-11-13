"""Tests of the run exports."""

import csv
import json
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from app.api.analyst_tools import AnalystTools
from app.api.runs import RunCatalogue
from app.services import data_service

pytestmark = pytest.mark.integration

MANIFEST = {
    "schema": "deepvac.run/1",
    "identity": {
        "name": "step run",
        "run_uid": "abc123",
        "app": {"name": "Deepvac Control", "version": "0.1"},
    },
    "metadata": {"operator": "Ana", "purpose": "export test", "tags": ["demo"]},
    "channels": [
        {"name": "temp", "label": "Chamber temperature", "unit": "°C", "role": "measured"}
    ],
    "configuration": {
        "captured_at_execution": True,
        "captured_at": "2026-10-20T10:00:00",
        "job": {
            "kind": "program",
            "label": "Step test",
            "program": {"intervals": [{"temp": 40, "duration": 400, "flags": 1 << 8}]},
        },
    },
    "events": [
        {"t": 1_780_000_000.0, "type": "job_start", "message": "Job started", "source": "app"}
    ],
}


def _insert_run(key, name, start_epoch, setpoint, manifest=None):
    rows, temp, integral = [], 20.0, 0.0
    for i in range(400):
        ref = 20.0 if i < 20 else setpoint
        error = ref - temp
        integral += error * 0.02
        u = max(-100.0, min(100.0, 8 * error + integral))
        temp += u * 0.004
        rows.append(
            {
                "timestamp": start_epoch + 2 * i,
                "temp": temp,
                "temp_ref": ref,
                "temp_u": u,
                "temp_u_p": 8 * error,
                "temp_u_i": integral,
                "temp_u_d": 0.0,
            }
        )
    conn = data_service.connect_cache()
    try:
        conn.execute(
            "INSERT INTO runs (key, id, group_name, root_path, run_path, samples_path, source_mtime, samples, "
            "duration_s, mae, start_time, end_time, columns_json, numeric_columns_json, summary_json, bands_json, "
            "annotations_json, samples_json, cached_at, source, start_epoch, manifest_json) "
            "VALUES (?, ?, 'tests', '', '', '', 0, ?, 798, 1.0, ?, '', '[]', '[]', '{}', '[]', '[]', ?, "
            "datetime('now'), 'import', ?, ?)",
            (
                key,
                name,
                len(rows),
                data_service.format_ts(start_epoch),
                json.dumps(rows),
                start_epoch,
                json.dumps(manifest) if manifest else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def analyst(deepvac_data_dir, qapp):
    _insert_run("control/a", "step run", 1_780_000_000, 40.0, MANIFEST)
    _insert_run("control/b", "warmer run", 1_780_100_000, 45.0)
    return AnalystTools(RunCatalogue())


def test_chart_images(analyst):
    chart = analyst.export_chart("#1")
    image = QImage(chart["file"])
    assert (image.width(), image.height()) == (1600, 700) and chart["file"].endswith(".png")
    assert chart["channels"] == ["temp", "temp_ref"] and chart["link"].startswith("file:///")
    control = analyst.export_chart("#1", "control", format="jpg")
    assert control["channels"] == ["temp_u", "temp_u_p", "temp_u_i", "temp_u_d"]
    assert not QImage(control["file"]).isNull()
    zoom = analyst.export_chart("#1", channels=["temp"], from_s=0, to_s=100, format="bmp")
    assert zoom["file"].endswith(".bmp") and zoom["file"] != chart["file"]
    compared = analyst.export_chart("#1", compare_with=["#2"])
    assert compared["runs"] == ["#1", "#2"] and "aligned" in compared["caption"]
    with pytest.raises(ValueError, match="png, bmp or jpg"):
        analyst.export_chart("#1", format="gif")


def test_reports(analyst):
    pdf = analyst.export_report("#1")
    with open(pdf["file"], "rb") as f:
        assert f.read(5) == b"%PDF-"
    html = analyst.export_report("#1", "html", compare_with=["#2"])
    text = Path(html["file"]).read_text(encoding="utf-8")
    for heading in (
        "Experiment report: #1 step run",
        "Control KPIs",
        "Configuration snapshot",
        "Program sent to the PLC",
        "Stages: control KPIs",
        "Anomalies",
        "Events",
        "Comparison",
    ):
        assert heading in text, heading
    assert text.count("data:image/png;base64,") == 3
    assert "Ana" in text and "wait for setpoint" in text


def test_data_comparison_and_bundle(analyst):
    data = analyst.export_data("#1", ["temp", "temp_ref"], 0, 98)
    with open(data["file"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["Time", "Elapsed, s", "Chamber temperature", "temp_ref"]
    assert len(rows) - 1 == data["rows"] == 50 and rows[1][0].startswith("20")
    txt = analyst.export_data("#2", format="txt")
    assert "\t" in Path(txt["file"]).read_text(encoding="utf-8-sig").splitlines()[0]

    comparison = analyst.export_comparison(["#1", "#2"])
    with open(comparison["file"], encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][:5] == ["Experiment", "Start", "End", "Duration, s", "Samples"]
    assert [r[0] for r in rows[1:]] == ["#1 step run", "#2 warmer run"] and rows[1][4] == "400"

    bundle = json.loads(Path(analyst.export_bundle("#1")["file"]).read_text(encoding="utf-8"))
    assert bundle["schema"] == "deepvac.run/1" and len(bundle["samples"]) == 400
    assert {"analysis", "annotations", "variable_rules", "store"} <= set(bundle)
