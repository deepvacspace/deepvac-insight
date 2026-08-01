"""Persistent alarm rules and alarm history for Live Monitoring, in their
own local SQLite database -- rules and past events both survive restarts
(unlike the in-memory-only alarm list this replaces).

Two tables: alarm_rules (the persistent definition: name, variable,
condition, threshold(s), severity, deadband, delay, and which chamber it
belongs to) and alarm_events (one row per trigger, with
acknowledgement/comment fields and a cleared_at timestamp set when the
condition stops holding).

Rules are scoped per-chamber, not shared -- chamber_id ties a rule to one
row in chambers_service's chambers table. chamber_name is denormalized
onto both alarm_rules and alarm_events at write time (same pattern
annotations_service.py uses for its creator-name snapshot) so a rule's or
event's chamber still displays correctly even if that chamber is later
renamed or deleted.

Runtime-only evaluation state (is it *currently* active right now, given
the live samples flowing in) is NOT stored here -- see
services/chamber_session.py's ChamberSession._evaluate_alarms(), which
keeps that in memory alongside each loaded rule dict and calls
record_trigger()/record_clear() at the moments those transitions actually
happen.
"""

import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR

ALARMS_DB = DATA_DIR / "deepvac_alarms.sqlite3"

CONDITIONS = ["above", "below", "outside range"]
SEVERITIES = ["Info", "Warning", "Critical"]


def connect_alarms(db_path=None):
    """db_path overrides ALARMS_DB for this call only -- see
    auth_service.connect_auth()'s docstring for why the other functions in
    this file use the module-level constant implicitly instead."""
    path = db_path or ALARMS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            variable TEXT NOT NULL,
            condition TEXT NOT NULL,
            value REAL NOT NULL,
            value2 REAL,
            severity TEXT NOT NULL,
            deadband REAL NOT NULL DEFAULT 0,
            delay_s REAL NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL DEFAULT 'Unknown',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER,
            rule_name TEXT NOT NULL,
            variable TEXT NOT NULL,
            severity TEXT NOT NULL,
            trigger_value REAL,
            triggered_at TEXT NOT NULL,
            cleared_at TEXT,
            acknowledged_at TEXT,
            acknowledged_by TEXT,
            comment TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_triggered_at ON alarm_events(triggered_at)")

    rule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alarm_rules)")}
    if "chamber_id" not in rule_columns:
        conn.execute("ALTER TABLE alarm_rules ADD COLUMN chamber_id INTEGER")
    if "chamber_name" not in rule_columns:
        conn.execute("ALTER TABLE alarm_rules ADD COLUMN chamber_name TEXT")

    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alarm_events)")}
    if "chamber_id" not in event_columns:
        conn.execute("ALTER TABLE alarm_events ADD COLUMN chamber_id INTEGER")
    if "chamber_name" not in event_columns:
        conn.execute("ALTER TABLE alarm_events ADD COLUMN chamber_name TEXT")

    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _rule_row(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "variable": row["variable"],
        "condition": row["condition"],
        "value": row["value"],
        "value2": row["value2"],
        "severity": row["severity"],
        "deadband": row["deadband"],
        "delay_s": row["delay_s"],
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "chamber_id": row["chamber_id"],
        "chamber_name": row["chamber_name"],
    }


def _event_row(row):
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "rule_name": row["rule_name"],
        "variable": row["variable"],
        "severity": row["severity"],
        "trigger_value": row["trigger_value"],
        "triggered_at": row["triggered_at"],
        "cleared_at": row["cleared_at"],
        "acknowledged_at": row["acknowledged_at"],
        "acknowledged_by": row["acknowledged_by"],
        "comment": row["comment"],
        "chamber_id": row["chamber_id"],
        "chamber_name": row["chamber_name"],
    }


def list_rules(chamber_id=None):
    """chamber_id=None returns every rule regardless of chamber (used by
    any future cross-chamber admin view); pass it to scope to one
    chamber's rules -- this is what every chamber tab does."""
    conn = connect_alarms()
    try:
        if chamber_id is None:
            rows = conn.execute("SELECT * FROM alarm_rules ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alarm_rules WHERE chamber_id = ? ORDER BY id", (chamber_id,)
            ).fetchall()
        return [_rule_row(r) for r in rows]
    finally:
        conn.close()


def add_rule(
    name,
    variable,
    condition,
    value,
    value2,
    severity,
    deadband=0.0,
    delay_s=0.0,
    created_by="Unknown",
    chamber_id=None,
    chamber_name=None,
):
    conn = connect_alarms()
    try:
        cur = conn.execute(
            """
            INSERT INTO alarm_rules
                (name, variable, condition, value, value2, severity, deadband, delay_s,
                 enabled, created_by, created_at, chamber_id, chamber_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                str(name),
                str(variable),
                str(condition),
                float(value),
                float(value2) if value2 is not None else None,
                str(severity),
                float(deadband or 0.0),
                float(delay_s or 0.0),
                created_by or "Unknown",
                _now(),
                chamber_id,
                chamber_name,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM alarm_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _rule_row(row)
    finally:
        conn.close()


def delete_rule(rule_id):
    conn = connect_alarms()
    try:
        conn.execute("DELETE FROM alarm_rules WHERE id = ?", (rule_id,))
        conn.commit()
    finally:
        conn.close()


def record_trigger(rule, trigger_value):
    """Inserts a new alarm_events row for a rule that just transitioned
    inactive -> active. Returns the new event's id (needed by
    record_clear() later for this same episode). chamber_id/chamber_name
    are copied from the rule dict so the event still shows the right
    chamber even if that chamber is later renamed or deleted."""
    conn = connect_alarms()
    try:
        cur = conn.execute(
            """
            INSERT INTO alarm_events
                (rule_id, rule_name, variable, severity, trigger_value, triggered_at, comment,
                 chamber_id, chamber_name)
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                rule.get("id"),
                rule["name"],
                rule["variable"],
                rule["severity"],
                float(trigger_value) if trigger_value is not None else None,
                _now(),
                rule.get("chamber_id"),
                rule.get("chamber_name"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_clear(event_id):
    if event_id is None:
        return
    conn = connect_alarms()
    try:
        conn.execute(
            "UPDATE alarm_events SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
            (_now(), event_id),
        )
        conn.commit()
    finally:
        conn.close()


def acknowledge_event(event_id, user_name, comment=""):
    conn = connect_alarms()
    try:
        conn.execute(
            "UPDATE alarm_events SET acknowledged_at = ?, acknowledged_by = ?, comment = ? "
            "WHERE id = ?",
            (_now(), user_name or "Unknown", comment or "", event_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_events(limit=500, chamber_id=None):
    conn = connect_alarms()
    try:
        if chamber_id is None:
            rows = conn.execute(
                "SELECT * FROM alarm_events ORDER BY triggered_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alarm_events WHERE chamber_id = ? ORDER BY triggered_at DESC LIMIT ?",
                (chamber_id, limit),
            ).fetchall()
        return [_event_row(r) for r in rows]
    finally:
        conn.close()


def export_events_csv(path, events=None):
    import csv as csv_module

    rows = events if events is not None else list_events(limit=100_000)
    fieldnames = [
        "id",
        "rule_name",
        "variable",
        "severity",
        "trigger_value",
        "triggered_at",
        "cleared_at",
        "acknowledged_at",
        "acknowledged_by",
        "comment",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv_module.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
