"""Tests of auto-play."""

import pytest

from app.api.campaign_supervisor import CampaignSupervisor
from app.services import campaign_service as cs
from app.services import hold_advisor
from app.services.demo_autoplay import ACTOR, HOLD_WAIT_S, NEXT_RUN_S, AutoPlay
from tests.integration.test_demo_bridge import Links, SampledClient

pytestmark = pytest.mark.integration


class Host:
    def __init__(self, links, chamber_id):
        self.plc_links = links
        self.agent_api = object()
        self.demo = type("Demo", (), {"chamber_id": chamber_id})()


@pytest.fixture
def rig(deepvac_data_dir, qapp):
    from app.services import chambers_service

    chambers_service.add_chamber("Simulator (demo)", "127.0.0.1", 4321, protocol="plc")
    chamber = next(c for c in chambers_service.list_chambers() if c["name"] == "Simulator (demo)")
    client = SampledClient()
    links = Links(client, chamber)
    supervisor = CampaignSupervisor(
        links, advisor=lambda cid, h, f: hold_advisor.advise(cid, h, f, {"model": "rules"})
    )
    play = AutoPlay(Host(links, chamber["id"]))
    play.on = True
    return play, client, supervisor


def test_auto_play_runs_a_campaign_through_its_hold_points_and_fault(rig):
    play, client, supervisor = rig
    t = __import__("time").time()

    def step(dt=1.0):
        nonlocal t
        t += dt
        client.now = t
        supervisor.run_once(t)
        return play.step(t)

    did = []
    for _ in range(4000):
        c = cs.latest_campaign()
        if c is not None:
            s = cs.step_progress(c)
            if s and s.get("target_c") is not None:
                client.temp = client.setpoint = s["target_c"]
        action = step(HOLD_WAIT_S + 1 if c and cs.open_hold_of(c["id"]) else 300.0)
        if action:
            did.append(action)
        c = cs.latest_campaign()
        if c and c["state"] in cs.ENDED and c.get("report_issued_at"):
            break
    c = cs.latest_campaign()
    assert did[0].startswith("drafted") and "submitted" in did
    assert "decided approval: approve" in did and "decided pump: confirm" in did
    assert "fault injected" in did and "sim_fault" in client.commands
    assert "decided vent: approve" in did and "report" in did
    assert c["state"] == "completed" and c["summary_source"] == "Written from a template."
    assert all(d["by"] == ACTOR for d in cs.decisions(c["id"]))
    fault = next(e for e in cs.log_entries(c["id"]) if e["action"] == "PID settings changed")
    assert fault["actor"] == "PLC"

    assert play.step(t + 5) is None
    assert play.step(t + 5 + NEXT_RUN_S + 1).startswith("drafted")
    assert client.commands[-1] == "sim_reset" and cs.latest_campaign()["id"] != c["id"]


def _steps(*rows):
    keys = ("kind", "label", "cycle", "plateau")
    return {"steps": [dict(zip(keys, row, strict=True)) for row in rows]}


def test_the_fault_goes_into_cycle_2s_hot_dwell_or_the_only_one(rig):
    two = _steps(
        ("dwell", "Cycle 1: hot dwell", 1, "hot"), ("dwell", "Cycle 2: hot dwell", 2, "hot")
    )
    one = _steps(("dwell", "Cycle 1: cold dwell", 1, "cold"))
    assert AutoPlay.fault_step(two) == "Cycle 2: hot dwell"
    assert AutoPlay.fault_step(one) == "Cycle 1: cold dwell"
