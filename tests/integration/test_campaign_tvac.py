"""Tests of thermal-vacuum campaign rules on the test article."""

from types import SimpleNamespace

import pytest

from app.api.campaign_supervisor import CampaignSupervisor
from app.api.professor_tools import ProfessorTools
from app.services import campaign_service as cs
from app.services import hold_advisor
from tests.integration.test_campaign_fast_and_vacuum import SPEED, T0, FastClient
from tests.integration.test_campaigns import FakeLinks, decide

pytestmark = pytest.mark.integration

TCS = ("temp1", "temp2", "temp3", "temp4")


class TvacClient(FastClient):
    """Fake PLC with a platen, thermocouples and sample history."""

    def __init__(self, gauge=False):
        super().__init__(gauge)
        self.platen = 20.0
        self.tcs = dict.fromkeys(TCS, 20.0)
        self.frozen = set()
        self.samples = []

    def readings(self):
        """The thermocouples with a little sensor noise, as real ones always have."""
        noise = 0.02 * (-1) ** len(self.samples)
        return {k: v if k in self.frozen or v > 400 else v + noise for k, v in self.tcs.items()}

    def snapshot(self):
        snap = super().snapshot()
        snap.values.update(self.readings(), temp_platen=self.platen)
        return snap

    def record(self):
        self.samples.append({"plc_time": self.plc_now(), **self.readings(), "temp": self.temp})

    def plc_samples(self, since, max_points=500, keys=None):
        return [
            {"plc_time": s["plc_time"], **{k: s[k] for k in keys or s if k in s}}
            for s in self.samples
            if s["plc_time"] > since
        ]


@pytest.fixture
def rig(deepvac_data_dir):
    def make(gauge=False):
        client = TvacClient(gauge)
        links = FakeLinks(client)
        advisor = lambda cid, hold, facts: hold_advisor.advise(cid, hold, facts, {"model": "rules"})  # noqa: E731
        return client, CampaignSupervisor(links, advisor=advisor), ProfessorTools(links)

    return make


def tick(supervisor, client, wall):
    client.now = wall
    client.record()
    supervisor.run_once(wall)
    return cs.latest_campaign()


def run_for(supervisor, client, wall, chamber_s, step_s=60.0):
    """Ticks the supervisor for a span of chamber time."""
    for _ in range(int(chamber_s / step_s)):
        wall += step_s / SPEED
        c = tick(supervisor, client, wall)
    return wall, c


