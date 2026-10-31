"""Test campaigns: specification, hold points, run supervisor, chained log,
report and Professor tools."""

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from app.api import activity, server
from app.api.analyst_tools import AnalystTools
from app.api.campaign_supervisor import CampaignSupervisor
from app.api.chambers import ChamberLinks
from app.api.professor_tools import ProfessorTools
from app.api.worker_tools import WorkerTools
from app.services import campaign_report, chambers_service, plc_gate
from app.services import campaign_service as cs

CHAMBER = {"id": 7, "name": "DV-1", "protocol": "plc", "host": "127.0.0.1", "port": 1}


class FakeClient:
    """A chamber whose temperature the test sets; records what was sent."""

    def __init__(self):
        self.temp = 20.0
        self.setpoint = 20.0
        self.status = 1
        self.alarm = 0
        self.connected = True
        self.jobs = []
        self.commands = []
        self.now = 0.0

    def snapshot(self):
        return plc_gate.PlcSnapshot(
            connected=self.connected,
            address="fake",
            values={
                "temp": self.temp,
                "temp_ref": self.setpoint,
                "status_condition": float(self.status),
            },
            values_at=self.now,
            alarm_code=self.alarm,
        )

    def send_job(self, prog_type, intervals, params, cycle=None):
        self.jobs.append(intervals)
        self.status = 2
        self.setpoint = intervals[-1]["temp"]

    def send_text_cmd(self, command):
        self.commands.append(command)
        if command == "stop":
            self.status = 1


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
    return client, CampaignSupervisor(links), ProfessorTools(links)


def tick(supervisor, client, now):
    client.now = now
    supervisor.run_once(now)
    return cs.latest_campaign()


def decide(campaign_id, choice, user="Ana"):
    hold = cs.open_hold_of(campaign_id)
    assert hold is not None, "expected an open hold point"
    return cs.decide_hold(hold["id"], choice, user)


def test_specification_comes_from_the_pack_with_sources(rig):
    _, _, professor = rig
    draft = professor.draft_campaign("Star tracker", -20, 50, "qualification")
    assert draft["state"] == "draft" and draft["stage"] == "plan"
    rows = {r["item"]: r for r in draft["spec_rows"]}
    assert rows["Hot plateau"]["value"] == "+60 °C"
    assert "plus a 10 °C margin" in rows["Hot plateau"]["source"]
    assert rows["Cold plateau"]["value"] == "-30 °C"
    assert rows["Thermal cycles"]["value"] == "4"
    assert draft["steps"][0].startswith("Pump down") and draft["steps"][-1].startswith("Vent")

    with pytest.raises(cs.CampaignError, match="Give the reason"):
        professor.draft_campaign("Bus", -5, 40, "acceptance", cycles=1)
    changed = professor.draft_campaign(
        "Bus", -5, 40, "acceptance", cycles=1, reason="customer asks for one"
    )
    assert any("Changed by Professor" in r["source"] for r in changed["spec_rows"])
    with pytest.raises(cs.CampaignError, match="outside the chamber's setpoint range"):
        professor.draft_campaign("Too hot", 0, 145, "qualification")


