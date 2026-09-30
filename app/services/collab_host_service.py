"""Host-side storage for the collaboration server: paired peers and shared
items, in their own local SQLite database."""

import json
import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR
from app.services import licensing_client

COLLAB_HOST_DB = DATA_DIR / "deepvac_collab_host.sqlite3"


def connect_collab_host(db_path=None):
    path = db_path or COLLAB_HOST_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS peers (
            key_hash TEXT PRIMARY KEY,
            public_key BLOB NOT NULL,
            display_name TEXT NOT NULL,
            paired_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            uid TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_items_kind ON items(kind)")
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def register_peer(public_key_raw, display_name):
    key_hash = licensing_client.device_public_key_hash(public_key_raw)
    conn = connect_collab_host()
    try:
        conn.execute(
            "INSERT INTO peers (key_hash, public_key, display_name, paired_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key_hash) DO UPDATE SET display_name = excluded.display_name",
            (key_hash, public_key_raw, display_name or "Unknown", _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return key_hash


def get_peer(key_hash):
    conn = connect_collab_host()
    try:
        row = conn.execute("SELECT * FROM peers WHERE key_hash = ?", (key_hash,)).fetchone()
        if row is None:
            return None
        return {
            "key_hash": row["key_hash"],
            "public_key": bytes(row["public_key"]),
            "display_name": row["display_name"],
        }
    finally:
        conn.close()


def peer_count():
    conn = connect_collab_host()
    try:
        return conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
    finally:
        conn.close()


def list_items(kind):
    conn = connect_collab_host()
    try:
        rows = conn.execute(
            "SELECT uid, data FROM items WHERE kind = ? AND deleted_at IS NULL ORDER BY updated_at",
            (kind,),
        ).fetchall()
        return [{"uid": row["uid"], **json.loads(row["data"])} for row in rows]
    finally:
        conn.close()


def list_deleted_uids(kind):
    conn = connect_collab_host()
    try:
        rows = conn.execute(
            "SELECT uid FROM items WHERE kind = ? AND deleted_at IS NOT NULL", (kind,)
        ).fetchall()
        return [row["uid"] for row in rows]
    finally:
        conn.close()


def put_item(kind, uid, data, mutable):
    """Stores an item by uid. Immutable kinds ignore a known uid (including a
    deleted one); mutable kinds overwrite a live item's data."""
    conn = connect_collab_host()
    try:
        if mutable:
            conn.execute(
                "INSERT INTO items (uid, kind, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(uid) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at "
                "WHERE items.kind = excluded.kind AND items.deleted_at IS NULL",
                (uid, kind, json.dumps(data), _now()),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO items (uid, kind, data, updated_at) VALUES (?, ?, ?, ?)",
                (uid, kind, json.dumps(data), _now()),
            )
        conn.commit()
    finally:
        conn.close()


def delete_item(kind, uid):
    conn = connect_collab_host()
    try:
        cur = conn.execute(
            "UPDATE items SET deleted_at = COALESCE(deleted_at, ?) WHERE uid = ? AND kind = ?",
            (_now(), uid, kind),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
