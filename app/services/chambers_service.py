"""Persisted registry of named chamber connections (name + host + port), in
its own local SQLite database.

The app can connect to several of these at once (up to
chamber_session.MAX_CONNECTED_CHAMBERS, see views/monitoring.py and
main_window.py's _connect_chamber) (a run saved via
"Save Session as Run" is tagged with whichever chamber was connected at the
time -- see data_service.save_monitoring_session()'s chamber parameter).
"""

import sqlite3
from datetime import datetime, timezone

from app.common import DATA_DIR

CHAMBERS_DB = DATA_DIR / "deepvac_chambers.sqlite3"


class ChamberError(ValueError):
    pass


def connect_chambers(db_path=None):
    """db_path overrides CHAMBERS_DB for this call only -- see
    auth_service.connect_auth()'s docstring for why the other functions in
    this file use the module-level constant implicitly instead."""
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
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # Seed one default entry on first run so the chamber dropdown is never
    # empty out of the box -- matches the host/port defaults the old,
    # single-connection UI used to hardcode (127.0.0.1:5555).
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


def add_chamber(name, host, port):
    _validate(name, host, port)
    conn = connect_chambers()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO chambers (name, host, port, created_at) VALUES (?, ?, ?, ?)",
                (name.strip(), host.strip(), int(port), _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ChamberError(f"A chamber named '{name}' already exists.") from exc
        conn.commit()
        row = conn.execute("SELECT * FROM chambers WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row(row)
    finally:
        conn.close()


def update_chamber(chamber_id, name, host, port):
    _validate(name, host, port)
    conn = connect_chambers()
    try:
        try:
            conn.execute(
                "UPDATE chambers SET name = ?, host = ?, port = ? WHERE id = ?",
                (name.strip(), host.strip(), int(port), chamber_id),
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
