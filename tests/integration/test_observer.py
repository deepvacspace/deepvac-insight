"""Observer: monitor events, push to the webhook and Observer tools."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from starlette.testclient import TestClient

from app.api import activity, server
from app.api.analyst_tools import AnalystTools
from app.api.observer_monitor import ObserverMonitor
from app.api.observer_tools import ObserverTools
from app.api.professor_tools import ProfessorTools
from app.api.worker_tools import WorkerTools
from app.services import agent_requests_service as requests_service
from app.services import campaign_service as cs
from app.services import chambers_service, plc_gate
from app.services import observer_service as obs

CHAMBER = {"id": 7, "name": "DV-1", "protocol": "plc", "host": "127.0.0.1", "port": 1}


class FakeClient:
    def __init__(self):
        self.temp = 20.0
        self.setpoint = 20.0
        self.status = 1
        self.alarm = 0
        self.alarm_at = None
        self.connected = True
        self.now = 0.0
        self.job_started_at = None
        self.job_ended_at = None
        self.samples = []
        self.sent = []

    def snapshot(self):
        return plc_gate.PlcSnapshot(
            connected=self.connected,
            address="fake",
            values={
                "temp": self.temp,
                "temp_ref": self.setpoint,
                "temp_u": 12.0,
                "status_condition": float(self.status),
            },
            values_at=self.now,
            alarm_code=self.alarm,
            alarm_at=self.alarm_at,
            job_started_at=self.job_started_at,
            job_ended_at=self.job_ended_at,
        )

    def history(self, since=None):
        return [s for s in self.samples if since is None or s["timestamp"] > since]

    def send_text_cmd(self, command):
        self.sent.append(command)


class FakeLinks:
    def __init__(self, client):
        self._client = client

    def chambers(self):
        return [CHAMBER]

    def resolve(self, reference):
        return CHAMBER

    def client(self, chamber, wait=True):
        return self._client


@pytest.fixture
def rig(deepvac_data_dir):
    client = FakeClient()
    links = FakeLinks(client)
    sent = []
    monitor = ObserverMonitor(links, "tok", lambda e, url, token: sent.append(e["id"]) or "")
    return client, links, monitor, sent


def at(monitor, client, now):
    client.now = now
    monitor.run_once(now)


def kinds():
    return [e["kind"] for e in reversed(obs.list_events())]


def test_an_alarm_is_recorded_once_and_pushed_only_when_on(rig):
    client, links, monitor, sent = rig
    t0 = time.time()
    at(monitor, client, t0)
    assert obs.list_events() == []

    client.alarm, client.alarm_at, client.status = 1 << 2, t0 + 5, 4
    at(monitor, client, t0 + 5)
    at(monitor, client, t0 + 7)
    (alarm,) = obs.list_events()
    assert alarm["kind"] == obs.ALARM and alarm["severity"] == obs.CRITICAL
    assert alarm["facts"]["alarm_code"] == 4 and alarm["push_state"] == obs.PUSH_OFF
    assert sent == []

    ObserverMonitor(links, "tok", lambda *a: "").run_once(t0 + 8)
    assert len(obs.list_events()) == 1

    client.alarm, client.status = 0, 1
    at(monitor, client, t0 + 10)
    assert kinds() == [obs.ALARM, obs.ALARM_CLEARED]

    obs.save_config({"push": True})
    client.alarm, client.alarm_at, client.status = 1, t0 + 20, 4
    at(monitor, client, t0 + 20)
    second = obs.list_events()[0]
    assert second["kind"] == obs.ALARM and second["push_state"] == obs.PUSH_SENT
    assert sent == [second["id"]]


def test_a_job_end_is_reported_with_the_request_that_started_it(rig):
    client, _, monitor, _ = rig
    t0 = time.time()
    client.job_started_at, client.job_ended_at = t0 - 900, t0 - 600
    at(monitor, client, t0)
    assert obs.list_events() == []

    request = requests_service.create_request(
        "setpoint", CHAMBER, {"temperature_c": 30}, "Set 30", "x"
    )
    requests_service.decide(request["id"], True, "Ana")
    requests_service.claim(request["id"])
    requests_service.mark_done(request["id"], True, "sent", requests_service.JOB_STARTING)
    client.status, client.job_started_at, client.job_ended_at = 2, t0 + 1, None
    at(monitor, client, t0 + 2)
    client.status, client.job_ended_at = 3, t0 + 1201
    at(monitor, client, t0 + 1202)
    (ended,) = obs.list_events(min_severity=obs.INFO)
    assert ended["title"] == "The job on DV-1 finished" and ended["severity"] == obs.INFO
    assert ended["facts"]["duration_min"] == 20.0
    assert ended["facts"]["request_id"] == request["id"]


def test_drift_from_a_steady_setpoint_but_not_during_a_ramp(rig):
    client, _, monitor, _ = rig
    obs.save_config({"drift_c": 3.0, "drift_min": 5.0, "settled_min": 10.0})
    t0 = time.time()
    client.status = 2
    for k in range(0, 40):
        client.setpoint, client.temp = 20 + k, 20 + k * 0.5
        at(monitor, client, t0 + k * 60)
    assert obs.list_events() == []

    t1 = t0 + 40 * 60
    client.setpoint, client.temp = 60.0, 55.0
    for k in range(0, 16):
        at(monitor, client, t1 + k * 60)
    (drift,) = obs.list_events()
    assert drift["kind"] == obs.DRIFT
    assert drift["title"] == "DV-1 is 5.0 °C below its setpoint of +60.0 °C"
    assert drift["facts"]["error_c"] == -5.0

    client.temp = 59.5
    at(monitor, client, t1 + 17 * 60)
    client.temp = 55.0
    for k in range(18, 25):
        at(monitor, client, t1 + k * 60)
    assert kinds() == [obs.DRIFT, obs.DRIFT]


def test_offline_after_being_online_and_back(rig):
    client, links, monitor, _ = rig
    t0 = time.time()
    client.connected = False
    at(monitor, client, t0)
    at(monitor, client, t0 + 300)
    assert obs.list_events() == []

    client.connected = True
    at(monitor, client, t0 + 301)
    client.connected = False
    at(monitor, client, t0 + 330)
    assert obs.list_events() == []
    at(monitor, client, t0 + 400)
    at(monitor, client, t0 + 420)
    (offline,) = obs.list_events()
    assert offline["kind"] == obs.OFFLINE and offline["severity"] == obs.WARNING
    client.connected = True
    at(monitor, client, t0 + 500)
    assert kinds() == [obs.OFFLINE, obs.ONLINE]


def test_an_oscillation_is_reported_once(rig):
    client, _, monitor, _ = rig
    t0 = time.time()
    client.status, client.setpoint = 2, 40.0
    client.samples = [
        {
            "timestamp": t0 - 1200 + i,
            "temp": 40 + (1.0 if (i // 30) % 2 else -1.0),
            "temp_ref": 40.0,
        }
        for i in range(1200)
    ]
    at(monitor, client, t0)
    at(monitor, client, t0 + 61)
    (osc,) = obs.list_events()
    assert osc["kind"] == obs.ANOMALY and osc["facts"]["anomaly"] == "oscillation"


def test_hold_points_and_requests_waiting_for_a_person(rig):
    client, links, monitor, _ = rig
    obs.save_config({"waiting_min": 10})
    professor = ProfessorTools(links)
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    t0 = time.time()
    at(monitor, client, t0)
    assert obs.list_events() == []
    at(monitor, client, t0 + 11 * 60)
    (hold,) = obs.list_events()
    assert hold["kind"] == obs.HOLD and hold["facts"]["campaign_id"] == cid
    assert "Approve and start" in hold["facts"]["options"]

    request = requests_service.create_request("stop", CHAMBER, {}, "Stop the job", "done")
    at(monitor, client, t0 + 12 * 60)
    at(monitor, client, t0 + 12 * 60 + 2)
    waiting = obs.list_events()[0]
    assert waiting["kind"] == obs.REQUEST_WAITING
    assert waiting["facts"]["request_id"] == request["id"]
    assert len(obs.list_events()) == 2


def test_pushes_are_retried_then_given_up(rig):
    client, links, _, _ = rig
    obs.save_config({"push": True})
    attempts = []
    monitor = ObserverMonitor(links, "tok", lambda e, url, token: attempts.append(url) or "down")
    event = obs.record(obs.TEST, obs.WARNING, "test", push=True)
    t0 = time.time()
    monitor.push(obs.load_config(), t0)
    monitor.push(obs.load_config(), t0 + 1)
    assert len(attempts) == 1
    for k in range(1, 10):
        monitor.push(obs.load_config(), t0 + 10_000 * k)
    failed = obs.get_event(event["id"])
    assert len(attempts) == obs.MAX_PUSH_ATTEMPTS
    assert failed["push_state"] == obs.PUSH_FAILED and failed["push_error"] == "down"
    assert obs.queue_push(event["id"])["push_state"] == obs.PUSH_PENDING


def test_deliver_posts_the_event_with_the_bearer_token(deepvac_data_dir):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers["Authorization"], json.loads(body)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"message":"Workflow was started"}')

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/webhook/deepvac-observer"
        event = obs.record(obs.ALARM, obs.CRITICAL, "Alarm on DV-1", {"alarm_code": 4}, CHAMBER)
        assert obs.deliver(event, url, "secret") == ""
        path, auth, body = received[0]
        assert (path, auth) == ("/webhook/deepvac-observer", "Bearer secret")
        assert (
            body["event"]["title"] == "Alarm on DV-1" and body["event"]["facts"]["alarm_code"] == 4
        )
        assert obs.deliver(event, "http://127.0.0.1:1/nothing", "secret") != ""
    finally:
        httpd.shutdown()


def test_the_observer_tools_over_mcp(rig):
    activity.clear()
    client, links, _, _ = rig
    client.samples = [{"timestamp": 1000.0 + i, "temp": 30.0, "temp_ref": 30.0} for i in range(120)]
    client.now = time.time()
    tools = ObserverTools(links)
    event = obs.record(obs.DRIFT, obs.WARNING, "DV-1 drifts", {"error_c": 4.0}, CHAMBER)
    mcp = server.build_observer_server(tools)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "get_event",
        "list_events",
        "get_live_status",
        "report_event",
        "propose_stop",
        "propose_setpoint",
    }

    result = asyncio.run(mcp.call_tool("get_event", {"event_id": event["id"]}))
    data = json.loads(result.content[0].text if hasattr(result, "content") else result[0].text)
    assert data["event"]["facts"]["error_c"] == 4.0
    assert data["chamber_now"]["temperature_c"] == 20.0
    assert data["last_30_min"]["points"][-1]["min_ago"] == 0.0

    with pytest.raises(obs.ObserverError, match="message"):
        tools.report_event(event["id"], "Drifting.", True)
    tools.report_event(event["id"], "Drifting 4 °C low.", True, "Check the door seal on DV-1.")
    reported = obs.get_event(event["id"])
    assert reported["notify"] is True and reported["message"] == "Check the door seal on DV-1."

    request = tools.propose_stop(event["id"], "it keeps drifting")
    stored = requests_service.get_request(request["id"])
    assert (
        stored["status"] == requests_service.PENDING and stored["requested_by"] == "observer agent"
    )
    assert stored["reason"].startswith(f"Observer, event {event['id']}")
    assert obs.get_event(event["id"])["facts"]["proposed_request"] == request["id"]
    assert [e["agent"] for e in activity.since(0)] == ["Observer"]


def test_no_proposals_on_a_chamber_running_a_campaign(rig):
    client, links, _, _ = rig
    professor = ProfessorTools(links)
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    assert cs.require(cid)["state"] == cs.APPROVAL
    event = obs.record(obs.DRIFT, obs.WARNING, "DV-1 drifts", {}, CHAMBER)
    with pytest.raises(obs.ObserverError, match=f"Campaign {cid} runs on DV-1"):
        ObserverTools(links).propose_setpoint(event["id"], 20.0, "back to ambient")
    assert requests_service.list_requests() == []


def test_the_workflow_reads_events_by_id(deepvac_data_dir):
    chambers_service.add_chamber("DV-1", "127.0.0.1", 1, protocol="plc")
    event = obs.record(obs.TEST, obs.WARNING, "test")
    obs.report(event["id"], "A test.", True, "Hello")
    app = server.build_app("secret", AnalystTools(), WorkerTools())
    with TestClient(app) as http:
        listed = http.get(
            f"/observer/events?ids={event['id']}", headers={"Authorization": "Bearer secret"}
        ).json()
        assert listed["events"][0]["notify"] is True and listed["events"][0]["message"] == "Hello"
        assert http.get(f"/observer/events?ids={event['id']}").status_code == 401


def test_settings_keep_defaults_and_refuse_unknown_keys(deepvac_data_dir):
    config = obs.load_config()
    assert config["watch"] is True and config["push"] is False
    assert config["webhook_url"] == obs.DEFAULT_WEBHOOK_URL
    obs.save_config({"push": True, "drift_c": 2.0})
    assert obs.load_config()["drift_c"] == 2.0 and obs.load_config()["push"] is True
    with pytest.raises(obs.ObserverError):
        obs.save_config({"colour": "red"})
