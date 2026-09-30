"""User-created chart annotations and variable rules for runs, in their own
local SQLite database. Each row is linked to the run it was made on (by
run key) and the user who made it (id + a denormalized name snapshot for
display, so authorship still reads correctly even if that user later
renames themselves)."""

import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR

ANNOTATIONS_DB = DATA_DIR / "deepvac_annotations.sqlite3"


def connect_annotations(db_path=None):
    """db_path overrides ANNOTATIONS_DB for this call only -- see
    auth_service.connect_auth()'s docstring for the module-level-constant
    seam every other function in this file uses implicitly instead."""
    path = db_path or ANNOTATIONS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT NOT NULL DEFAULT 'Unknown',
            x0 REAL NOT NULL,
            x1 REAL NOT NULL,
            label TEXT NOT NULL,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annotations_run ON annotations(run_key)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS variable_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT NOT NULL DEFAULT 'Unknown',
            name TEXT NOT NULL,
            channel TEXT NOT NULL,
            lo REAL,
            hi REAL,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_run ON variable_rules(run_key)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS collab_tombstones "
        "(uid TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'annotation')"
    )

    for table in ("annotations", "variable_rules"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "collab_uid" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN collab_uid TEXT")
    tombstone_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(collab_tombstones)")
    }
    if "kind" not in tombstone_columns:
        conn.execute(
            "ALTER TABLE collab_tombstones ADD COLUMN kind TEXT NOT NULL DEFAULT 'annotation'"
        )
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _annotation_row(row):
    return {
        "id": row["id"],
        "run_key": row["run_key"],
        "user_id": row["user_id"],
        "user_name": row["user_name"],
        "x0": row["x0"],
        "x1": row["x1"],
        "label": row["label"],
        "color": row["color"],
        "created_at": row["created_at"],
        "collab_uid": row["collab_uid"],
    }


def _rule_row(row):
    return {
        "id": row["id"],
        "run_key": row["run_key"],
        "user_id": row["user_id"],
        "user_name": row["user_name"],
        "name": row["name"],
        "channel": row["channel"],
        "lo": row["lo"],
        "hi": row["hi"],
        "color": row["color"],
        "created_at": row["created_at"],
        "collab_uid": row["collab_uid"],
    }


def list_annotations(run_key):
    conn = connect_annotations()
    try:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE run_key = ? ORDER BY x0", (run_key,)
        ).fetchall()
        return [_annotation_row(r) for r in rows]
    finally:
        conn.close()


def add_annotation(
    run_key, user_id, user_name, x0, x1, label, color, collab_uid=None, created_at=None
):
    conn = connect_annotations()
    try:
        cur = conn.execute(
            """
            INSERT INTO annotations
                (run_key, user_id, user_name, x0, x1, label, color, created_at, collab_uid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_key,
                user_id,
                user_name or "Unknown",
                float(x0),
                float(x1),
                str(label),
                str(color),
                created_at or _now(),
                collab_uid,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM annotations WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _annotation_row(row)
    finally:
        conn.close()


def delete_annotation(annotation_id):
    """Deletes an annotation, remembering a tombstone if it was shared."""
    conn = connect_annotations()
    try:
        row = conn.execute(
            "SELECT collab_uid FROM annotations WHERE id = ?", (annotation_id,)
        ).fetchone()
        conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        if row is not None and row["collab_uid"]:
            conn.execute(
                "INSERT OR IGNORE INTO collab_tombstones (uid, kind) VALUES (?, 'annotation')",
                (row["collab_uid"],),
            )
        conn.commit()
    finally:
        conn.close()


def delete_annotation_without_tombstone(annotation_id):
    conn = connect_annotations()
    try:
        conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        conn.commit()
    finally:
        conn.close()


def list_all_annotations():
    conn = connect_annotations()
    try:
        rows = conn.execute("SELECT * FROM annotations ORDER BY id").fetchall()
        return [_annotation_row(r) for r in rows]
    finally:
        conn.close()


def set_collab_uid(annotation_id, collab_uid):
    conn = connect_annotations()
    try:
        conn.execute(
            "UPDATE annotations SET collab_uid = ? WHERE id = ?", (collab_uid, annotation_id)
        )
        conn.commit()
    finally:
        conn.close()


def list_tombstones(kind):
    conn = connect_annotations()
    try:
        rows = conn.execute("SELECT uid FROM collab_tombstones WHERE kind = ?", (kind,))
        return [row["uid"] for row in rows]
    finally:
        conn.close()


def clear_tombstone(uid):
    conn = connect_annotations()
    try:
        conn.execute("DELETE FROM collab_tombstones WHERE uid = ?", (uid,))
        conn.commit()
    finally:
        conn.close()


def list_rules(run_key):
    conn = connect_annotations()
    try:
        rows = conn.execute(
            "SELECT * FROM variable_rules WHERE run_key = ? ORDER BY id", (run_key,)
        ).fetchall()
        return [_rule_row(r) for r in rows]
    finally:
        conn.close()


def add_rule(
    run_key, user_id, user_name, name, channel, lo, hi, color, collab_uid=None, created_at=None
):
    conn = connect_annotations()
    try:
        cur = conn.execute(
            """
            INSERT INTO variable_rules
                (run_key, user_id, user_name, name, channel, lo, hi, color, created_at, collab_uid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_key,
                user_id,
                user_name or "Unknown",
                str(name),
                str(channel),
                float(lo) if lo is not None else None,
                float(hi) if hi is not None else None,
                str(color),
                created_at or _now(),
                collab_uid,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM variable_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _rule_row(row)
    finally:
        conn.close()


def delete_rule(rule_id):
    """Deletes a variable rule, remembering a tombstone if it was shared."""
    conn = connect_annotations()
    try:
        row = conn.execute(
            "SELECT collab_uid FROM variable_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        conn.execute("DELETE FROM variable_rules WHERE id = ?", (rule_id,))
        if row is not None and row["collab_uid"]:
            conn.execute(
                "INSERT OR IGNORE INTO collab_tombstones (uid, kind) VALUES (?, 'variable_rule')",
                (row["collab_uid"],),
            )
        conn.commit()
    finally:
        conn.close()


def delete_rule_without_tombstone(rule_id):
    conn = connect_annotations()
    try:
        conn.execute("DELETE FROM variable_rules WHERE id = ?", (rule_id,))
        conn.commit()
    finally:
        conn.close()


def list_all_rules():
    conn = connect_annotations()
    try:
        rows = conn.execute("SELECT * FROM variable_rules ORDER BY id").fetchall()
        return [_rule_row(r) for r in rows]
    finally:
        conn.close()


def set_rule_collab_uid(rule_id, collab_uid):
    conn = connect_annotations()
    try:
        conn.execute("UPDATE variable_rules SET collab_uid = ? WHERE id = ?", (collab_uid, rule_id))
        conn.commit()
    finally:
        conn.close()
