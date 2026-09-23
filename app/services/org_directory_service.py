"""Local read-only cache of the organization member directory pulled from
Deepvac Hub, in its own local SQLite database."""

import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR

ORG_DIRECTORY_DB = DATA_DIR / "deepvac_org_directory.sqlite3"


def connect_org_directory(db_path=None):
    path = db_path or ORG_DIRECTORY_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            email TEXT NOT NULL,
            role TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            synced_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _member_row(row):
    return {
        "user_id": row["user_id"],
        "display_name": row["display_name"],
        "email": row["email"],
        "role": row["role"],
    }


def list_members():
    conn = connect_org_directory()
    try:
        rows = conn.execute("SELECT * FROM members ORDER BY display_name").fetchall()
        return [_member_row(r) for r in rows]
    finally:
        conn.close()


def last_synced_at():
    conn = connect_org_directory()
    try:
        row = conn.execute("SELECT synced_at FROM sync_state WHERE id = 1").fetchone()
        return row["synced_at"] if row else None
    finally:
        conn.close()


def replace_all(members):
    conn = connect_org_directory()
    try:
        conn.execute("DELETE FROM members")
        conn.executemany(
            "INSERT INTO members (user_id, display_name, email, role) VALUES (?, ?, ?, ?)",
            [
                (str(m["user_id"]), str(m["display_name"]), str(m["email"]), str(m["role"]))
                for m in members
            ],
        )
        conn.execute(
            "INSERT INTO sync_state (id, synced_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET synced_at = excluded.synced_at",
            (_now(),),
        )
        conn.commit()
    finally:
        conn.close()
