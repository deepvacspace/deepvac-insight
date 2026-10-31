"""What the agents are doing right now: every tool call through the agent API,
described in plain words ("Analyst: comparing runs #12, #13").

Kept in memory (the last few hundred calls) and served at GET /activity, so
the chat in the app can show the crew's progress while it works on an
answer. A call that returned a chamber request also records its id and the
state the agent saw ("request" / "request_state"), so the chat knows which
requests this conversation made and what the crew already knows of them;
likewise for test campaigns ("campaign" / "campaign_news").
Nothing here is stored.
"""

import itertools
import threading
import time
from collections import deque

_lock = threading.Lock()
_events: deque = deque(maxlen=300)
_ids = itertools.count(1)

AGENT_NAMES = {
    "analyst": "Analyst",
    "worker": "Worker",
    "brain": "Brain",
    "professor": "Professor",
    "observer": "Observer",
}


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
        "export_data": lambda: f"exporting {_runs(a)} to {a.get('format', 'csv').upper()}",
        "export_chart": lambda: (
            f"drawing a chart comparing {_runs(a)} with {', '.join(map(str, a['compare_with']))}"
            if a.get("compare_with")
            else f"drawing the {a.get('kind', 'temperature')} chart of {_runs(a)}"
        ),
        "export_report": lambda: (
            f"writing the {a.get('format', 'pdf').upper()} report of {_runs(a)}"
        ),
        "export_comparison": lambda: (
            "writing a comparison CSV of runs " + ", ".join(str(r) for r in a.get("runs") or [])
        ),
        "export_bundle": lambda: f"exporting the bundle of {_runs(a)}",
        "list_recordings": lambda: (
            "listing the recorded days" + (f" of {a['chamber']}" if a.get("chamber") else "")
        ),
        "get_recording": lambda: (
            f"reading the recording of {a.get('chamber') or 'the chamber'}"
            + (f" from {a['start']}" if a.get("start") else " today")
            + (f" to {a['end']}" if a.get("end") else "")
        ),
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
        "wiki_index": lambda: "reading the wiki's index",
        "wiki_search": lambda: f"searching the wiki for '{a.get('query', '')}'",
        "wiki_read": lambda: f"reading the wiki page '{a.get('page', '')}'",
        "wiki_propose": lambda: (
            f"proposing a change to the wiki page '{a.get('page', '')}' (waits for review)"
        ),
        "sql_schema": lambda: "reading the experiments database's tables",
        "sql_query": lambda: "querying the experiments database",
        "get_standards_pack": lambda: "reading the standards pack",
        "draft_campaign": lambda: (
            f"drafting a {a.get('level', 'qualification')} campaign for {a.get('article', 'the article')}"
        ),
        "submit_campaign": lambda: (
            f"submitting campaign {a.get('campaign_id')} for approval (Campaigns page)"
        ),
        "get_campaign": lambda: (
            f"checking campaign {a['campaign_id']}"
            if a.get("campaign_id")
            else "checking the campaign"
        ),
        "list_campaigns": lambda: "listing the campaigns",
        "wait_for_campaign": lambda: f"following campaign {a.get('campaign_id')}",
        "recommend": lambda: (
            f"recommending '{a.get('option')}' at the hold point of campaign {a.get('campaign_id')}"
        ),
        "get_report_facts": lambda: f"reading the results of campaign {a.get('campaign_id')}",
        "issue_report": lambda: f"writing the report of campaign {a.get('campaign_id')}",
        "cancel_campaign": lambda: f"withdrawing campaign {a.get('campaign_id')}",
        "get_event": lambda: f"reading event {a.get('event_id')}",
        "list_events": lambda: "listing the recent events",
        "report_event": lambda: (
            f"reporting on event {a.get('event_id')}"
            + (" and notifying people" if a.get("notify") else "")
        ),
        "propose_stop": lambda: (
            f"proposing to stop the job (event {a.get('event_id')}; waits for approval)"
        ),
        "propose_setpoint": lambda: (
            f"proposing a setpoint of {a.get('temperature_c')} °C (event {a.get('event_id')}; "
            "waits for approval)"
        ),
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


def finished(
    event_id: int, ok: bool, request: dict | None = None, campaign: dict | None = None
) -> None:
    """request: the chamber request the call returned (worker_tools.public_request), if any;
    campaign: the test campaign it returned (campaign_service.public), if any."""
    with _lock:
        for event in reversed(_events):
            if event["id"] == event_id:
                event["status"] = "done" if ok else "error"
                event["seconds"] = round(time.time() - event["time"], 2)
                if request and request.get("id") is not None:
                    event["request"] = request["id"]
                    event["request_state"] = request_state(request)
                if campaign and campaign.get("id") is not None:
                    event["campaign"] = campaign["id"]
                    event["campaign_news"] = campaign.get("news")
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
