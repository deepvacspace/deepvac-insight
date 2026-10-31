"""Observer events: configuration, storage in data/deepvac_observer.sqlite3, the
Observer's reports and the push to the n8n webhook."""

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import app.common as common

DB_NAME = "deepvac_observer.sqlite3"
CONFIG_FILE = "observer.json"

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"
SEVERITIES = (INFO, WARNING, CRITICAL)
SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}

ALARM = "alarm"
ALARM_CLEARED = "alarm_cleared"
JOB_ENDED = "job_ended"
DRIFT = "drift"
ANOMALY = "anomaly"
OFFLINE = "offline"
ONLINE = "online"
HOLD = "hold"
REQUEST_WAITING = "request_waiting"
TEST = "test"
KINDS = (
    ALARM,
    ALARM_CLEARED,
    JOB_ENDED,
    DRIFT,
    ANOMALY,
    OFFLINE,
    ONLINE,
    HOLD,
    REQUEST_WAITING,
    TEST,
)

PUSH_OFF = "off"
PUSH_PENDING = "pending"
PUSH_SENT = "sent"
PUSH_FAILED = "failed"
MAX_PUSH_ATTEMPTS = 5
PUSH_RETRY_S = 30.0
PUSH_TIMEOUT_S = 5.0

DEFAULT_WEBHOOK_URL = "http://localhost:5678/webhook/deepvac-observer"
DEFAULT_CONFIG = {
    "watch": True,
    "push": False,
    "webhook_url": DEFAULT_WEBHOOK_URL,
    "min_severity": WARNING,
    "drift_c": 3.0,
    "drift_min": 5.0,
    "settled_min": 10.0,
    "offline_s": 60.0,
    "anomaly_window_min": 30.0,
    "waiting_min": 10.0,
}


class ObserverError(ValueError):
    pass


def config_path():
    return common.DATA_DIR / CONFIG_FILE


def load_config():
    """Loads data/observer.json with defaults for missing keys."""
    config = dict(DEFAULT_CONFIG)
    try:
        stored = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if isinstance(stored, dict):
        config.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    if config["min_severity"] not in SEVERITIES:
        config["min_severity"] = WARNING
    return config


def save_config(config):
    merged = load_config()
    for key, value in config.items():
        if key not in DEFAULT_CONFIG:
            raise ObserverError(f"Unknown observer setting '{key}'.")
        merged[key] = value
    if merged["min_severity"] not in SEVERITIES:
        raise ObserverError(f"min_severity must be one of {', '.join(SEVERITIES)}.")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged


def at_least(severity, threshold):
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(threshold, 0)


def db_path():
    return common.DATA_DIR / DB_NAME


