"""Chamber actions requested by agents, waiting for a person to approve them,
in their own local SQLite database.

The agent API (app/api) never sends a command to a chamber directly: its
worker tools create a request here (status "pending"). A signed-in user
approves or rejects it in the app's Agent Requests dialog. The API's executor
then sends approved requests to the PLC and records the outcome. Agents can
read a request's status but have no way to approve one.

    pending --approve--> approved --claim--> executing --> executed | failed
            --reject---> rejected
            --timeout--> expired

claim() is atomic, so a request is sent at most once even when two API
processes run executors on the same database.
"""

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

# a request nobody decided on is not executed hours later, when the chamber may be doing something else
PENDING_TIMEOUT = timedelta(minutes=30)


class AgentRequestError(ValueError):
    pass


def connect_agent_requests(db_path=None):
    """db_path overrides AGENT_REQUESTS_DB for this call only -- see
    auth_service.connect_auth()'s docstring for why the other functions in
    this file use the module-level constant implicitly instead."""
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
    }


def create_request(kind, chamber, params, summary, reason="", requested_by="agent"):
    """chamber: a chambers_service row. Returns the new request."""
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
    """A person's decision on a pending request. Only pending requests can be decided."""
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
    """approved -> executing; True for the one caller that got it."""
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


def mark_done(request_id, ok, result):
    conn = connect_agent_requests()
    try:
        conn.execute(
            "UPDATE agent_requests SET status = ?, executed_at = ?, result = ? WHERE id = ? AND status = ?",
            (EXECUTED if ok else FAILED, _now(), str(result), int(request_id), EXECUTING),
        )
        conn.commit()
    finally:
        conn.close()


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