def test_a_campaign_runs_from_approval_to_a_signed_report(rig):
    client, supervisor, professor = rig
    draft = professor.draft_campaign(
        "CubeSat", 10, 30, "acceptance", cycles=1, dwell_min=1, reason="short test"
    )
    cid = draft["id"]
    spec = cs.require(cid)["spec"]
    assert (spec["hot_c"], spec["cold_c"]) == (35.0, 5.0)

    professor.submit_campaign(cid)
    c = tick(supervisor, client, 1000)
    assert c["state"] == "approval" and not client.jobs
    professor.recommend(cid, "approve", "Values follow the pack.")
    decide(cid, "approve")

    c = tick(supervisor, client, 1001)
    assert c["state"] == "holding" and cs.open_hold_of(cid)["kind"] == "pump"
    assert not client.jobs
    decide(cid, "confirm")

    c = tick(supervisor, client, 1002)
    assert c["state"] == "running" and len(client.jobs) == 1
    ramp, hold = client.jobs[0]
    assert ramp["temp"] == 35.0 and ramp["flags"] & (1 << plc_gate.RAMP_BIT)
    assert ramp["duration"] == int(15 / spec["ramp_c_per_min"] * 60)
    assert hold["flags"] & (1 << plc_gate.AWAIT_BIT)

    t = 1002 + ramp["duration"] + 1
    c = tick(supervisor, client, t)
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "dwell"
    client.temp = 34.0
    c = tick(supervisor, client, t + 10)
    assert c["runtime"]["stabilised_at"] == t + 10

    client.temp = 39.5
    tick(supervisor, client, t + 20)
    c = tick(supervisor, client, t + 20 + spec["excursion_s"])
    assert c["state"] == "holding" and cs.open_hold_of(cid)["kind"] == "anomaly"
    assert c["deviations"][-1]["kind"] == "excursion"
    client.temp = 35.2
    decide(cid, "extend_dwell")
    resumed = t + 20 + spec["excursion_s"] + 300
    c = tick(supervisor, client, resumed)
    assert c["state"] == "running" and c["runtime"]["dwell_extra_min"] == spec["extend_dwell_min"]

    dwell_s = (spec["dwell_min"] + spec["extend_dwell_min"]) * 60
    c = tick(supervisor, client, t + 10 + 300 + dwell_s + 1)
    assert c["results"][0]["plateau"] == "hot"
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "ramp"
    assert client.jobs[-1][0]["temp"] == 5.0

    now = t + 10 + 300 + dwell_s + 1
    now += client.jobs[-1][0]["duration"] + 1
    client.temp = 5.5
    tick(supervisor, client, now)
    tick(supervisor, client, now + 1)
    c = tick(supervisor, client, now + 2 + spec["dwell_min"] * 60)
    assert [r["plateau"] for r in c["results"]] == ["hot", "cold"]
    assert client.jobs[-1][0]["temp"] == spec["ambient_c"]

    now += 10 + spec["dwell_min"] * 60 + client.jobs[-1][0]["duration"]
    client.temp = 20.5
    c = tick(supervisor, client, now)
    assert cs.open_hold_of(cid)["kind"] == "vent"
    client.temp = 5.0
    decide(cid, "approve")
    c = tick(supervisor, client, now + 1)
    assert c["state"] == "holding" and c["blocked"][-1]["command"] == "vent the chamber"
    client.temp = 21.0
    decide(cid, "approve")
    c = tick(supervisor, client, now + 2)
    assert c["state"] == "completed" and client.commands[-1] == "stop"

    files = professor.issue_report(cid, "One cycle completed between +5 °C and +35 °C.")
    assert files["chain_ok"] and files["signed"]
    text = Path(cs.require(cid)["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "Drafted by Professor" in text
    assert "Extend the dwell" in text and "Approve and start" in text
    assert "Venting is locked" in text
    assert cs.verify_chain(cid)["ok"]

    campaign_report.sign_off(cid, "Ana")
    text = Path(cs.require(cid)["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "Signed off by Ana" in text


def test_the_log_detects_tampering(rig):
    _, _, professor = rig
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    cs.record(cid, "Operator", "Note", "first")
    cs.record(cid, "Operator", "Note", "second")
    assert cs.verify_chain(cid) == {
        "ok": True,
        "entries": 3,
        "last_hash": cs.log_entries(cid)[-1]["hash"],
    }
    conn = cs.connect()
    conn.execute(
        "UPDATE campaign_log SET detail = 'changed' WHERE campaign_id = ? AND seq = 2", (cid,)
    )
    conn.commit()
    conn.close()
    assert cs.verify_chain(cid) == {"ok": False, "entries": 3, "broken_at": 2}


def test_an_interrupted_job_holds_and_abort_returns_to_ambient(rig):
    client, supervisor, professor = rig
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, 10)
    decide(cid, "confirm")
    tick(supervisor, client, 11)
    tick(supervisor, client, 12)
    client.status = 4
    c = tick(supervisor, client, 13)
    hold = cs.open_hold_of(cid)
    assert c["state"] == "holding" and hold["kind"] == "interrupted" and "alarm" in hold["detail"]

    decide(cid, "resume")
    c = tick(supervisor, client, 14)
    assert "in alarm" in c["blocked"][-1]["reasons"][0]
    assert cs.open_hold_of(cid)["kind"] == "interrupted"

    client.status = 1
    decide(cid, "ambient")
    c = tick(supervisor, client, 15)
    assert c["runtime"]["aborted"] and client.jobs[-1][0]["temp"] == 20.0


def test_only_one_active_campaign_per_chamber_and_operator_stop(rig):
    client, supervisor, professor = rig
    first = professor.draft_campaign("A", 10, 30, rehearsal=True)["id"]
    second = professor.draft_campaign("B", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(first)
    with pytest.raises(cs.CampaignError, match="already approval"):
        professor.submit_campaign(second)
    decide(first, "approve")
    tick(supervisor, client, 1)
    cs.request_stop(first, "Ana", "end of the day")
    c = tick(supervisor, client, 2)
    assert c["id"] == second or cs.require(first)["state"] == "aborted"
    assert cs.require(first)["state"] == "aborted" and client.commands[-1] == "stop"
    assert cs.open_hold_of(first) is None


def test_the_professor_tools_over_mcp(rig):
    activity.clear()
    _, _, professor = rig
    mcp = server.build_professor_server(professor)
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"draft_campaign", "submit_campaign", "recommend", "issue_report"} <= tools
    result = asyncio.run(
        mcp.call_tool(
            "draft_campaign",
            {"article": "CubeSat", "op_min_c": 10, "op_max_c": 30, "rehearsal": True},
        )
    )
    campaign = json.loads(result.content[0].text)
    assert campaign["state"] == "draft"
    event = activity.since(0)[-1]
    assert event["agent"] == "Professor" and event["campaign"] == campaign["id"]
    assert "drafting a qualification campaign for CubeSat" in event["text"]
    with pytest.raises(ToolError, match="no hold point"):
        asyncio.run(
            mcp.call_tool(
                "recommend", {"campaign_id": campaign["id"], "option": "approve", "rationale": "ok"}
            )
        )


def test_the_chat_reads_campaigns_by_id(deepvac_data_dir):
    chambers_service.add_chamber("DV-1", "127.0.0.1", 1, protocol="plc")
    cid = ProfessorTools().draft_campaign("CubeSat", 10, 30, chamber="DV-1", rehearsal=True)["id"]
    app = server.build_app("secret", AnalystTools(), WorkerTools())
    with TestClient(app) as client:
        listed = client.get(
            f"/campaigns?ids={cid}", headers={"Authorization": "Bearer secret"}
        ).json()
    assert listed["campaigns"][0]["title"] == "CubeSat"
    assert listed["campaigns"][0]["news"].startswith("draft|")


def test_the_supervisor_drives_the_fake_plc_over_tcp(deepvac_data_dir):
    from tests.integration.test_agent_api import _load_fake_plc

    fake = _load_fake_plc()
    chamber = fake.Chamber(24.0)
    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(1)
    port = server_socket.getsockname()[1]

    def accept():
        while True:
            try:
                conn, _ = server_socket.accept()
            except OSError:
                return
            threading.Thread(
                target=fake.serve_client, args=(conn, chamber, 0.05), daemon=True
            ).start()

    threading.Thread(target=accept, daemon=True).start()
    chambers_service.add_chamber("Fake PLC", "127.0.0.1", port, protocol="plc")
    links = ChamberLinks(connect_timeout_s=5)
    try:
        links.client(links.resolve("Fake PLC"))
        professor = ProfessorTools(links)
        supervisor = CampaignSupervisor(links)
        cid = professor.draft_campaign("CubeSat", 10, 30, chamber="Fake PLC", rehearsal=True)["id"]
        professor.submit_campaign(cid)
        decide(cid, "approve")
        supervisor.run_once()
        decide(cid, "confirm")
        supervisor.run_once()
        deadline = time.monotonic() + 3
        while chamber.status != 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert chamber.status == 2 and chamber.intervals[0]["temp"] == 40.0
        assert chamber.intervals[0]["flags"] & (1 << plc_gate.RAMP_BIT)
        time.sleep(0.3)
        supervisor.run_once()
        assert cs.require(cid)["runtime"]["job_confirmed"]
        cs.request_stop(cid, "Ana")
        supervisor.run_once()
        deadline = time.monotonic() + 3
        while chamber.status != 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert chamber.status == 1 and cs.require(cid)["state"] == "aborted"
    finally:
        links.close()
        server_socket.close()
