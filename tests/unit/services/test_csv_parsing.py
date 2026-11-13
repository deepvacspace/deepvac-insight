"""Tests of CSV parsing and data transforms."""

from pathlib import Path

import pytest

from app.services import data_service

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures/runs"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("3.14", 3.14),
        (3, 3.0),
        ("", None),
        (None, None),
        ("not a number", None),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("  ", None),
        ("0", 0.0),
        ("-5.5", -5.5),
    ],
)
def test_to_float(value, expected):
    result = data_service.to_float(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_first_float_returns_first_parseable_value():
    row = {"a": "", "b": "not a number", "c": "3.5", "d": "999"}
    assert data_service.first_float(row, ["a", "b", "c", "d"]) == pytest.approx(3.5)


def test_first_float_returns_none_when_nothing_parses():
    row = {"a": "", "b": "nope"}
    assert data_service.first_float(row, ["a", "b", "missing"]) is None


def test_format_ts_epoch_seconds():
    formatted = data_service.format_ts(1700000000)
    assert formatted.endswith("UTC")
    assert "2023-11-14" in formatted


def test_format_ts_passthrough_for_non_numeric():
    assert data_service.format_ts("already-a-label") == "already-a-label"


def test_format_ts_empty_value():
    assert data_service.format_ts(None) == ""
    assert data_service.format_ts("") == ""


def test_csv_dicts_valid_realistic_file():
    rows = data_service.csv_dicts(FIXTURES / "valid_run" / "run_samples.csv")
    assert len(rows) == 10
    assert rows[0]["run_id"] == "valid_run"
    assert set(rows[0].keys()) == {
        "run_id",
        "timestamp",
        "start_temp",
        "temp",
        "temp_ref",
        "temp_u",
        "temp_u_p",
        "temp_u_i",
        "temp_u_d",
        "kp",
        "ki",
        "kd",
        "valid",
    }


def test_csv_dicts_missing_file_returns_empty_list():
    assert data_service.csv_dicts(FIXTURES / "does_not_exist.csv") == []


def test_csv_dicts_empty_file(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    assert data_service.csv_dicts(path) == []


def test_csv_dicts_header_only_file(tmp_path):
    path = tmp_path / "header_only.csv"
    path.write_text("run_id,timestamp,temp\n", encoding="utf-8")
    assert data_service.csv_dicts(path) == []


def test_csv_dicts_missing_column_leaves_it_absent_per_row(tmp_path):
    path = tmp_path / "no_temp_ref.csv"
    path.write_text("run_id,timestamp,temp\nr1,1700000000,20.0\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert "temp_ref" not in rows[0]


def test_csv_dicts_short_row_gets_none_for_missing_trailing_fields(tmp_path):
    path = tmp_path / "short_row.csv"
    path.write_text("run_id,timestamp,temp\nr1,1700000000\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert rows[0]["temp"] is None


def test_csv_dicts_unexpected_extra_column_is_grouped_under_none_key(tmp_path):
    path = tmp_path / "extra_column.csv"
    path.write_text("run_id,temp\nr1,20.0,unexpected_extra\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert rows[0][None] == ["unexpected_extra"]


def test_csv_dicts_invalid_numeric_value_parses_as_none_via_to_float(tmp_path):
    path = tmp_path / "bad_numeric.csv"
    path.write_text("run_id,temp\nr1,not-a-number\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert data_service.to_float(rows[0]["temp"]) is None


def test_csv_dicts_blank_values(tmp_path):
    path = tmp_path / "blanks.csv"
    path.write_text("run_id,temp\nr1,\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert rows[0]["temp"] == ""
    assert data_service.to_float(rows[0]["temp"]) is None


def test_csv_dicts_unicode_values_and_filename(tmp_path):
    unicode_dir = tmp_path / "rún_ünïcödé"
    unicode_dir.mkdir()
    path = unicode_dir / "run_samples.csv"
    path.write_text("run_id,label\nr1,Café ☕\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert rows[0]["label"] == "Café ☕"


def test_csv_dicts_path_containing_spaces(tmp_path):
    spaced_dir = tmp_path / "folder with spaces"
    spaced_dir.mkdir()
    path = spaced_dir / "run_samples.csv"
    path.write_text("run_id,temp\nr1,20.0\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert rows[0]["temp"] == "20.0"


def test_csv_dicts_duplicate_samples_are_kept_as_separate_rows(tmp_path):
    path = tmp_path / "dupes.csv"
    path.write_text("run_id,timestamp,temp\nr1,100,20.0\nr1,100,20.0\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert len(rows) == 2
    assert rows[0] == rows[1]


def test_csv_dicts_bom_is_stripped_by_utf_8_sig_encoding(tmp_path):
    path = tmp_path / "with_bom.csv"
    path.write_bytes("run_id,temp\r\nr1,20.0\r\n".encode("utf-8-sig"))
    rows = data_service.csv_dicts(path)
    assert list(rows[0].keys())[0] == "run_id"


def test_csv_dicts_large_synthetic_file_parses_fully(tmp_path):
    path = tmp_path / "large.csv"
    lines = ["run_id,timestamp,temp"] + [
        f"r1,{1700000000 + i * 2},{20.0 + i * 0.01}" for i in range(5000)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows = data_service.csv_dicts(path)
    assert len(rows) == 5000
    assert rows[-1]["timestamp"] == str(1700000000 + 4999 * 2)


def test_numeric_columns_excludes_run_id_timestamp_start_temp():
    rows = data_service.csv_dicts(FIXTURES / "valid_run" / "run_samples.csv")
    numeric = data_service.numeric_columns(rows)
    assert "run_id" not in numeric
    assert "timestamp" not in numeric
    assert "start_temp" not in numeric
    assert "temp" in numeric
    assert "kp" in numeric


def test_numeric_columns_empty_rows_returns_empty_list():
    assert data_service.numeric_columns([]) == []


def test_numeric_columns_all_non_numeric_column_excluded():
    rows = [
        {"run_id": "r1", "label": "abc"},
        {"run_id": "r2", "label": "def"},
    ]
    assert data_service.numeric_columns(rows) == []


def test_run_record_reads_summary_fields():
    samples_path = FIXTURES / "valid_run" / "run_samples.csv"
    record = data_service.run_record(samples_path, key="valid_run", group="fixtures")
    assert record["key"] == "valid_run"
    assert record["id"] == "valid_run"
    assert record["samples"] == 10
    assert record["duration_s"] == pytest.approx(18.0)
    assert record["cost"] == pytest.approx(45.2)
    assert record["tail_mae"] == pytest.approx(32.9)
    assert record["overshoot"] == pytest.approx(0.0)
    assert record["settle_time_s"] == pytest.approx(999.0)
    assert "2023-11-14" in record["start_time"]


def test_run_record_falls_back_to_counting_rows_when_summary_lacks_num_samples(tmp_path):
    run_dir = tmp_path / "no_summary_count"
    run_dir.mkdir()
    (run_dir / "run_samples.csv").write_text(
        "run_id,temp\nr1,20.0\nr1,21.0\nr1,22.0\n", encoding="utf-8"
    )
    record = data_service.run_record(run_dir / "run_samples.csv", key="k", group="g")
    assert record["samples"] == 3


def test_elapsed_x_relative_to_first_timestamp():
    rows = [{"timestamp": "100"}, {"timestamp": "102"}, {"timestamp": "110"}]
    assert data_service.elapsed_x(rows) == [0.0, 2.0, 10.0]


def test_elapsed_x_falls_back_to_index_when_no_timestamps():
    rows = [{"temp": "20"}, {"temp": "21"}, {"temp": "22"}]
    assert data_service.elapsed_x(rows) == [0.0, 1.0, 2.0]


def test_elapsed_x_empty_rows():
    assert data_service.elapsed_x([]) == []


def test_run_annotations_includes_start_and_target():
    samples = data_service.csv_dicts(FIXTURES / "valid_run" / "run_samples.csv")
    summary = data_service.read_summary(FIXTURES / "valid_run")
    bands = data_service.csv_dicts(FIXTURES / "valid_run" / "band_metrics.csv")
    annotations = data_service.run_annotations(samples, summary, bands)
    kinds = {a["kind"] for a in annotations}
    assert "start" in kinds
    assert "target" in kinds
    assert "settling" in kinds
    assert "pid" in kinds


def test_run_annotations_empty_samples_returns_empty_list():
    assert data_service.run_annotations([], {}, []) == []


def test_run_annotations_invalid_region_detected():
    samples = [
        {"timestamp": "100", "temp": "20", "temp_ref": "60", "valid": "1"},
        {"timestamp": "102", "temp": "21", "temp_ref": "60", "valid": "0"},
        {"timestamp": "104", "temp": "22", "temp_ref": "60", "valid": "0"},
        {"timestamp": "106", "temp": "23", "temp_ref": "60", "valid": "1"},
    ]
    annotations = data_service.run_annotations(samples, {}, [])
    invalid_regions = [a for a in annotations if a["kind"] == "invalid"]
    assert len(invalid_regions) == 1
    assert invalid_regions[0]["x0"] == pytest.approx(2.0)
    assert invalid_regions[0]["x1"] == pytest.approx(6.0)


def test_run_annotations_invalid_region_open_at_end_of_samples():
    samples = [
        {"timestamp": "100", "temp": "20", "temp_ref": "60", "valid": "1"},
        {"timestamp": "102", "temp": "21", "temp_ref": "60", "valid": "0"},
    ]
    annotations = data_service.run_annotations(samples, {}, [])
    invalid_regions = [a for a in annotations if a["kind"] == "invalid"]
    assert len(invalid_regions) == 1
    assert invalid_regions[0]["x1"] == pytest.approx(2.0)


def test_downsample_below_threshold_is_unchanged():
    rows = list(range(100))
    assert data_service.downsample(rows, max_points=1800) == rows


def test_downsample_above_threshold_reduces_row_count():
    rows = list(range(5000))
    result = data_service.downsample(rows, max_points=1000)
    assert len(result) <= 1000
    assert result[0] == 0


def test_downsample_preserves_order():
    rows = list(range(3000))
    result = data_service.downsample(rows, max_points=500)
    assert result == sorted(result)
