"""Tests of the experiment recorder."""

import json
import time
from datetime import datetime

import pytest

from app.services import data_service, day_recorder, experiment_recorder
from app.services.experiment_recorder import ExperimentRecorder

pytestmark = pytest.mark.integration

T0 = 1_780_000_000.0


def _sample(t, status=2, temp=20.0, ref=40.0):
    return {"timestamp": t, "status_condition": status, "temp": temp, "temp_ref": ref}


def _run_job(recorder, chamber, seconds, end_status=3, start=T0):
    for i in range(int(seconds) + 1):
        t = start + i
        recorder.add(chamber, _sample(t, temp=20.0 + min(i, 200) * 0.1), True)
    end = _sample(start + seconds + 1, status=end_status)
    recorder.add(chamber, end, experiment_recorder.plc_working(end))


def _saved_rows():
    conn = data_service.connect_cache()
    try:
        return conn.execute(
            "SELECT id, chamber, test_profile, samples, start_epoch, mae, manifest_json, source "
            "FROM runs ORDER BY start_epoch"
        ).fetchall()
    finally:
        conn.close()


def test_a_finished_job_is_saved_as_a_run_named_after_what_was_sent(deepvac_data_dir):
    saved = []
    recorder = ExperimentRecorder(
        configuration=lambda chamber: {"user": {"name": "Ana"}},
        channels=lambda chamber: [{"name": "temp", "label": "Temperature", "unit": "°C"}],
        on_saved=lambda key, name: saved.append((key, name)),
    )
    experiment_recorder.announce_job("PLC 1", "Program Soak", "program", test_profile="Soak")
    _run_job(recorder, "PLC 1", 300)
    recorder.wait_saved()

    expected = f"{datetime.fromtimestamp(T0):%Y-%m-%d %H:%M} Program Soak"
    assert [name for _, name in saved] == [expected]
    (row,) = _saved_rows()
    assert (row["id"], row["chamber"], row["test_profile"]) == (expected, "PLC 1", "Soak")
    assert row["samples"] == 301 and row["start_epoch"] == pytest.approx(T0)
    assert row["source"] == "monitoring" and row["mae"] is not None
    manifest = json.loads(row["manifest_json"])
    assert manifest["configuration"]["job"] == {
        "kind": "program",
        "label": "Program Soak",
        "test_profile": "Soak",
    }
    assert manifest["configuration"]["user"] == {"name": "Ana"}
    assert manifest["channels"][0]["unit"] == "°C"
    assert [e["type"] for e in manifest["events"]] == ["job_start", "job_end"]
    assert manifest["events"][1]["data"] == {"outcome": "finished"}
    assert not list(experiment_recorder.journal_dir().glob("*.jsonl"))


def test_short_jobs_are_not_saved_and_alarms_are_named(deepvac_data_dir):
    recorder = ExperimentRecorder()
    _run_job(recorder, "PLC", 30)
    _run_job(recorder, "PLC", 120, end_status=4, start=T0 + 1000)
    recorder.wait_saved()
    rows = _saved_rows()
    assert len(rows) == 1 and rows[0]["id"].endswith("Chamber job (alarm)")
    assert json.loads(rows[0]["manifest_json"])["configuration"]["job"]["kind"] == "external"


def test_a_sample_feed_job_ends_when_the_chamber_stops(deepvac_data_dir):
    recorder = ExperimentRecorder()
    recorder.set_next_job("Feed", "Manual setpoint", "manual_setpoint")
    for i in range(90):
        recorder.add("Feed", {"timestamp": T0 + i, "temp": 25.0, "temp_ref": 30.0}, True)
    assert recorder.running("Feed")["label"] == "Manual setpoint"
    recorder.set_working("Feed", False)
    recorder.wait_saved()
    assert recorder.running("Feed") is None
    assert _saved_rows()[0]["id"].endswith("Manual setpoint")


def test_a_job_cut_short_by_a_stop_is_saved_at_the_next_start(deepvac_data_dir):
    first = ExperimentRecorder()
    for i in range(200):
        first.add("PLC", _sample(T0 + i), True)
    first.close()
    assert len(list(experiment_recorder.journal_dir().glob("*.jsonl"))) == 1

    second = ExperimentRecorder()
    assert second.recover_interrupted() == 1
    second.wait_saved()
    (row,) = _saved_rows()
    assert row["id"].endswith("Chamber job (interrupted)") and row["samples"] == 200
    assert not list(experiment_recorder.journal_dir().glob("*.jsonl"))


def test_the_window_joins_the_day_in_memory_and_the_files(deepvac_data_dir):
    now = time.time()
    yesterday = now - 24 * 3600
    day_recorder.write_day(
        day_recorder.day_path("PLC", day_recorder.day_of(yesterday)),
        [yesterday + i for i in range(10)],
        {"temp": [10.0] * 10},
        "PLC",
    )
    recorder = day_recorder.DayRecorder()
    for i in range(5):
        recorder.add("PLC", {"timestamp": now - 5 + i, "temp": 20.0 + i})
    times, values = recorder.window("PLC", yesterday - 1, now + 1)
    assert len(times) == 15
    assert values["temp"][-1] == 24.0 and values["temp"][0] == 10.0
    status = recorder.day_status("PLC")
    assert status["samples"] == 5 and status["pending"]
