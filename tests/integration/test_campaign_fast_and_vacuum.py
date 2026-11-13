"""Tests of campaigns on the PLC clock and the gauge pump-down."""

import pytest

from app.api.campaign_supervisor import CampaignSupervisor
from app.api.professor_tools import ProfessorTools
from app.services import campaign_service as cs
from app.services import hold_advisor, plc_gate
from tests.integration.test_campaigns import FakeClient, FakeLinks, decide

pytestmark = pytest.mark.integration

SPEED = 300.0
T0 = 1_000_000.0


def _values(t_total, working=True):
    return {"status_condition": 2.0 if working else 1.0, "t_total": t_total}


def test_the_clock_follows_the_plcs_job_runtime():
    clock = plc_gate.PlcClock()
    assert clock.advance(T0, _values(0.0)) == T0
    for i in range(1, 11):
        clock.advance(T0 + i, _values(i * SPEED))
    assert clock.time == pytest.approx(T0 + 3000) and clock.rate == pytest.approx(SPEED)
    clock.advance(T0 + 11, _values(12.0))
    assert clock.time == pytest.approx(T0 + 3000 + SPEED)
    clock.advance(T0 + 12, _values(312.0))
    assert clock.time == pytest.approx(T0 + 3000 + 2 * SPEED)
    clock.advance(T0 + 14, _values(0.0, working=False))
    assert clock.time == pytest.approx(T0 + 3000 + 4 * SPEED)


def test_a_real_plc_keeps_this_pcs_time():
    clock = plc_gate.PlcClock()
    for i in range(0, 120):
        clock.advance(T0 + i * 0.5, _values(float(int(i * 0.5))))
    assert clock.rate == pytest.approx(1.0, abs=0.05)
    assert clock.time == pytest.approx(T0 + 59.5, abs=1.0)


class FastClient(FakeClient):
    """Fake PLC with a faster clock and an optional pressure gauge."""

    def __init__(self, gauge=False):
        super().__init__()
        self.gauge = gauge
        self.pressure = 760.0
        self.pump_torr_per_s = None
        self.pumped_s = 0.0
        self.last_plc = None

    def plc_now(self):
        return T0 + (self.now - T0) * SPEED

    def snapshot(self):
        snap = super().snapshot()
        snap.plc_time, snap.plc_rate = self.plc_now(), SPEED
        if self.gauge:
            if self.last_plc is not None and self.status == 2:
                self.pumped_s += self.plc_now() - self.last_plc
            self.last_plc = self.plc_now()
            if self.pump_torr_per_s:
                self.pressure = self.pump_torr_per_s(self.pumped_s)
            snap.values["pres"] = self.pressure
        return snap

    def plc_history(self, since=None):
        return []


@pytest.fixture
def rig(deepvac_data_dir):
    def make(gauge=False):
        client = FastClient(gauge)
        links = FakeLinks(client)
        advisor = lambda cid, hold, facts: hold_advisor.advise(cid, hold, facts, {"model": "rules"})  # noqa: E731
        return client, CampaignSupervisor(links, advisor=advisor), ProfessorTools(links)

    return make


def tick(supervisor, client, wall):
    client.now = wall
    supervisor.run_once(wall)
    return cs.latest_campaign()


