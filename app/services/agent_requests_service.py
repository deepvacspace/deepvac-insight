"""Chamber actions requested by agents, awaiting approval."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.common import DATA_DIR

AGENT_REQUESTS_DB = DATA_DIR / "deepvac_agent_requests.sqlite3"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTING = "executing"
EXECUTED = "executed"
FAILED = "failed"
EXPIRED = "expired"
STATUSES = (PENDING, APPROVED, REJECTED, EXECUTING, EXECUTED, FAILED, EXPIRED)

KINDS = ("setpoint", "program", "stop")

JOB_STARTING = "starting"
JOB_RUNNING = "running"
JOB_FINISHED = "finished"
JOB_STOPPED = "stopped"
JOB_ALARM = "alarm"
JOB_REPLACED = "replaced"
JOB_UNKNOWN = "unknown"
JOB_OPEN = (JOB_STARTING, JOB_RUNNING)

PENDING_TIMEOUT = timedelta(minutes=30)


class AgentRequestError(ValueError):
    pass


def connect_agent_requests(db_path=None):
    """Opens the agent requests database."""
    path = db_path or AGENT_REQUESTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            chamber_id INTEGER,
            chamber_name TEXT NOT NULL DEFAULT '',
            params_json TEXT NOT NULL DEFAULT '{}',
            summary TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL DEFAULT 'agent',
            status TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT,
            decided_by TEXT,
            decision_comment TEXT,
            executed_at TEXT,
            result TEXT
        )
        """
    )
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(agent_requests)")}
    for column in ("job_state", "job_started_at", "job_ended_at"):
        if column not in columns:
            conn.execute(f"ALTER TABLE agent_requests ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_requests_status ON agent_requests(status, id)"
    )
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "chamber_id": row["chamber_id"],
        "chamber_name": row["chamber_name"],
        "params": json.loads(row["params_json"]),
        "summary": row["summary"],
        "reason": row["reason"],
        "requested_by": row["requested_by"],
        "status": row["status"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "decision_comment": row["decision_comment"],
        "executed_at": row["executed_at"],
        "result": row["result"],
        "job_state": row["job_state"],
        "job_started_at": row["job_started_at"],
        "job_ended_at": row["job_ended_at"],
    }


def create_request(kind, chamber, params, summary, reason="", requested_by="agent"):
    """Creates a chamber request."""
    if kind not in KINDS:
        raise AgentRequestError(f"Unknown request kind '{kind}'.")
    conn = connect_agent_requests()
    try:
        cur = conn.execute(
            "INSERT INTO agent_requests (created_at, kind, chamber_id, chamber_name, params_json, summary, reason, "
            "requested_by, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                kind,
                chamber["id"],
                chamber["name"],
                json.dumps(params),
                summary,
                reason or "",
                requested_by or "agent",
                PENDING,
            ),
        )
        conn.commit()
        return _row(
            conn.execute("SELECT * FROM agent_requests WHERE id = ?", (cur.lastrowid,)).fetchone()
        )
    finally:
        conn.close()


def get_request(request_id):
    conn = connect_agent_requests()
    try:
        row = conn.execute(
            "SELECT * FROM agent_requests WHERE id = ?", (int(request_id),)
        ).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def list_requests(status=None, limit=100):
    conn = connect_agent_requests()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM agent_requests WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_requests ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def pending_count():
    expire_stale()
    conn = connect_agent_requests()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_requests WHERE status = ?", (PENDING,)
        ).fetchone()[0]
    finally:
        conn.close()


def decide(request_id, approve, user_name, comment=""):
    """A person's decision on a pending request."""
    if not user_name:
        raise AgentRequestError("Only a signed-in user can decide on a request.")
    expire_stale()
    conn = connect_agent_requests()
    try:
        cur = conn.execute(
            "UPDATE agent_requests SET status = ?, decided_at = ?, decided_by = ?, decision_comment = ? "
            "WHERE id = ? AND status = ?",
            (
                APPROVED if approve else REJECTED,
                _now(),
                user_name,
                comment or "",
                int(request_id),
                PENDING,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise AgentRequestError(f"Request {request_id} is no longer pending.")
        return _row(
            conn.execute("SELECT * FROM agent_requests WHERE id = ?", (int(request_id),)).fetchone()
        )
    finally:
        conn.close()


def approved_requests():
    """Approved requests, oldest first, for the executor."""
    conn = connect_agent_requests()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_requests WHERE status = ? ORDER BY id", (APPROVED,)
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def claim(request_id):
    """Claims an approved request for execution."""
    conn = connect_agent_requests()
    try:
        cur = conn.execute(
            "UPDATE agent_requests SET status = ? WHERE id = ? AND status = ?",
            (EXECUTING, int(request_id), APPROVED),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_requests(request_ids):
    """The requests with these ids that exist, by id."""
    ids = [int(i) for i in request_ids][:100]
    if not ids:
        return []
    conn = connect_agent_requests()
    try:
        rows = conn.execute(
            f"SELECT * FROM agent_requests WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
            ids,
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def mark_done(request_id, ok, result, job_state=None):
    """Records a request's outcome."""
    conn = connect_agent_requests()
    try:
        conn.execute(
            "UPDATE agent_requests SET status = ?, executed_at = ?, result = ?, job_state = ? "
            "WHERE id = ? AND status = ?",
            (
                EXECUTED if ok else FAILED,
                _now(),
                str(result),
                job_state if ok else None,
                int(request_id),
                EXECUTING,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def open_jobs():
    """Executed requests whose job hasn't ended, oldest first, for the executor to follow."""
    conn = connect_agent_requests()
    try:
        rows = conn.execute(
            f"SELECT * FROM agent_requests WHERE status = ? AND job_state IN "
            f"({','.join('?' * len(JOB_OPEN))}) ORDER BY id",
            (EXECUTED, *JOB_OPEN),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def set_job_state(request_id, job_state, started_at=None, ended_at=None):
    """Updates the state of a request's job."""

    def iso(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch else None

    conn = connect_agent_requests()
    try:
        conn.execute(
            "UPDATE agent_requests SET job_state = ?, job_started_at = COALESCE(?, job_started_at), "
            "job_ended_at = COALESCE(?, job_ended_at) WHERE id = ?",
            (job_state, iso(started_at), iso(ended_at), int(request_id)),
        )
        conn.commit()
    finally:
        conn.close()


def epoch(iso_text):
    """Converts a stored timestamp to epoch seconds."""
    return datetime.fromisoformat(iso_text).timestamp() if iso_text else None


def expire_stale(now=None):
    """Pending requests older than PENDING_TIMEOUT become expired."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - PENDING_TIMEOUT).isoformat()
    conn = connect_agent_requests()
    try:
        conn.execute(
            "UPDATE agent_requests SET status = ?, result = ? WHERE status = ? AND created_at < ?",
            (
                EXPIRED,
                f"not decided within {int(PENDING_TIMEOUT.total_seconds() // 60)} minutes",
                PENDING,
                cutoff,
            ),
        )
        conn.commit()
    finally:
        conn.close()