def started(rig, gauge=False):
    """Starts an approved 6U CubeSat campaign."""
    client, supervisor, professor = rig(gauge)
    draft = professor.draft_campaign("6U CubeSat", -10, 45, "acceptance", cycles=1, reason="demo")
    rows = {r["item"]: r["value"] for r in draft["spec_rows"]}
    assert "Thermocouples" not in rows
    assert rows["Stabilisation"].startswith("Every valid test-article temperature within ±3 °C")
    assert "3 °C/h over 15 min" in rows["Stabilisation"]
    assert "Shroud (the PLC's control sensor), platen and 4 temperatures" in rows["Sensors"]
    cid = draft["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    return client, supervisor, cid


def to_dwell(client, supervisor, cid):
    """Runs the campaign to the hot dwell."""
    tick(supervisor, client, T0)
    assert "sim_dut 2 25 -0.2 -0.3 0.75" in client.commands
    assert "sim_tc 1 ok" in client.commands
    decide(cid, "confirm")
    tick(supervisor, client, T0 + 1)
    client.temp = client.setpoint = 50.0
    wall = T0 + 1 + 900 / SPEED + 1
    c = tick(supervisor, client, wall)
    assert c["steps"][c["runtime"]["step_index"]]["kind"] == "dwell"
    return wall


def test_the_soak_waits_for_the_stabilisation_sensors(rig):
    client, supervisor, cid = started(rig)
    wall = to_dwell(client, supervisor, cid)
    for minute in range(30):
        client.tcs.update(
            temp1=30 + minute * 0.5,
            temp2=32 + minute * 0.5,
            temp3=35 + minute * 0.4,
            temp4=33 + minute * 0.45,
        )
        wall, c = run_for(supervisor, client, wall, 60)
    assert c["runtime"]["stabilised_at"] is None
    assert c["runtime"]["stab_status"].startswith("Temperature 1 44.")
    assert "Temperature 4" in c["runtime"]["stab_status"] and "°C/h" in c["runtime"]["stab_status"]
    assert cs.step_progress(c)["status"].startswith("stabilising: Temperature 1")
    client.tcs.update(temp1=48.0, temp2=48.2, temp3=48.5, temp4=48.4)
    wall, c = run_for(supervisor, client, wall, 300)
    assert c["runtime"]["stabilised_at"] is None
    wall, c = run_for(supervisor, client, wall, 16 * 60)
    assert c["runtime"]["stabilised_at"] is not None
    stabilised = next(e for e in cs.log_entries(cid) if e["action"] == "Plateau stabilised")
    assert "Temperature 1 48.0 °C" in stabilised["detail"]


def test_the_pressure_rule_holds_a_heating_ramp_and_resumes_it(rig):
    client, supervisor, cid = started(rig, gauge=True)
    client.pump_torr_per_s = lambda s: 760.0 * 10 ** (-s / 1200.0)
    tick(supervisor, client, T0)
    c = tick(supervisor, client, T0 + 40)
    assert c["runtime"]["pump_confirmed"] and client.jobs[-1][0]["temp"] == 50.0
    client.pump_torr_per_s = None
    client.temp = 35.0
    client.pressure = 1.2e-4
    c = tick(supervisor, client, T0 + 41)
    held = c["runtime"]["pressure_hold"]
    assert held["kind"] == "ramp" and held["temp_c"] == 35.0
    assert client.jobs[-1][-1]["temp"] == 35.0
    entry = next(e for e in cs.log_entries(cid) if e["action"] == "Ramp held (pressure rule)")
    assert "1.2×10⁻⁴ Torr, above the procedure's 7.5×10⁻⁵ Torr" in entry["detail"]
    assert cs.step_progress(c)["status"] == "held at +35 °C (pressure)"
    assert c["deviations"][-1]["decision"] == "held by the approved pressure rule"
    client.pressure = 5e-5
    c = tick(supervisor, client, T0 + 42)
    assert c["runtime"]["pressure_hold"] is not None
    client.pressure = 3e-5
    c = tick(supervisor, client, T0 + 43)
    assert c["runtime"]["pressure_hold"] is None
    assert client.jobs[-1][-1]["temp"] == 50.0
    assert any(e["action"] == "Ramp resumed (pressure rule)" for e in cs.log_entries(cid))

    client.pressure = 1.2e-4
    tick(supervisor, client, T0 + 44)
    c = tick(supervisor, client, T0 + 44 + 61 * 60 / SPEED)
    hold = cs.open_hold_of(cid)
    assert c["state"] == "holding" and hold["kind"] == "pressure"
    assert hold["recommendation"]["option"] == "wait"
    decide(cid, "resume")
    c = tick(supervisor, client, T0 + 60 + 61 * 60 / SPEED)
    assert c["runtime"]["pressure_hold"] is None and client.jobs[-1][-1]["temp"] == 50.0
    assert c["deviations"][-1]["decision"] == "resume anyway (Ana)"


def test_an_invalid_stabilisation_sensor_needs_a_person(rig):
    client, supervisor, cid = started(rig)
    wall = to_dwell(client, supervisor, cid)
    client.tcs["temp2"] = 1372.0
    wall, c = run_for(supervisor, client, wall, 60)
    assert c["runtime"]["tc"]["temp2"]["status"] == "invalid"
    assert "open circuit" in c["deviations"][-1]["text"]
    hold = cs.open_hold_of(cid)
    assert c["state"] == "holding" and hold["kind"] == "sensor"
    assert hold["title"] == "Temperature 2 is invalid. Exclude it from the stabilisation criterion?"
    assert "the criterion uses Temperature 1, Temperature 3 and Temperature 4" in hold["detail"]
    assert hold["recommendation"]["option"] == "exclude"
    decide(cid, "exclude")
    wall, c = run_for(supervisor, client, wall, 60)
    assert c["state"] == "running" and c["runtime"]["tc"]["temp2"]["status"] == "excluded"
    assert any(e["action"] == "Thermocouple excluded" for e in cs.log_entries(cid))
    assert "excluded from the criterion by Ana" in [d.get("decision") for d in c["deviations"]]
    client.tcs.update(temp1=49.5, temp3=49.6, temp4=49.4)
    wall, c = run_for(supervisor, client, wall, 20 * 60)
    assert c["runtime"]["stabilised_at"] is not None, (
        c["state"],
        c["runtime"].get("stab_status"),
        cs.open_hold_of(cid),
        c["runtime"]["tc"],
    )
    assert "Temperature 2" not in c["runtime"]["stab_status"]


def test_a_frozen_or_spiking_sensor_is_found(rig):
    client, supervisor, cid = started(rig)
    wall = to_dwell(client, supervisor, cid)
    client.frozen.add("temp3")
    for i in range(8):
        client.tcs.update(temp2=40 + i * 0.1, temp3=41.234)
        wall, c = run_for(supervisor, client, wall, 60)
    assert c["runtime"]["tc"]["temp3"]["status"] == "invalid"
    assert "frozen" in c["runtime"]["tc"]["temp3"]["reason"]
    hold = cs.open_hold_of(cid)
    assert hold["kind"] == "sensor" and hold["title"].startswith("Temperature 3 is invalid")
    decide(cid, "recheck")
    wall, c = run_for(supervisor, client, wall, 60)
    assert c["runtime"]["tc"]["temp3"]["status"] == "recheck"
    client.frozen.clear()
    for i in range(4):
        client.tcs.update(temp3=42 + i * 0.2)
        wall, c = run_for(supervisor, client, wall, 60)
    assert c["runtime"]["tc"]["temp3"]["status"] == "ok"
    assert any(e["action"] == "Thermocouple valid again" for e in cs.log_entries(cid))
    for i in range(10):
        client.tcs["temp4"] = 45.0 + (30.0 if i % 2 else 0.0)
        wall, c = run_for(supervisor, client, wall, 30, step_s=30)
    assert "spiking" in c["runtime"]["tc"]["temp4"]["reason"]


def test_the_gateway_refuses_the_rogue_commands(rig):
    from app.views.demo_bridge import DemoBridge

    client, supervisor, cid = started(rig)
    wall = to_dwell(client, supervisor, cid) + 0.1
    host = SimpleNamespace(agent_api=SimpleNamespace(supervisor=supervisor))
    bridge = DemoBridge(host=host, user_name="Ana")
    expected = {
        "worker_vent_cold": "Venting needs the operator's approval",
        "worker_ramp_fast": "faster than the specification's 2",
        "worker_no_vacuum": "can't be skipped",
        "analyst_pid": "Only a person changes PID settings",
        "worker_out_of_range": "+150 °C is not one of the campaign's plateaus",
        "planner_self_approve": "Only a signed-in person decides",
    }
    for kind in expected:
        assert '"ok": true' in bridge.rogue(cid, kind)
    c = tick(supervisor, client, wall)
    blocked = c["blocked"][-len(expected) :]
    for (kind, phrase), entry in zip(expected.items(), blocked, strict=True):
        assert any(phrase in r for r in entry["reasons"]), (kind, entry)
    assert blocked[1]["command"] == "set +50 °C at 10 °C/min"
    assert blocked[5]["actor"] == "Planner"
    assert c["state"] == "running"


def test_an_old_pack_gets_the_new_articles(deepvac_data_dir):
    import json

    import app.common as common

    old = dict(cs.DEFAULT_PACK)
    old.pop("pack_version")
    old["stabilise_timeout_min"] = 60.0
    old["articles"] = [
        {"name": "6U CubeSat, Earth observation demo", "op_min_c": -10, "op_max_c": 45},
        {"name": "Star tracker unit", "op_min_c": -20, "op_max_c": 50},
    ]
    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (common.DATA_DIR / cs.PACK_FILE).write_text(json.dumps(old), encoding="utf-8")
    pack = cs.load_pack()
    assert [a["name"] for a in pack["articles"]][1] == "Reaction Wheel Assembly"
    assert pack["stabilise_timeout_min"] == 180.0
    names = [t["name"] for t in cs.thermocouples_of(pack)]
    assert names == ["Temperature 1", "Temperature 2", "Temperature 3", "Temperature 4"]
    assert all("thermocouples" not in a for a in pack["articles"])