def test_a_campaign_is_timed_with_the_plcs_clock(rig):
    client, supervisor, professor = rig()
    cid = professor.draft_campaign(
        "CubeSat", 10, 30, "acceptance", cycles=1, dwell_min=240, reason="the pack's dwell"
    )["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, T0)
    decide(cid, "confirm")
    c = tick(supervisor, client, T0 + 1)
    ramp = client.jobs[-1][0]
    assert ramp["temp"] == 35.0 and ramp["duration"] == 450

    c = tick(supervisor, client, T0 + 3)
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "dwell"
    client.temp = 35.0
    c = tick(supervisor, client, T0 + 4)
    assert c["runtime"]["stabilised_at"] == pytest.approx(client.plc_now())
    assert cs.step_progress(c, now=client.plc_now() + 3600)["dwell_elapsed_min"] == 60.0

    c = tick(supervisor, client, T0 + 4 + 240 * 60 / SPEED + 1)
    assert [r["plateau"] for r in c["results"]] == ["hot"]
    assert c["results"][0]["dwell_min"] == pytest.approx(240 + SPEED / 60, abs=0.1)
    assert c["runtime"]["clock"]["rate"] == SPEED


def test_the_gauge_starts_cycling_at_the_start_pressure(rig):
    client, supervisor, professor = rig(gauge=True)
    client.pump_torr_per_s = lambda s: 760.0 * 10 ** (-s / 1200.0)
    draft = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)
    assert draft["steps"][0] == "Pump down to 7.5×10⁻⁶ Torr"
    rows = {r["item"]: r["value"] for r in draft["spec_rows"]}
    assert "gauge reads 7.5×10⁻⁶ Torr or less" in rows["Vacuum"]
    cid = draft["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    c = tick(supervisor, client, T0)
    assert c["state"] == "running" and cs.open_hold_of(cid) is None
    assert client.jobs[-1][-1]["temp"] == 20.0
    pumping = next(e for e in cs.log_entries(cid) if e["action"] == "Pumping down")
    assert "7.5×10⁻⁶ Torr or less" in pumping["detail"] and "hPa" not in pumping["detail"]

    c = tick(supervisor, client, T0 + 10)
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "pump"
    c = tick(supervisor, client, T0 + 40)
    assert c["runtime"]["pump_confirmed"]
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "ramp"
    assert client.jobs[-1][0]["temp"] == 40.0
    pumped = next(e for e in cs.log_entries(cid) if e["action"] == "Pumped down")
    assert "at or below 7.5×10⁻⁶ Torr" in pumped["detail"]


def test_a_slow_pump_down_holds_and_can_be_aborted(rig):
    client, supervisor, professor = rig(gauge=True)
    client.pump_torr_per_s = lambda s: max(760.0 * 10 ** (-s / 600.0), 2e-4)
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, T0)
    tick(supervisor, client, T0 + 60)
    timeout_s = cs.require(cid)["spec"]["pump_timeout_min"] * 60
    c = tick(supervisor, client, T0 + timeout_s / SPEED + 1)
    hold = cs.open_hold_of(cid)
    assert c["state"] == "holding" and hold["kind"] == "pump_slow"
    assert hold["recommendation"]["option"] == "abort"
    assert "isn't falling" in hold["recommendation"]["rationale"]

    decide(cid, "abort")
    c = tick(supervisor, client, T0 + timeout_s / SPEED + 2)
    assert c["state"] == "aborted" and client.commands[-1] == "stop"


def test_the_gateway_needs_the_pump_down_before_cycling(rig):
    client, supervisor, professor = rig(gauge=True)
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, T0)
    from app.api.campaign_supervisor import SUPERVISOR, Context

    ctx = Context(cs.require(cid))
    ctx.wall = T0
    reasons = supervisor.gateway(
        ctx, SUPERVISOR, {"name": "setpoint", "target_c": 35.0, "start_cycling": True}, T0
    )
    assert any("pumped down" in r for r in reasons)


def test_a_ramp_ends_with_the_plcs_setpoint_and_a_slow_one_isnt_a_takeover(rig):
    client, supervisor, professor = rig()
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, T0)
    decide(cid, "confirm")
    tick(supervisor, client, T0 + 1)
    client.setpoint = 30.0
    c = tick(supervisor, client, T0 + 1 + 600 / SPEED + 1)
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "ramp"
    client.setpoint = 36.0
    c = tick(supervisor, client, T0 + 10)
    assert cs.open_hold_of(cid) is None
    client.setpoint = 40.0
    c = tick(supervisor, client, T0 + 11)
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "dwell"


def test_the_real_clients_snapshot_keeps_every_field_in_its_place():
    client = plc_gate.PlcGateClient("127.0.0.1", 1)
    client._handle(plc_gate.IN_STATE_NAMES, ["temp", "status_condition", "t_total"])
    client._handle(plc_gate.IN_SETTINGS_MAP, {"p1p0": 5.0})
    client._handle(plc_gate.IN_STATE_VALUES, [20.0, 2.0, 0.0])
    snap = client.snapshot()
    assert snap.plc_rate == 1.0 and snap.settings == {"p1p0": 5.0}
    assert abs(snap.plc_time - snap.values_at) < 1 and abs(snap.settings_at - snap.values_at) < 1
    assert snap.values == {"temp": 20.0, "status_condition": 2.0, "t_total": 0.0}
    snap.values["temp"] = 99.0
    assert client.snapshot().values["temp"] == 20.0


def test_a_long_hold_point_keeps_the_plc_job_renewed(rig):
    client, supervisor, professor = rig()
    cid = professor.draft_campaign("CubeSat", 10, 30, rehearsal=True)["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, T0)
    decide(cid, "confirm")
    tick(supervisor, client, T0 + 1)
    client.setpoint = 40.0
    client.temp = 40.0
    tick(supervisor, client, T0 + 4)
    tick(supervisor, client, T0 + 5)
    cs.open_hold(cid, "anomaly", "Hold", "a long one")
    jobs = len(client.jobs)
    hold_s = cs.require(cid)["runtime"]["job_hold_s"]
    wall = T0 + 6
    for _ in range(3):
        wall += hold_s / SPEED / 2
        tick(supervisor, client, wall)
    assert len(client.jobs) >= jobs + 2
    assert cs.open_hold_of(cid)["kind"] == "anomaly"
