"""Tests of the AI Agents page bridge."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.api.campaign_supervisor import CampaignSupervisor
from app.services import ai_mode, campaign_report, chambers_service, hold_advisor, local_llm
from app.services import campaign_service as cs
from app.views.demo_bridge import DemoBridge
from tests.integration.test_campaigns import FakeClient, decide, tick

pytestmark = pytest.mark.integration


class SampledClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.samples = []

    def plc_samples(self, since, max_points=500, keys=None):
        rows = [s for s in self.samples if s["plc_time"] > since]
        return rows[:: max(1, len(rows) // max_points)]


class Links:
    def __init__(self, client, chamber):
        self._client, self._chamber = client, chamber

    def chambers(self):
        return [self._chamber]

    def resolve(self, reference):
        return self._chamber

    def client(self, chamber, wait=True):
        return self._client


class Host:
    """What the bridge uses of the main window."""

    def __init__(self, links, supervisor):
        self.plc_links = links
        self.current_user = {"name": "Ana"}
        self.agent_api = type("Api", (), {"supervisor": supervisor})()
        self.local_model = type("Model", (), {"status": "ready"})()


@pytest.fixture
def rig(deepvac_data_dir):
    chambers_service.add_chamber("Simulator", "127.0.0.1", 4321, protocol="plc")
    chamber = next(c for c in chambers_service.list_chambers() if c["name"] == "Simulator")
    client = SampledClient()
    links = Links(client, chamber)
    advisor = lambda cid, hold, facts: hold_advisor.advise(cid, hold, facts, {"model": "rules"})  # noqa: E731
    supervisor = CampaignSupervisor(links, advisor=advisor)
    bridge = DemoBridge(host=Host(links, supervisor))
    return bridge, client, supervisor, chamber


def j(text):
    return json.loads(text)


def _draft(bridge, chamber, **extra):
    requirement = {"chamber_id": chamber["id"], "article": "6U CubeSat", "op_min_c": -10,
                   "op_max_c": 45, "level": "acceptance", **extra}  # fmt: skip
    result = j(bridge.draft(json.dumps(requirement)))
    assert result["ok"], result
    return result["campaign"]


def test_the_page_drafts_submits_and_a_person_decides(rig):
    bridge, client, supervisor, chamber = rig
    catalog = j(bridge.catalog())
    assert catalog["articles"][0]["name"].startswith("6U CubeSat") and catalog["user"] == "Ana"
    client.now = __import__("time").time()
    state = j(bridge.state(0))
    assert state["chamber_id"] == chamber["id"] and state["campaign"] is None
    assert state["chamber"]["connected"] and state["agent_api"]

    cid = _draft(bridge, chamber)
    state = j(bridge.state(chamber["id"]))
    assert (
        state["campaign"]["state"] == "draft" and state["campaign"]["log"][0]["actor"] == "Planner"
    )
    assert j(bridge.submit(cid))["ok"]
    hold = j(bridge.state(chamber["id"]))["campaign"]["hold_point"]
    assert hold["kind"] == "approval"
    assert j(bridge.decide(hold["id"], "approve"))["ok"]
    assert cs.decisions(cid)[0]["by"] == "Ana"
    bad = j(bridge.decide(hold["id"], "approve"))
    assert not bad["ok"] and "already decided" in bad["error"]


def test_the_series_are_on_the_plcs_clock(rig):
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber, rehearsal=True)
    bridge.submit(cid)
    decide(cid, "approve")
    tick(supervisor, client, 1000.0)
    start = cs.require(cid)["runtime"]["started_plc"]
    client.samples = [
        {
            "plc_time": start + 60 * i,
            "temp": 20 + i,
            "temp_ref": 40.0,
            "pres": 760.0 / (i + 1),
            "temp1": 19.0 + i,
        }  # fmt: skip
        for i in range(30)
    ]
    series = j(bridge.state(chamber["id"]))["series"]
    assert series["t_h"][0] == pytest.approx(1 / 60, abs=1e-3) and series["t_h"][
        -1
    ] == pytest.approx(29 / 60, abs=1e-3)
    assert series["temp"][0] == 21 and series["duts"][0][0] == 20.0 and series["duts"][1][0] is None


def test_a_single_ramp_has_one_plateau(rig):
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber, test_type="ramp", direction="down")
    campaign = cs.require(cid)
    kinds = [s["kind"] for s in campaign["steps"]]
    assert kinds == ["pump", "ramp", "dwell", "ambient", "vent"]
    assert campaign["steps"][1]["target_c"] == -20.0
    assert cs.spec_line(campaign["spec"]).startswith("single ramp down to -20 °C")
    rows = {r[0]: r[1] for r in cs.spec_rows(campaign["spec"])}
    assert rows["Test type"] == "Single ramp down, for accuracy" and "Hot plateau" not in rows
    assert "Test level" not in rows


def test_declining_the_approval_returns_to_the_plan(rig):
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber)
    assert j(bridge.submit(cid))["ok"]
    hold = j(bridge.state(chamber["id"]))["campaign"]["hold_point"]
    assert j(bridge.decide(hold["id"], "decline"))["ok"]
    supervisor.run_once()
    campaign = cs.require(cid)
    assert campaign["state"] == "draft" and cs.stage_of(campaign) == "plan"
    assert cs.log_entries(cid)[-1]["action"] == "Approval declined"
    assert j(bridge.submit(cid))["ok"]


def test_the_requirements_margin_and_cycles(rig):
    bridge, client, supervisor, chamber = rig
    spec = cs.require(_draft(bridge, chamber, level="qualification", margin_c=10, cycles=4))["spec"]
    assert (spec["hot_c"], spec["cold_c"], spec["cycles"]) == (55.0, -20.0, 4)
    assert "qualification level" in spec["sources"]["cycles"]
    cs.cancel(cs.latest_campaign()["id"], "Ana")
    spec = cs.require(_draft(bridge, chamber, level="qualification", margin_c=15, cycles=6))["spec"]
    assert (spec["hot_c"], spec["cold_c"], spec["cycles"]) == (60.0, -25.0, 6)
    assert "default is 10 °C" in spec["sources"]["hot_c"]
    assert "default is 4" in spec["sources"]["cycles"]
    cs.cancel(cs.latest_campaign()["id"], "Ana")
    spec = cs.require(_draft(bridge, chamber, test_type="ramp", direction="up", margin_c=3))["spec"]
    assert spec["hot_c"] == 48.0 and spec["cycles"] == 1


def test_rogue_commands_are_refused_and_logged(rig):
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber, rehearsal=True)
    bridge.submit(cid)
    decide(cid, "approve")
    tick(supervisor, client, 1000.0)
    for kind in ("analyst_output", "worker_alarm", "worker_job", "operator_alarm"):
        assert j(bridge.rogue(cid, kind))["ok"]
    tick(supervisor, client, 1001.0)
    blocked = cs.require(cid)["blocked"]
    assert [b["actor"] for b in blocked] == ["Analyst", "Worker", "Worker", "Ana (Edit level)"]
    reasons = [" ".join(b["reasons"]) for b in blocked]
    assert "Nobody forces PLC outputs" in reasons[0]
    assert "Service Engineer" in reasons[1] and "Service Engineer" in reasons[3]
    assert (
        "Only the run supervisor" in reasons[2]
        and "not one of the campaign's plateaus" in reasons[2]
    )
    assert sum(e["action"] == "Command blocked" for e in cs.log_entries(cid)) == 4
    assert cs.verify_chain(cid)["ok"]


def test_tampering_breaks_the_chain_until_undone(rig):
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber)
    bridge.submit(cid)
    cs.record(cid, "Ana", "Note", "one more")
    seq = j(bridge.tamper(cid))["seq"]
    campaign = j(bridge.state(chamber["id"]))["campaign"]
    assert campaign["tampered"] and campaign["verify"] == {
        "ok": False,
        "entries": 4,
        "broken_at": seq,
    }
    assert not j(bridge.tamper(cid))["ok"]
    assert j(bridge.untamper(cid))["undone"]
    assert j(bridge.state(chamber["id"]))["campaign"]["verify"]["ok"]


def test_the_simulator_commands_and_the_reset(rig):
    bridge, client, supervisor, chamber = rig
    bridge.fault(chamber["id"])
    bridge.speed(chamber["id"], 300.0)
    cid = _draft(bridge, chamber)
    bridge.submit(cid)
    assert j(bridge.resetDemo(chamber["id"]))["ok"]
    assert client.commands == ["sim_fault", "sim_speed 300", "sim_reset"]
    assert cs.require(cid)["state"] == "cancelled"


class _FakeModel(BaseHTTPRequestHandler):
    content = "OK"
    bodies = []

    def do_GET(self):  # noqa: N802
        self._send({"status": "ok"})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeModel.bodies.append(body)
        self._send({"choices": [{"message": {"content": _FakeModel.content}}]})

    def _send(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_model(deepvac_data_dir):
    server = HTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeModel.bodies = []
    ai_mode.save(mode="offline", local_url=f"http://127.0.0.1:{server.server_port}/v1")
    yield server
    server.shutdown()


def _ended_campaign(rig):
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber, rehearsal=True)
    bridge.submit(cid)
    decide(cid, "approve")
    tick(supervisor, client, 1000.0)
    cs.request_stop(cid, "Ana", "enough")
    tick(supervisor, client, 1001.0)
    assert cs.require(cid)["state"] == "aborted"
    return cid


def test_offline_the_local_model_writes_the_report_summary(rig, local_model):
    cid = _ended_campaign(rig)
    _FakeModel.content = "The 6U CubeSat campaign was stopped by Ana before cycling."
    campaign_report.issue_with_ai(cid, by="Ana")
    campaign = cs.require(cid)
    assert campaign["summary"].startswith("The 6U CubeSat campaign was stopped")
    assert "Reporter (local model)" in campaign["summary_source"]
    assert '"state": "aborted"' in _FakeModel.bodies[-1]["messages"][0]["content"]


def test_offline_without_a_model_the_template_writes_it(rig, deepvac_data_dir):
    ai_mode.save(mode="offline", local_url="http://127.0.0.1:9/v1")
    cid = _ended_campaign(rig)
    campaign_report.issue_with_ai(cid, by="Ana")
    assert cs.require(cid)["summary_source"] == "Written from a template."


def test_the_local_model_server(local_model, monkeypatch):
    assert local_llm.available()
    server = local_llm.LocalModelServer()
    done = []
    server._ensure(done.append)
    assert done == ["running elsewhere"]
    local_model.shutdown()
    monkeypatch.setattr(ai_mode, "llm_dir", lambda: None)
    ai_mode.save(local_url="http://127.0.0.1:9/v1")
    server._ensure(done.append)
    assert done[-1] == "missing"
    folder = __import__("pathlib").Path("C:/llm")
    command = local_llm.start_command(folder)
    assert command[0].endswith("llama-server.exe") and command[command.index("--port") + 1] == "9"


def test_the_report_is_issued_once(rig):
    bridge, client, supervisor, chamber = rig
    cid = _ended_campaign(rig)
    assert j(bridge.templateReport(cid))["ok"]
    again = j(bridge.templateReport(cid))
    assert not again["ok"] and "already issued" in again["error"]
    assert not j(bridge.issueReport(cid))["ok"]
    assert sum(e["action"] == "Report issued" for e in cs.log_entries(cid)) == 1


def test_the_history_shows_any_campaign_and_its_report_is_signed_off(rig):
    bridge, client, supervisor, chamber = rig
    first = _ended_campaign(rig)
    assert j(bridge.templateReport(first))["ok"]
    second = _draft(bridge, chamber, notes="the next one")
    listing = j(bridge.campaigns())
    assert [c["id"] for c in listing][:2] == [second, first]
    assert listing[1]["report_issued_at"] and listing[1]["chamber"] == "Simulator"
    current = j(bridge.state(chamber["id"], 0))["campaign"]
    assert current["id"] == second and current["current"] and current["notes"] == "the next one"
    earlier = j(bridge.state(chamber["id"], first))
    assert earlier["campaign"]["id"] == first and not earlier["campaign"]["current"]
    assert earlier["campaign"]["state"] == "aborted" and "t_h" in earlier["series"]
    assert j(bridge.signOff(first))["ok"]
    assert cs.require(first)["signed_off_by"] == "Ana"
    assert not j(bridge.signOff(first))["ok"]
    assert j(bridge.state(chamber["id"], first))["campaign"]["signed_off_by"] == "Ana"
