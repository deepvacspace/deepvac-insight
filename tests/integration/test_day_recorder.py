"""Tests of the full-day recordings."""

import importlib.util
import math
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.api.analyst_tools import AnalystTools
from app.api.chambers import ChamberLinks
from app.services import chambers_service, day_recorder

pytestmark = pytest.mark.integration


def _noon(days_ago=0):
    day = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
        days=days_ago
    )
    return day.timestamp()


def test_the_file_is_the_format_deepvac_control_reads(deepvac_data_dir):
    path = deepvac_data_dir / "day.parquet"
    times = [1780000000.25 + i for i in range(100)]
    temp = [20.0 + i / 10 for i in range(100)]
    temp[5] = math.nan
    day_recorder.write_day(
        path,
        times,
        {"temp": temp, "status_condition": [1.0] * 100},
        "PLC",
        {"temp": {"label": "Temperature", "unit": "°C"}},
    )
    table = pq.read_table(path)
    schema = table.schema
    assert schema.field("timestamp").type == pa.timestamp("us", tz="UTC")
    assert all(not f.nullable for f in schema) and schema.field("temp").type == pa.float64()
    meta = {k.decode(): v.decode() for k, v in schema.metadata.items()}
    assert meta["deepvac.format"] == "deepvac.recording/1" and meta["deepvac.chamber"] == "PLC"
    assert '"unit": "°C"' in meta["deepvac.channels"]
    column = pq.ParquetFile(path).metadata.row_group(0).column(1)
    assert column.compression == "GZIP" and not column.has_dictionary_page
    assert "PLAIN" in column.encodings and not any("DICT" in e for e in column.encodings)
    read_times, values, _ = day_recorder.read_day(path)
    assert read_times[0] == pytest.approx(1780000000.25, abs=1e-6)
    assert math.isnan(values["temp"][5]) and values["temp"][6] == pytest.approx(20.6)


def test_recorder_keeps_the_day_and_picks_it_up_again(deepvac_data_dir):
    t0 = _noon()
    recorder = day_recorder.DayRecorder()
    assert recorder.add("PLC", {"timestamp": t0, "temp": 20.0})
    assert recorder.add("PLC", {"timestamp": t0 + 1, "temp": 21.0, "temp_ref": 30.0})
    assert not recorder.add("PLC", {"timestamp": t0 + 1, "temp": 99.0})
    assert not recorder.add("PLC", {"temp": 1.0})
    path = day_recorder.day_path("PLC", day_recorder.day_of(t0))
    assert not path.exists()
    recorder.flush()
    times, values, _ = day_recorder.read_day(path)
    assert len(times) == 2 and math.isnan(values["temp_ref"][0]) and values["temp_ref"][1] == 30.0

    again = day_recorder.DayRecorder()
    again.add("PLC", {"timestamp": t0 + 2, "temp": 22.0, "temp_ref": 30.0})
    again.flush()
    times, values, _ = day_recorder.read_day(path)
    assert list(values["temp"]) == [20.0, 21.0, 22.0]


def test_a_new_day_starts_a_new_file(deepvac_data_dir):
    recorder = day_recorder.DayRecorder()
    yesterday, today = _noon(1), _noon()
    recorder.add("PLC", {"timestamp": yesterday, "temp": 1.0})
    recorder.add("PLC", {"timestamp": today, "temp": 2.0})
    recorder.flush()
    days = day_recorder.list_days("PLC")
    assert [d["day"] for d in days] == [
        f"{day_recorder.day_of(yesterday):%Y-%m-%d}",
        f"{day_recorder.day_of(today):%Y-%m-%d}",
    ]
    times, values = day_recorder.read_range("PLC", yesterday - 10, today + 10)
    assert list(values["temp"]) == [1.0, 2.0]


def test_a_damaged_day_file_is_kept_aside(deepvac_data_dir):
    t0 = _noon()
    path = day_recorder.day_path("PLC", day_recorder.day_of(t0))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not parquet")
    recorder = day_recorder.DayRecorder()
    recorder.add("PLC", {"timestamp": t0, "temp": 5.0})
    recorder.flush()
    assert path.with_name(path.name + ".bad").read_bytes() == b"not parquet"
    assert list(day_recorder.read_day(path)[1]["temp"]) == [5.0]


def _fake_plc():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_plc.py"
    spec = importlib.util.spec_from_file_location("fake_plc_recording", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_feed_records_the_plc(deepvac_data_dir):
    fake = _fake_plc()
    chamber = fake.Chamber(24.0)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

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
    chambers_service.add_chamber("Fake PLC", "127.0.0.1", server.getsockname()[1], protocol="plc")
    links = ChamberLinks(connect_timeout_s=5)
    recorder = day_recorder.DayRecorder(
        labels=lambda name: {"temp": {"label": "Temperature", "unit": "°C"}}
    )
    feed = day_recorder.RecordingFeed(links, recorder)
    try:
        feed.poll_once()
        deadline = time.monotonic() + 8
        added = 0
        while added < 20 and time.monotonic() < deadline:
            time.sleep(0.2)
            added += feed.poll_once()
        assert added >= 20
        assert feed.poll_once() < 10
        feed.stop()
        days = day_recorder.list_days("Fake PLC")
        times, values, meta = day_recorder.read_day(days[-1]["path"])
        assert len(times) >= 20 and {"temp", "temp_ref", "status_condition"} <= set(values)
        assert np.all(np.diff(times) > 0) and '"Temperature"' in meta["deepvac.channels"]
    finally:
        links.close()
        server.close()


def test_the_analyst_reads_a_day(deepvac_data_dir):
    t0 = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
    recorder = day_recorder.DayRecorder()
    for i in range(600):
        if 200 <= i < 260:
            continue
        status = 2.0 if 100 <= i < 400 else 1.0
        recorder.add(
            "PLC",
            {
                "timestamp": t0 + i,
                "temp": 20 + i / 100,
                "temp_ref": 30.0 if i >= 100 else 20.0,
                "status_condition": status,
            },
        )
    recorder.flush()
    tools = AnalystTools()
    listed = tools.list_recordings()
    assert listed["recordings"][0]["chamber"] == "PLC" and listed["recordings"][0]["samples"] == 540
    day = f"{datetime.fromtimestamp(t0):%Y-%m-%d}"
    result = tools.get_recording("", day)
    assert result["samples"] == 540 and result["sample_period_s"] == 1.0
    assert [c["to"] for c in result["status_changes"]] == ["work", "idle"]
    assert result["setpoint_changes"][0]["to"] == 30.0
    assert len(result["gaps"]) == 1 and result["gaps"][0]["minutes"] == pytest.approx(
        61 / 60, abs=0.01
    )
    assert result["channels"]["temp"]["min"] == 20.0
    narrow = tools.get_recording("PLC", f"{day} 08:01", f"{day} 08:02", ["temp", "pressure"])
    assert narrow["samples"] == 61 and narrow["unknown_channels"] == ["pressure"]
    with pytest.raises(ValueError, match="No recordings of 'Other'"):
        tools.list_recordings("Other")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tools.get_recording("PLC", "yesterday")
