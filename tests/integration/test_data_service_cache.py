"""Integration tests for data_service.py's SQLite cache: sync_cache(),
upload_runs(), rename_run(), cached_run_payload(), load_cached_runs().

Every test gets its own fresh sqlite file under tmp_path (via
deepvac_data_dir, which also isolates auth/annotations/backups) and its
own fake "run history" folder (via fake_workspace, which points
DEEPVAC_DATA_ROOT at a tmp_path directory instead of the real
DEEPVAC_WORKSPACE_ROOT-discovered folder) -- never the real 52 MB
deepvac_runs.sqlite3 or a real run-history folder.
"""

import sqlite3
import time

import pytest

from app.services import data_service

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """Points data_service's source-of-truth discovery at an empty tmp_path
    dir instead of whatever real deepvac/scripts/optimization folder this
    machine happens to have. Run folders are created directly under this
    (data_root()/runs_root() both fall back to using the root itself when
    no run_history/ subfolder exists)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("DEEPVAC_DATA_ROOT", str(workspace))
    return workspace


def _write_run(root, name, *, rows=5, start_ts=1700000000, temp_start=20.0, valid_flags=None):
    run_dir = root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = ["run_id,timestamp,start_temp,temp,temp_ref,valid"]
    for i in range(rows):
        flag = "1" if valid_flags is None else valid_flags[i]
        lines.append(f"{name},{start_ts + i * 2},{temp_start},{temp_start + i},60.0,{flag}")
    (run_dir / "run_samples.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_dir


def test_connect_cache_creates_schema(deepvac_data_dir):
    conn = data_service.connect_cache()
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "runs" in tables
        assert "meta" in tables
    finally:
        conn.close()


def test_connect_cache_sets_cache_version_meta_row(deepvac_data_dir):
    conn = data_service.connect_cache()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'cache_version'").fetchone()
        assert row["value"] == str(data_service.CACHE_VERSION)
    finally:
        conn.close()


def test_sync_cache_miss_then_parse_and_insert(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=5)
    result = data_service.sync_cache()
    assert len(result["runs"]) == 1
    assert result["runs"][0]["id"] == "run_a"
    assert result["runs"][0]["samples"] == 5


def test_sync_cache_hit_returns_equivalent_data_without_reparsing(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=5)
    data_service.sync_cache()

    call_count = {"n": 0}
    original = data_service.cache_record_for

    def spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    data_service.cache_record_for = spy
    try:
        result = data_service.sync_cache()  # source files unchanged -> cache hit
    finally:
        data_service.cache_record_for = original

    assert call_count["n"] == 0  # never re-parsed
    assert len(result["runs"]) == 1
    assert result["runs"][0]["id"] == "run_a"


def test_sync_cache_invalidated_when_source_mtime_advances(deepvac_data_dir, fake_workspace):
    run_dir = _write_run(fake_workspace, "run_a", rows=5)
    data_service.sync_cache()

    time.sleep(0.05)
    (run_dir / "run_samples.csv").write_text(
        "run_id,timestamp,start_temp,temp,temp_ref,valid\n"
        "run_a,1700000000,20.0,20.0,60.0,1\n"
        "run_a,1700000002,20.0,21.0,60.0,1\n"
        "run_a,1700000004,20.0,22.0,60.0,1\n",
        encoding="utf-8",
    )

    call_count = {"n": 0}
    original = data_service.cache_record_for

    def spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    data_service.cache_record_for = spy
    try:
        result = data_service.sync_cache()
    finally:
        data_service.cache_record_for = original

    assert call_count["n"] == 1  # re-parsed because the source changed
    assert result["runs"][0]["samples"] == 3


def test_sync_cache_prunes_folder_run_whose_source_was_deleted(deepvac_data_dir, fake_workspace):
    # A second run must survive so active_keys is non-empty -- an *empty*
    # active_keys short-circuits the prune entirely (see the zero-source-
    # files safety test below), which would make this test pass for the
    # wrong reason.
    run_dir = _write_run(fake_workspace, "run_a", rows=5)
    _write_run(fake_workspace, "run_b", rows=3)
    data_service.sync_cache()
    assert len(data_service.load_cached_runs()) == 2

    import shutil

    shutil.rmtree(run_dir)
    result = data_service.sync_cache()
    assert [r["id"] for r in result["runs"]] == ["run_b"]


def test_sync_cache_never_prunes_upload_sourced_rows(deepvac_data_dir, fake_workspace, tmp_path):
    upload_source = tmp_path / "uploaded_elsewhere"
    _write_run(tmp_path, "uploaded_elsewhere", rows=4)
    data_service.upload_runs([str(upload_source)])
    assert len(data_service.load_cached_runs()) == 1

    # An empty folder workspace (no folder-sourced runs at all) must not
    # touch the upload -- sync_cache() only ever prunes source='folder' rows.
    result = data_service.sync_cache()
    assert len(result["runs"]) == 1
    assert result["runs"][0]["source"] == "upload"


def test_sync_cache_zero_source_files_leaves_existing_cache_alone(deepvac_data_dir, fake_workspace):
    run_dir = _write_run(fake_workspace, "run_a", rows=5)
    data_service.sync_cache()
    assert len(data_service.load_cached_runs()) == 1

    # Simulate the workspace becoming unreachable/misconfigured (e.g. a
    # transient mount issue) rather than genuinely emptied -- the documented
    # safety behavior is to leave the cache alone, not wipe it.
    import shutil

    shutil.rmtree(run_dir)
    (fake_workspace / "run_a").mkdir()  # dir exists, but samples file is gone -> zero sources found
    result = data_service.sync_cache()
    assert len(result["runs"]) == 1  # NOT wiped


def test_upload_runs_duplicate_target_name_gets_suffixed_key(deepvac_data_dir, tmp_path):
    source_a = tmp_path / "same_name"
    _write_run(tmp_path, "same_name", rows=3)
    result_a = data_service.upload_runs([str(source_a)])
    key_a = result_a["imported"][0]["key"]

    # Re-upload a *different* folder that happens to share the same leaf name.
    other_root = tmp_path / "other_location"
    other_root.mkdir()
    source_b = _write_run(other_root, "same_name", rows=3)
    result_b = data_service.upload_runs([str(source_b)])
    key_b = result_b["imported"][0]["key"]

    assert key_a != key_b
    assert key_a == "uploads/same_name"
    assert key_b == "uploads/same_name_2"
    assert len(data_service.load_cached_runs()) == 2


def test_upload_runs_no_samples_files_raises_value_error(deepvac_data_dir, tmp_path):
    empty_dir = tmp_path / "nothing_here"
    empty_dir.mkdir()
    with pytest.raises(ValueError):
        data_service.upload_runs([str(empty_dir)])


def test_upload_runs_parent_folder_with_many_run_subfolders(deepvac_data_dir, tmp_path):
    batch_root = tmp_path / "batch"
    _write_run(batch_root, "run_1", rows=3)
    _write_run(batch_root, "run_2", rows=4)
    result = data_service.upload_runs([str(batch_root)])
    assert len(result["imported"]) == 2
    assert {r["samples"] for r in result["imported"]} == {3, 4}


def test_rename_run_updates_display_id_not_key(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=3)
    data_service.sync_cache()
    key = data_service.load_cached_runs()[0]["key"]

    result = data_service.rename_run(key, "  My Renamed Run  ")
    assert result["id"] == "My Renamed Run"  # stripped
    assert result["key"] == key  # lookup key untouched

    runs = data_service.load_cached_runs()
    assert runs[0]["key"] == key
    assert runs[0]["id"] == "My Renamed Run"


def test_rename_run_empty_name_raises():
    with pytest.raises(ValueError):
        data_service.rename_run("some-key", "   ")


def test_rename_run_missing_key_raises_run_not_found(deepvac_data_dir):
    with pytest.raises(data_service.RunNotFound):
        data_service.rename_run("does-not-exist", "New Name")


def test_cached_run_payload_round_trips_json_fields(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=3)
    data_service.sync_cache()
    key = data_service.load_cached_runs()[0]["key"]

    payload = data_service.cached_run_payload(key)
    assert payload["run"]["id"] == "run_a"
    assert len(payload["samples_rows"]) == 3
    assert isinstance(payload["columns"], list)
    assert isinstance(payload["numeric_columns"], list)


def test_cached_run_payload_missing_key_returns_none(deepvac_data_dir):
    assert data_service.cached_run_payload("nope") is None


def test_cached_run_payload_lookup_by_id_or_key(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=3)
    data_service.sync_cache()
    run = data_service.load_cached_runs()[0]
    assert data_service.cached_run_payload(run["key"]) is not None
    assert data_service.cached_run_payload(run["id"]) is not None


def test_reopening_cache_created_by_a_previous_connection_sees_committed_data(deepvac_data_dir):
    conn1 = data_service.connect_cache()
    conn1.execute(
        "INSERT INTO runs (key, id, group_name, root_path, run_path, samples_path, "
        "source_mtime, samples, columns_json, numeric_columns_json, summary_json, "
        "bands_json, annotations_json, samples_json, source, cached_at) VALUES "
        "('k1','id1','g','r','rp','sp',0,1,'[]','[]','{}','[]','[]','[]','folder',datetime('now'))"
    )
    conn1.commit()
    conn1.close()

    conn2 = data_service.connect_cache()
    try:
        row = conn2.execute("SELECT id FROM runs WHERE key = 'k1'").fetchone()
        assert row["id"] == "id1"
    finally:
        conn2.close()


def test_unicode_run_names_round_trip(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "rún_ünïcödé", rows=2)
    result = data_service.sync_cache()
    assert result["runs"][0]["id"] == "rún_ünïcödé"


def test_wal_journal_mode_is_active(deepvac_data_dir):
    conn = data_service.connect_cache()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_two_connections_can_both_read_after_a_commit(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=2)
    data_service.sync_cache()

    conn_a = data_service.connect_cache()
    conn_b = data_service.connect_cache()
    try:
        rows_a = conn_a.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        rows_b = conn_b.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        assert rows_a == rows_b == 1
    finally:
        conn_a.close()
        conn_b.close()


def test_corrupt_cache_file_raises_recoverable_sqlite_error(deepvac_data_dir):
    # Characterizes current behavior: connect_cache() doesn't special-case
    # a corrupt file -- sqlite3 itself raises DatabaseError trying to read
    # it, which is a normal Python exception a caller can catch, not a
    # silent hang or a crash that takes down the whole process.
    data_service.CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    data_service.CACHE_DB.write_bytes(b"this is not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        conn = data_service.connect_cache()
        conn.execute("SELECT * FROM runs").fetchall()


def test_save_monitoring_session_creates_a_run_with_monitoring_source(deepvac_data_dir):
    samples = [{"timestamp": str(1700000000 + i * 2), "temp": str(20.0 + i)} for i in range(5)]
    result = data_service.save_monitoring_session("live-test", samples)
    assert result["id"] == "live-test"

    runs = data_service.load_cached_runs()
    assert len(runs) == 1
    assert runs[0]["source"] == "monitoring"
    assert runs[0]["samples"] == 5
    assert runs[0]["duration_s"] == pytest.approx(8.0)


def test_save_monitoring_session_empty_name_raises(deepvac_data_dir):
    with pytest.raises(ValueError):
        data_service.save_monitoring_session("   ", [{"timestamp": "1700000000", "temp": "20.0"}])


def test_save_monitoring_session_no_samples_raises(deepvac_data_dir):
    with pytest.raises(ValueError):
        data_service.save_monitoring_session("live-test", [])


def test_save_monitoring_session_duplicate_name_gets_suffixed_key(deepvac_data_dir):
    samples = [{"timestamp": "1700000000", "temp": "20.0"}]
    result_a = data_service.save_monitoring_session("live-test", samples)
    result_b = data_service.save_monitoring_session("live-test", samples)
    assert result_a["key"] != result_b["key"]
    assert len(data_service.load_cached_runs()) == 2


def test_save_monitoring_session_tags_chamber_and_test_profile(deepvac_data_dir):
    samples = [{"timestamp": "1700000000", "temp": "20.0"}]
    data_service.save_monitoring_session(
        "live-test", samples, chamber="Chamber 1", test_profile="Thermal Soak A"
    )
    runs = data_service.load_cached_runs()
    assert runs[0]["chamber"] == "Chamber 1"
    assert runs[0]["test_profile"] == "Thermal Soak A"


def test_save_monitoring_session_without_chamber_or_profile_leaves_them_null(deepvac_data_dir):
    samples = [{"timestamp": "1700000000", "temp": "20.0"}]
    data_service.save_monitoring_session("live-test", samples)
    runs = data_service.load_cached_runs()
    assert runs[0]["chamber"] is None
    assert runs[0]["test_profile"] is None


def test_folder_sourced_runs_have_no_chamber_or_test_profile(deepvac_data_dir, fake_workspace):
    _write_run(fake_workspace, "run_a", rows=3)
    data_service.sync_cache()
    runs = data_service.load_cached_runs()
    assert runs[0]["chamber"] is None
    assert runs[0]["test_profile"] is None


def test_save_monitoring_session_is_never_pruned_by_sync_cache(deepvac_data_dir, fake_workspace):
    samples = [{"timestamp": "1700000000", "temp": "20.0"}]
    data_service.save_monitoring_session("live-test", samples)
    # An empty folder workspace must not prune the monitoring-sourced row,
    # same guarantee as source='upload' (see the never-prunes-uploads test above).
    result = data_service.sync_cache()
    assert len(result["runs"]) == 1
    assert result["runs"][0]["source"] == "monitoring"


def test_schema_migration_adds_source_column_to_pre_existing_table(deepvac_data_dir):
    # Characterizes the in-place ALTER TABLE migration: a "runs" table
    # created without the `source` column (as an older cache file would
    # have) gets it added on the next connect_cache(), defaulted to 'folder'.
    data_service.CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_service.CACHE_DB)
    conn.execute(
        """
        CREATE TABLE runs (
            key TEXT PRIMARY KEY, id TEXT NOT NULL, group_name TEXT NOT NULL,
            root_path TEXT NOT NULL, run_path TEXT NOT NULL, samples_path TEXT NOT NULL,
            source_mtime REAL NOT NULL, samples INTEGER NOT NULL, duration_s REAL, mae REAL,
            cost REAL, tail_mae REAL, overshoot REAL, settle_time_s REAL, start_time TEXT,
            end_time TEXT, columns_json TEXT NOT NULL, numeric_columns_json TEXT NOT NULL,
            summary_json TEXT NOT NULL, bands_json TEXT NOT NULL, annotations_json TEXT NOT NULL,
            samples_json TEXT NOT NULL, cached_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO runs VALUES ('k','id','g','r','rp','sp',0,1,NULL,NULL,NULL,NULL,NULL,"
        "NULL,NULL,NULL,'[]','[]','{}','[]','[]','[]',datetime('now'))"
    )
    conn.commit()
    conn.close()

    conn2 = data_service.connect_cache()
    try:
        row = conn2.execute("SELECT source FROM runs WHERE key = 'k'").fetchone()
        assert row["source"] == "folder"
    finally:
        conn2.close()
