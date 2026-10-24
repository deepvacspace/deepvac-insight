"""What the agents are doing right now: every tool call through the agent API,
described in plain words ("Analyst: comparing runs #12, #13").

Kept in memory (the last few hundred calls) and served at GET /activity, so
the chat in the app can show the crew's progress while it works on an
answer. A call that returned a chamber request also records its id and the
state the agent saw ("request" / "request_state"), so the chat knows which
requests this conversation made and what the crew already knows of them.
Nothing here is stored.
"""

import itertools
import threading
import time
from collections import deque

_lock = threading.Lock()
_events: deque = deque(maxlen=300)
_ids = itertools.count(1)

AGENT_NAMES = {"analyst": "Analyst", "worker": "Worker"}


def _runs(args):
    run = args.get("run")
    return f"run {run}" if run else "a run"


def describe(tool: str, args: dict) -> str:
    """Plain-words description of a tool call."""
    a = args or {}
    texts = {
        "list_runs": lambda: (
            "listing runs"
            + (f" matching '{a['search']}'" if a.get("search") else "")
            + (f" in {a['group']}" if a.get("group") else "")
        ),
        "rank_runs": lambda: (
            f"ranking runs by {a.get('metric', 'tail_mae')}"
            + (f" in {a['group']}" if a.get("group") else "")
        ),
        "get_run_summary": lambda: f"reading the summary of {_runs(a)}",
        "get_stages": lambda: f"looking at the stages of {_runs(a)}",
        "get_kpis": lambda: f"computing the KPIs of {_runs(a)}",
        "get_statistics": lambda: (
            "computing statistics of "
            + (", ".join(a["channels"]) if a.get("channels") else "the main channels")
            + f" in {_runs(a)}"
        ),
        "get_anomalies": lambda: f"checking {_runs(a)} for anomalies",
        "compare_runs": lambda: "comparing runs " + ", ".join(str(r) for r in a.get("runs") or []),
        "list_channels": lambda: f"listing the channels of {_runs(a)}",
        "get_events": lambda: f"reading the events of {_runs(a)}",
        "export_data": lambda: f"exporting {_runs(a)} to CSV",
        "simulate_pid": lambda: (
            f"simulating Kp {a.get('kp')}, Ki {a.get('ki')}, Kd {a.get('kd')} "
            f"to {a.get('target_c')} °C on the digital twin"
        ),
        "list_chambers": lambda: "listing the chambers",
        "get_live_status": lambda: "reading the chamber's live status",
        "get_settings": lambda: "reading the PLC settings",
        "list_programs": lambda: "listing the test programs",
        "request_setpoint": lambda: (
            f"requesting {a.get('temperature_c')} °C (waits for your approval)"
        ),
        "request_program": lambda: (
            f"requesting program '{a.get('program')}' (waits for your approval)"
        ),
        "request_stop": lambda: "requesting a stop (waits for your approval)",
        "get_request": lambda: f"checking request {a.get('request_id')}",
        "wait_for_request": lambda: (
            f"waiting for your decision on request {a.get('request_id')} (Agent Requests)"
        ),
        "list_requests": lambda: "checking the requests",
    }
    try:
        return texts[tool]() if tool in texts else f"using {tool}"
    except Exception:
        return f"using {tool}"


def started(agent: str, tool: str, args: dict) -> int:
    event_id = next(_ids)
    event = {
        "id": event_id,
        "time": time.time(),
        "agent": AGENT_NAMES.get(agent, agent),
        "tool": tool,
        "text": describe(tool, args),
        "status": "running",
        "seconds": None,
    }
    with _lock:
        _events.append(event)
    return event_id


def finished(event_id: int, ok: bool, request: dict | None = None) -> None:
    """request: the chamber request the call returned (worker_tools.public_request), if any."""
    with _lock:
        for event in reversed(_events):
            if event["id"] == event_id:
                event["status"] = "done" if ok else "error"
                event["seconds"] = round(time.time() - event["time"], 2)
                if request and request.get("id") is not None:
                    event["request"] = request["id"]
                    event["request_state"] = request_state(request)
                return


def request_state(request: dict) -> str:
    """'status' or 'status/job_state': what changed about a request, for the chat."""
    job = request.get("job_state")
    return f"{request.get('status')}/{job}" if job else str(request.get("status"))


def since(timestamp: float, limit: int = 50) -> list[dict]:
    """Calls started at or after `timestamp`, oldest first."""
    with _lock:
        recent = [dict(e) for e in _events if e["time"] >= timestamp]
    return recent[-limit:]


def clear() -> None:
    with _lock:
        _events.clear()