def connect(path=None):
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            chamber_id INTEGER,
            chamber_name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            facts_json TEXT NOT NULL DEFAULT '{}',
            key TEXT UNIQUE,
            push_state TEXT NOT NULL DEFAULT 'off',
            push_attempts INTEGER NOT NULL DEFAULT 0,
            push_retry_at REAL,
            push_error TEXT,
            pushed_at TEXT,
            summary TEXT,
            notify INTEGER,
            message TEXT,
            reported_at TEXT,
            reported_by TEXT,
            acknowledged_at TEXT,
            acknowledged_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_push ON events(push_state, id);
        """
    )
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def iso(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat() if epoch_s else None


def _row(row):
    event = dict(row)
    event["facts"] = json.loads(event.pop("facts_json") or "{}")
    if event["notify"] is not None:
        event["notify"] = bool(event["notify"])
    return event


def record(kind, severity, title, facts=None, chamber=None, key=None, push=False):
    """Records an event; None if one with the same key exists."""
    if kind not in KINDS:
        raise ObserverError(f"Unknown event kind '{kind}'.")
    if severity not in SEVERITIES:
        raise ObserverError(f"Unknown severity '{severity}'.")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (created_at, chamber_id, chamber_name, kind, severity, "
            "title, facts_json, key, push_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                chamber["id"] if chamber else None,
                chamber["name"] if chamber else "",
                kind,
                severity,
                title,
                json.dumps(facts or {}, ensure_ascii=False),
                key,
                PUSH_PENDING if push else PUSH_OFF,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return _row(conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def get_event(event_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def require(event_id):
    event = get_event(event_id)
    if event is None:
        raise ObserverError(f"No event {event_id}.")
    return event


def get_events(event_ids):
    return [e for e in (get_event(i) for i in event_ids) if e]


def list_events(limit=100, min_severity=INFO, chamber_id=None, hours=None, unacknowledged=False):
    """Events, newest first, filtered by severity, chamber, age and acknowledgement."""
    where, args = [], []
    ranks = [s for s in SEVERITIES if at_least(s, min_severity)]
    where.append(f"severity IN ({','.join('?' * len(ranks))})")
    args.extend(ranks)
    if chamber_id is not None:
        where.append("chamber_id = ?")
        args.append(int(chamber_id))
    if hours:
        where.append("created_at >= ?")
        args.append((datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat())
    if unacknowledged:
        where.append("acknowledged_at IS NULL")
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*args, int(limit)),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def unacknowledged_count(min_severity=WARNING):
    return len(list_events(500, min_severity, unacknowledged=True))


def due_pushes(now_s, limit=20):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE push_state = ? AND (push_retry_at IS NULL OR push_retry_at <= ?) "
            "ORDER BY id LIMIT ?",
            (PUSH_PENDING, float(now_s), int(limit)),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def mark_pushed(event_id, ok, error="", now_s=None):
    """Records a push attempt and schedules the retry after a failure."""
    event = require(event_id)
    attempts = event["push_attempts"] + 1
    if ok:
        fields = {"push_state": PUSH_SENT, "pushed_at": now_iso(), "push_error": None}
    elif attempts >= MAX_PUSH_ATTEMPTS:
        fields = {"push_state": PUSH_FAILED, "push_error": error}
    else:
        retry_at = (now_s or datetime.now(timezone.utc).timestamp()) + PUSH_RETRY_S * 2 ** (
            attempts - 1
        )
        fields = {"push_state": PUSH_PENDING, "push_error": error, "push_retry_at": retry_at}
    fields["push_attempts"] = attempts
    _update(event_id, fields)
    return get_event(event_id)


def queue_push(event_id):
    """Queues an event to be pushed at the next pass."""
    require(event_id)
    _update(event_id, {"push_state": PUSH_PENDING, "push_attempts": 0, "push_retry_at": None})
    return get_event(event_id)


def report(event_id, summary, notify, message="", by="Observer"):
    """Records the Observer's summary, notify decision and message for an event."""
    require(event_id)
    summary = (summary or "").strip()
    if not summary:
        raise ObserverError("Give the summary: what happened, from the facts.")
    message = (message or "").strip()
    if notify and not message:
        raise ObserverError("Give the message for the people to notify.")
    _update(
        event_id,
        {
            "summary": summary,
            "notify": 1 if notify else 0,
            "message": message if notify else "",
            "reported_at": now_iso(),
            "reported_by": by,
        },
    )
    return get_event(event_id)


def acknowledge(event_id, user_name):
    event = require(event_id)
    if event["acknowledged_at"]:
        return event
    _update(event_id, {"acknowledged_at": now_iso(), "acknowledged_by": user_name or "Unknown"})
    return get_event(event_id)


def add_facts(event_id, **facts):
    event = require(event_id)
    merged = {**event["facts"], **facts}
    _update(event_id, {"facts_json": json.dumps(merged, ensure_ascii=False)})
    return get_event(event_id)


def _update(event_id, fields):
    conn = connect()
    try:
        conn.execute(
            f"UPDATE events SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), int(event_id)),
        )
        conn.commit()
    finally:
        conn.close()


def public(event):
    """Event as the Observer and the webhook see it."""
    keys = (
        "id",
        "created_at",
        "chamber_name",
        "kind",
        "severity",
        "title",
        "facts",
        "summary",
        "notify",
        "message",
        "acknowledged_by",
        "acknowledged_at",
    )
    return {k: event[k] for k in keys if event.get(k) not in (None, "", {})}


def deliver(event, url, token, timeout=PUSH_TIMEOUT_S):
    """POSTs an event to the Observer webhook; returns '' or the error."""
    body = json.dumps({"event": public(event)}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} from {url}"
    except (urllib.error.URLError, OSError) as exc:
        return f"{url}: {getattr(exc, 'reason', exc)}"
    return "" if 200 <= status < 300 else f"HTTP {status} from {url}"
