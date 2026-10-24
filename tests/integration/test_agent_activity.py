"""The agents' activity feed (app/api/activity.py) behind GET /activity: what the
chat shows while the crew works."""

import time

import pytest
from starlette.testclient import TestClient

from app.api import activity, server
from app.api.analyst_tools import AnalystTools
from app.api.chambers import ChamberLinks
from app.api.worker_tools import WorkerTools

pytestmark = pytest.mark.integration


def test_tool_calls_are_described_in_plain_words():
    assert activity.describe("get_run_summary", {"run": "#12"}) == "reading the summary of run #12"
    assert activity.describe("compare_runs", {"runs": ["#12", "#13"]}) == "comparing runs #12, #13"
    assert "waits for your approval" in activity.describe("request_setpoint", {"temperature_c": 30})
    assert activity.describe("something_new", {}) == "using something_new"


def test_feed_records_start_and_end():
    activity.clear()
    since = time.time() - 1
    event = activity.started("analyst", "list_runs", {"search": "0degrees"})
    assert activity.since(since)[-1]["status"] == "running"
    activity.finished(event, ok=True)
    last = activity.since(since)[-1]
    assert (last["agent"], last["text"], last["status"]) == (
        "Analyst",
        "listing runs matching '0degrees'",
        "done",
    )
    assert activity.since(time.time() + 10) == []


def test_activity_route_needs_the_token(deepvac_data_dir):
    activity.clear()
    app = server.build_app("secret", AnalystTools(), WorkerTools(ChamberLinks()))
    activity.finished(activity.started("worker", "get_live_status", {}), ok=True)
    with TestClient(app) as client:
        assert client.get("/activity?since=0").status_code == 401
        events = client.get("/activity?since=0", headers={"Authorization": "Bearer secret"}).json()[
            "events"
        ]
    assert events[-1]["text"] == "reading the chamber's live status"
