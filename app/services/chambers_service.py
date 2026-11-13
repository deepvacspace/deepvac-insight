"""Registry of saved chamber connections."""

import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR

CHAMBERS_DB = DATA_DIR / "deepvac_chambers.sqlite3"


class ChamberError(ValueError):
    pass


_HUB_SYNC_COLUMNS = ("hub_id", "hub_synced_at")


def _ensure_hub_sync_columns(conn):
    """Adds any missing hub_* sync columns to the chambers table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(chambers)")}
    for column in _HUB_SYNC_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE chambers ADD COLUMN {column} TEXT")


def connect_chambers(db_path=None):
    """Opens the chambers database."""
    path = db_path or CHAMBERS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chambers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            hub_id TEXT UNIQUE,
            hub_synced_at TEXT
        )
        """
    )
    _ensure_hub_sync_columns(conn)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(chambers)")}
    if "protocol" not in existing:
        conn.execute("ALTER TABLE chambers ADD COLUMN protocol TEXT NOT NULL DEFAULT 'json'")
    conn.commit()

    if conn.execute("SELECT 1 FROM chambers LIMIT 1").fetchone() is None:
        conn.execute(
            "INSERT INTO chambers (name, host, port, created_at) VALUES (?, ?, ?, ?)",
            ("Chamber 1", "127.0.0.1", 5555, _now()),
        )
        conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "port": row["port"],
        "created_at": row["created_at"],
        "hub_id": row["hub_id"],
        "hub_synced_at": row["hub_synced_at"],
        "protocol": row["protocol"],
    }


def list_chambers():
    conn = connect_chambers()
    try:
        rows = conn.execute("SELECT * FROM chambers ORDER BY name").fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def _validate(name, host, port):
    if not name or not name.strip():
        raise ChamberError("Name is required.")
    if not host or not host.strip():
        raise ChamberError("Host is required.")
    try:
        port_int = int(port)
    except (TypeError, ValueError) as exc:
        raise ChamberError("Port must be a number.") from exc
    if not (1 <= port_int <= 65535):
        raise ChamberError("Port must be between 1 and 65535.")


PROTOCOLS = ("json", "plc")


def add_chamber(name, host, port, hub_id=None, protocol="json"):
    _validate(name, host, port)
    if protocol not in PROTOCOLS:
        raise ChamberError(f"Protocol must be one of {', '.join(PROTOCOLS)}.")
    conn = connect_chambers()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO chambers (name, host, port, created_at, hub_id, hub_synced_at, protocol) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name.strip(),
                    host.strip(),
                    int(port),
                    _now(),
                    hub_id,
                    _now() if hub_id else None,
                    protocol,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ChamberError(f"A chamber named '{name}' already exists.") from exc
        conn.commit()
        row = conn.execute("SELECT * FROM chambers WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row(row)
    finally:
        conn.close()


def get_chamber_by_hub_id(hub_id):
    conn = connect_chambers()
    try:
        row = conn.execute("SELECT * FROM chambers WHERE hub_id = ?", (hub_id,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def mark_hub_synced(chamber_id, hub_id):
    conn = connect_chambers()
    try:
        conn.execute(
            "UPDATE chambers SET hub_id = ?, hub_synced_at = ? WHERE id = ?",
            (hub_id, _now(), chamber_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM chambers WHERE id = ?", (chamber_id,)).fetchone()
        return _row(row)
    finally:
        conn.close()


def touch_hub_synced(chamber_id):
    conn = connect_chambers()
    try:
        conn.execute("UPDATE chambers SET hub_synced_at = ? WHERE id = ?", (_now(), chamber_id))
        conn.commit()
    finally:
        conn.close()


def update_chamber(chamber_id, name, host, port, protocol=None):
    """Updates a saved chamber."""
    _validate(name, host, port)
    if protocol is not None and protocol not in PROTOCOLS:
        raise ChamberError(f"Protocol must be one of {', '.join(PROTOCOLS)}.")
    conn = connect_chambers()
    try:
        try:
            conn.execute(
                "UPDATE chambers SET name = ?, host = ?, port = ?, protocol = COALESCE(?, protocol) "
                "WHERE id = ?",
                (name.strip(), host.strip(), int(port), protocol, chamber_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ChamberError(f"A chamber named '{name}' already exists.") from exc
        conn.commit()
        row = conn.execute("SELECT * FROM chambers WHERE id = ?", (chamber_id,)).fetchone()
        if row is None:
            raise ChamberError("Chamber not found.")
        return _row(row)
    finally:
        conn.close()


def delete_chamber(chamber_id):
    conn = connect_chambers()
    try:
        conn.execute("DELETE FROM chambers WHERE id = ?", (chamber_id,))
        conn.commit()
    finally:
        conn.close()
