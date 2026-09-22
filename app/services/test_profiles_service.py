"""Test Profiles -- reusable, named multi-step experiment definitions (a
sequence of temperature/pressure setpoints, each held for a duration), in
their own local SQLite database.

Replaces the "Recipe" concept the Dashboard's filter row used to have a
placeholder for: unlike a recipe (never modeled anywhere else in this
app), a test profile is a real, editable schedule you can create here and
then run against a connected chamber from Live Monitoring (see
views/monitoring.py's step-sequencer, which sends each step's setpoint via
services/tcp_client.ChamberConnection.send_command() as its turn comes up).

Two tables: test_profiles (name, description) and test_profile_steps (one
row per ordered step: setpoint_temp and/or setpoint_pressure, duration_s,
an optional label) -- a step needs at least one setpoint set; a step with
neither would just be "wait", which isn't useful on its own here.
"""

import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR

TEST_PROFILES_DB = DATA_DIR / "deepvac_test_profiles.sqlite3"


class TestProfileError(ValueError):
    pass


_HUB_SYNC_COLUMNS = ("hub_id", "hub_synced_at")


def _ensure_hub_sync_columns(conn):
    """Adds any missing hub_* sync columns to the test_profiles table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(test_profiles)")}
    for column in _HUB_SYNC_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE test_profiles ADD COLUMN {column} TEXT")


def connect_test_profiles(db_path=None):
    """db_path overrides TEST_PROFILES_DB for this call only -- see
    auth_service.connect_auth()'s docstring for why the other functions in
    this file use the module-level constant implicitly instead."""
    path = db_path or TEST_PROFILES_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT 'Unknown',
            created_at TEXT NOT NULL,
            hub_id TEXT UNIQUE,
            hub_synced_at TEXT
        )
        """
    )
    _ensure_hub_sync_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_profile_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES test_profiles(id) ON DELETE CASCADE,
            step_order INTEGER NOT NULL,
            setpoint_temp REAL,
            setpoint_pressure REAL,
            duration_s REAL NOT NULL,
            label TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_steps_profile_order "
        "ON test_profile_steps(profile_id, step_order)"
    )
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _step_row(row):
    return {
        "id": row["id"],
        "step_order": row["step_order"],
        "setpoint_temp": row["setpoint_temp"],
        "setpoint_pressure": row["setpoint_pressure"],
        "duration_s": row["duration_s"],
        "label": row["label"],
    }


def _profile_row(conn, row):
    steps = conn.execute(
        "SELECT * FROM test_profile_steps WHERE profile_id = ? ORDER BY step_order",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "hub_id": row["hub_id"],
        "hub_synced_at": row["hub_synced_at"],
        "steps": [_step_row(s) for s in steps],
    }


def list_profiles():
    conn = connect_test_profiles()
    try:
        rows = conn.execute("SELECT * FROM test_profiles ORDER BY name").fetchall()
        return [_profile_row(conn, r) for r in rows]
    finally:
        conn.close()


def get_profile(profile_id):
    conn = connect_test_profiles()
    try:
        row = conn.execute("SELECT * FROM test_profiles WHERE id = ?", (profile_id,)).fetchone()
        return _profile_row(conn, row) if row else None
    finally:
        conn.close()


def _validate(name, steps):
    if not name or not name.strip():
        raise TestProfileError("Name is required.")
    if not steps:
        raise TestProfileError("A test profile needs at least one step.")
    for i, step in enumerate(steps, start=1):
        temp = step.get("setpoint_temp")
        pressure = step.get("setpoint_pressure")
        if temp is None and pressure is None:
            raise TestProfileError(
                f"Step {i} needs at least one setpoint (temperature or pressure)."
            )
        duration = step.get("duration_s")
        try:
            duration_val = float(duration)
        except (TypeError, ValueError) as exc:
            raise TestProfileError(f"Step {i} needs a numeric duration.") from exc
        if duration_val <= 0:
            raise TestProfileError(f"Step {i}'s duration must be greater than 0 seconds.")


def _insert_steps(conn, profile_id, steps):
    for order, step in enumerate(steps):
        conn.execute(
            """
            INSERT INTO test_profile_steps
                (profile_id, step_order, setpoint_temp, setpoint_pressure, duration_s, label)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                order,
                float(step["setpoint_temp"]) if step.get("setpoint_temp") is not None else None,
                float(step["setpoint_pressure"])
                if step.get("setpoint_pressure") is not None
                else None,
                float(step["duration_s"]),
                str(step.get("label") or ""),
            ),
        )


def add_profile(name, description, steps, created_by="Unknown", hub_id=None):
    _validate(name, steps)
    conn = connect_test_profiles()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO test_profiles (name, description, created_by, created_at, hub_id, hub_synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    name.strip(),
                    description or "",
                    created_by or "Unknown",
                    _now(),
                    hub_id,
                    _now() if hub_id else None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise TestProfileError(f"A test profile named '{name}' already exists.") from exc
        profile_id = cur.lastrowid
        _insert_steps(conn, profile_id, steps)
        conn.commit()
        return get_profile(profile_id)
    finally:
        conn.close()


def get_profile_by_hub_id(hub_id):
    conn = connect_test_profiles()
    try:
        row = conn.execute("SELECT * FROM test_profiles WHERE hub_id = ?", (hub_id,)).fetchone()
        return _profile_row(conn, row) if row else None
    finally:
        conn.close()


def mark_hub_synced(profile_id, hub_id):
    conn = connect_test_profiles()
    try:
        conn.execute(
            "UPDATE test_profiles SET hub_id = ?, hub_synced_at = ? WHERE id = ?",
            (hub_id, _now(), profile_id),
        )
        conn.commit()
        return get_profile(profile_id)
    finally:
        conn.close()


def update_profile(profile_id, name, description, steps):
    _validate(name, steps)
    conn = connect_test_profiles()
    try:
        try:
            cur = conn.execute(
                "UPDATE test_profiles SET name = ?, description = ? WHERE id = ?",
                (name.strip(), description or "", profile_id),
            )
        except sqlite3.IntegrityError as exc:
            raise TestProfileError(f"A test profile named '{name}' already exists.") from exc
        if cur.rowcount == 0:
            # Check before _insert_steps() below would otherwise fail with a
            # raw, less helpful sqlite3.IntegrityError (FK violation) for
            # the same underlying reason -- no such profile to attach to.
            raise TestProfileError("Test profile not found.")
        conn.execute("DELETE FROM test_profile_steps WHERE profile_id = ?", (profile_id,))
        _insert_steps(conn, profile_id, steps)
        conn.commit()
        return get_profile(profile_id)
    finally:
        conn.close()


def touch_hub_synced(profile_id):
    conn = connect_test_profiles()
    try:
        conn.execute(
            "UPDATE test_profiles SET hub_synced_at = ? WHERE id = ?", (_now(), profile_id)
        )
        conn.commit()
    finally:
        conn.close()


def delete_profile(profile_id):
    conn = connect_test_profiles()
    try:
        conn.execute("DELETE FROM test_profiles WHERE id = ?", (profile_id,))
        conn.commit()
    finally:
        conn.close()


def total_duration_s(profile):
    return sum(step["duration_s"] for step in profile["steps"])
