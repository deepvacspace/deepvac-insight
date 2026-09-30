"""Local read-only cache of the run catalog shared through the collaboration
host, in its own local SQLite database."""

import json
import sqlite3

from app.common import DATA_DIR

SHARED_RUNS_DB = DATA_DIR / "deepvac_shared_runs.sqlite3"


def connect_shared_runs(db_path=None):
    path = db_path or SHARED_RUNS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS shared_runs (run_uid TEXT PRIMARY KEY, data TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def replace_all(items):
    """Replaces the cache with items, each a dict with a "uid" and the run's metadata."""
    conn = connect_shared_runs()
    try:
        conn.execute("DELETE FROM shared_runs")
        conn.executemany(
            "INSERT INTO shared_runs (run_uid, data) VALUES (?, ?)",
            [
                (item["uid"], json.dumps({k: v for k, v in item.items() if k != "uid"}))
                for item in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


def list_runs():
    conn = connect_shared_runs()
    try:
        rows = conn.execute("SELECT run_uid, data FROM shared_runs").fetchall()
        runs = [{"run_uid": row["run_uid"], **json.loads(row["data"])} for row in rows]
        return sorted(runs, key=lambda run: run.get("start_time") or "", reverse=True)
    finally:
        conn.close()
