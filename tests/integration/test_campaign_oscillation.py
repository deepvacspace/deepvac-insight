"""Tests of the oscillation decision point."""

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app.common as common
from app.api.campaign_supervisor import CampaignSupervisor, Context
from app.api.professor_tools import ProfessorTools
from app.services import campaign_service as cs
from app.services import hold_advisor
from tests.integration.test_campaigns import FakeClient, FakeLinks, decide, tick

pytestmark = pytest.mark.integration

PIDS = {"name": "p1", "label": "Temperature PID", "min": -80, "max": 200, "points": 5,
        "p": [5, 5, 10, 10, 10], "i": [600, 82, 600, 600, 600], "d": [6, 20, 6, 6, 6]}  # fmt: skip
OPERATOR_TABLE = {
    f"p1{k}{band}": float(PIDS[k][band]) for k in ("p", "i", "d") for band in range(5)
}
FAULT = {"p": 2.0, "i": 50.0, "d": 1.0}


class PidClient(FakeClient):
    """A chamber with a PID table and a sample history."""

    def __init__(self, report_gains=False):
        super().__init__()
        self.report_gains = report_gains
        self.plc_settings = dict(OPERATOR_TABLE)
        self.sent_settings = dict(OPERATOR_TABLE)
        self.samples = []
        self.written = []

    @property
    def gains(self):
        return {k: self.plc_settings[f"p1{k}2"] for k in ("p", "i", "d")}

    def inject_fault(self):
        """Injects the demo PID fault."""
        self.plc_settings.update({f"p1{k}2": v for k, v in FAULT.items()})

    def snapshot(self):
        snap = super().snapshot()
        if self.report_gains:
            snap.values.update(
                temp_kp=self.gains["p"], temp_ki=self.gains["i"], temp_kd=self.gains["d"]
            )
        snap.values["temp_pid_idx"] = 2.0
        snap.settings = dict(self.sent_settings)
        return snap

    def send_text_cmd(self, command):
        super().send_text_cmd(command)
        if command == "get_settings":
            self.sent_settings = dict(self.plc_settings)

    def history(self, since=None):
        return [s for s in self.samples if since is None or s["timestamp"] > since]

    def send_settings_map(self, values):
        self.written.append(dict(values))
        self.plc_settings.update(values)

    def feed(self, start, seconds, swing):
        """Feeds oscillating samples around the setpoint."""
        for k in range(int(seconds / 2)):
            t = start + 2 * k
            self.temp = self.setpoint + swing * math.sin(2 * math.pi * t / 75.0)
            self.samples.append({"timestamp": t, "temp": self.temp, "temp_ref": self.setpoint})


@pytest.fixture
def rig(deepvac_data_dir):
    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (common.DATA_DIR / "control_settings.json").write_text(
        json.dumps({"plc_settings": {"pids": [PIDS]}}), encoding="utf-8"
    )
    client = PidClient()
    links = FakeLinks(client)
    advisor = lambda cid, hold, facts: hold_advisor.advise(cid, hold, facts, {"model": "rules"})  # noqa: E731
    return client, CampaignSupervisor(links, advisor=advisor), ProfessorTools(links)


def _to_the_hot_dwell(client, supervisor, professor):
    """Runs a one-cycle campaign until the hot plateau stabilises."""
    cid = professor.draft_campaign(
        "CubeSat", 10, 30, "acceptance", cycles=1, dwell_min=30, reason="demo"
    )["id"]
    professor.submit_campaign(cid)
    decide(cid, "approve")
    tick(supervisor, client, 1000)
    decide(cid, "confirm")
    tick(supervisor, client, 1002)
    ramp = client.jobs[-1][0]
    t = 1002 + ramp["duration"] + 1
    tick(supervisor, client, t)
    client.temp = 35.0
    c = tick(supervisor, client, t + 10)
    assert c["runtime"]["stabilised_at"] == t + 10
    return cid, t + 10


def _log(cid):
    return [(e["actor"], e["action"], e["detail"]) for e in cs.log_entries(cid)]


@pytest.mark.parametrize("report_gains", [False, True], ids=["gains-from-settings", "gain-states"])
def test_the_oscillation_holds_and_a_person_reverts_the_pid_table(rig, report_gains):
    client, supervisor, professor = rig
    client.report_gains = report_gains
    cid, t0 = _to_the_hot_dwell(client, supervisor, professor)
    recorded = next(d for a, action, d in _log(cid) if action == "Operator PID table recorded")
    assert "read from the PLC" in recorded
    assert cs.require(cid)["runtime"]["pid_table"]["gains"] == OPERATOR_TABLE

    client.inject_fault()
    tick(supervisor, client, t0 + 5)
    assert client.commands[-1] == "get_settings"
    client.feed(t0 + 6, 600, swing=0.7)
    c = tick(supervisor, client, t0 + 620)
    hold = cs.open_hold_of(cid)
    assert c["state"] == "holding" and hold["kind"] == "oscillation"
    assert "PID band 3 in use: P 2, I 50, D 1" in hold["detail"]
    assert "the operator's table: P 10, I 600, D 6" in hold["detail"]
    assert c["deviations"][-1]["kind"] == "oscillation"
    assert c["runtime"]["hold_paused_at"] == t0 + 620
    rec = hold["recommendation"]
    assert rec["option"] == "revert_pid" and rec["by"] == hold_advisor.RULES_BY
    assert "P 2, I 50, D 1 instead of the operator's P 10, I 600, D 6" in rec["rationale"]

    ctx = Context(cs.require(cid))
    values = dict(OPERATOR_TABLE)
    refused = supervisor.gateway(
        ctx, "Professor", {"name": "pid_table", "values": values, "decided_by": "Ana"}, t0 + 621
    )
    assert "Only a person changes PID settings, at a decision point." in refused
    values["p1p2"] = 1.0
    refused = supervisor.gateway(
        ctx, "Ana", {"name": "pid_table", "values": values, "decided_by": "Ana"}, t0 + 621
    )
    assert "Only the operator's recorded PID table can be written back." in refused

    decide(cid, "revert_pid")
    c = tick(supervisor, client, t0 + 630)
    assert c["state"] == "running" and client.written == [OPERATOR_TABLE]
    assert client.commands[-1] == "get_settings"
    assert client.gains == {"p": 10.0, "i": 600.0, "d": 6.0}
    actor, _, detail = next(e for e in _log(cid) if e[1] == "PID table restored")
    assert actor == "Ana" and "band 3: P 2 → 10, I 50 → 600, D 1 → 6" in detail
    assert c["deviations"][-1]["decision"] == "PID table restored by Ana"
    assert cs.verify_chain(cid)["ok"]

    client.feed(t0 + 631, 700, swing=0.05)
    c = tick(supervisor, client, t0 + 1340)
    assert cs.open_hold_of(cid) is None and c["state"] == "running"


def test_keeping_the_gains_logs_the_oscillation_and_writes_nothing(rig):
    client, supervisor, professor = rig
    cid, t0 = _to_the_hot_dwell(client, supervisor, professor)
    client.inject_fault()
    tick(supervisor, client, t0 + 5)
    client.feed(t0 + 6, 600, swing=0.7)
    tick(supervisor, client, t0 + 620)
    decide(cid, "accept")
    c = tick(supervisor, client, t0 + 630)
    assert c["runtime"]["oscillation_accepted"] and not client.written
    assert c["deviations"][-1]["decision"] == "accepted, logged as a deviation"
    client.feed(t0 + 631, 700, swing=0.7)
    tick(supervisor, client, t0 + 1340)
    assert cs.open_hold_of(cid) is None


def test_the_rules_recommend_at_each_hold_point():
    gains = {
        "gains_in_use": {"p": 2, "i": 50, "d": 1},
        "operator_gains": {"p": 10, "i": 600, "d": 6},
    }
    osc = {"band_c": 3.0, "peak_to_peak_c": 1.4, "crossings": 16, "pid_band": 3}
    assert hold_advisor.rule("oscillation", {**osc, **gains})[0] == "revert_pid"
    same = {"gains_in_use": gains["operator_gains"], "operator_gains": gains["operator_gains"]}
    assert hold_advisor.rule("oscillation", {**osc, **same})[0] == "accept"
    out = {"band_c": 3.0, "target_c": 35.0}
    assert hold_advisor.rule("anomaly", {**out, "temp_c": 39.5})[0] == "extend_dwell"
    assert hold_advisor.rule("anomaly", {**out, "temp_c": 50.0})[0] == "abort"
    assert (
        hold_advisor.rule("stabilise", {**out, "temp_c": 30.0, "temp_5min_ago_c": 25.0})[0]
        == "wait"
    )
    assert (
        hold_advisor.rule("stabilise", {**out, "temp_c": 30.0, "temp_5min_ago_c": 30.0})[0]
        == "start_dwell"
    )
    assert (
        hold_advisor.rule("stabilise", {**out, "temp_c": 20.0, "temp_5min_ago_c": 20.0})[0]
        == "abort"
    )
    assert hold_advisor.rule("interrupted", {"status": "alarm"})[0] == "ambient"
    assert hold_advisor.rule("interrupted", {"status": "idle", "connected": True})[0] == "resume"
    assert hold_advisor.rule("vent", {"temp_c": 21.0, "vent_band_c": [10, 40]})[0] == "approve"
    assert hold_advisor.rule("vent", {"temp_c": 5.0, "vent_band_c": [10, 40]}) is None
    assert hold_advisor.rule("approval", {}) is None and hold_advisor.rule("pump", {}) is None


def test_the_answer_must_name_an_offered_option():
    keys = ["revert_pid", "accept", "abort"]
    assert hold_advisor.parse_answer(
        '```json\n{"option": "accept", "rationale": "ok"}\n```', keys
    ) == (
        "accept",
        "ok",
    )
    assert hold_advisor.parse_answer('{"option": "retune", "rationale": "x"}', keys) is None
    assert hold_advisor.parse_answer('{"option": "abort", "rationale": ""}', keys) is None
    assert hold_advisor.parse_answer("no json", keys) is None


class _FakeModel(BaseHTTPRequestHandler):
    """Fake OpenAI-compatible chat completions server."""

    content = ""
    prompts = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeModel.prompts.append(body["messages"][0]["content"])
        reply = json.dumps({"choices": [{"message": {"content": _FakeModel.content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_model():
    server = HTTPServer(("127.0.0.1", 0), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeModel.prompts = []
    yield {"model": "local", "url": f"http://127.0.0.1:{server.server_port}/v1", "timeout_s": 5}
    server.shutdown()


def _oscillation_hold(rig):
    client, supervisor, professor = rig
    cid, t0 = _to_the_hot_dwell(client, supervisor, professor)
    client.inject_fault()
    tick(supervisor, client, t0 + 5)
    client.feed(t0 + 6, 600, swing=0.7)
    supervisor.advisor = lambda *a: None
    tick(supervisor, client, t0 + 620)
    hold = cs.open_hold_of(cid)
    facts = {"band_c": 3.0, "peak_to_peak_c": 1.4, "crossings": 16, "pid_band": 3,
             "gains_in_use": FAULT, "operator_gains": {"p": 10, "i": 600, "d": 6}}  # fmt: skip
    return cid, hold, facts


def test_the_local_model_replaces_the_rules_recommendation(rig, local_model):
    cid, hold, facts = _oscillation_hold(rig)
    _FakeModel.content = json.dumps(
        {"option": "revert_pid", "rationale": "Band 3 runs P 2 instead of P 10: revert."}
    )
    hold_advisor.advise(cid, hold, facts, local_model, background=False)
    rec = cs.open_hold_of(cid)["recommendation"]
    assert rec["by"] == hold_advisor.LOCAL_BY and rec["rationale"].startswith("Band 3 runs P 2")
    assert '"gains_in_use"' in _FakeModel.prompts[0] and "revert_pid" in _FakeModel.prompts[0]
    actions = [e["action"] for e in cs.log_entries(cid)]
    assert actions.count("Recommendation") == 2


def test_a_bad_answer_or_the_professor_keep_their_recommendation(rig, local_model):
    cid, hold, facts = _oscillation_hold(rig)
    _FakeModel.content = '{"option": "retune the PID", "rationale": "x"}'
    hold_advisor.advise(cid, hold, facts, local_model, background=False)
    assert cs.open_hold_of(cid)["recommendation"]["by"] == hold_advisor.RULES_BY

    cs.recommend(cid, "accept", "The swing is small.", by="Professor")
    _FakeModel.content = '{"option": "revert_pid", "rationale": "revert"}'
    hold_advisor._refine(cid, hold, facts, None, local_model)
    assert cs.open_hold_of(cid)["recommendation"]["by"] == "Professor"


def test_without_a_server_the_rules_recommendation_stays(rig):
    cid, hold, facts = _oscillation_hold(rig)
    config = {"model": "local", "url": "http://127.0.0.1:9/v1", "timeout_s": 2}
    hold_advisor.advise(cid, hold, facts, config, background=False)
    assert cs.open_hold_of(cid)["recommendation"]["by"] == hold_advisor.RULES_BY


def test_the_advisor_follows_the_ai_mode(deepvac_data_dir, monkeypatch):
    from app.services import ai_mode

    monkeypatch.delenv("DEEPVAC_HOLD_ADVISOR", raising=False)
    assert ai_mode.mode() == "online" and hold_advisor.load_config()["model"] == "rules"
    ai_mode.set_mode("offline")
    config = hold_advisor.load_config()
    assert config["model"] == "local" and config["url"] == "http://127.0.0.1:8089/v1"
    monkeypatch.setenv("DEEPVAC_HOLD_ADVISOR", "rules")
    assert hold_advisor.load_config()["model"] == "rules"


def test_the_hold_point_waits_for_the_settings_asked_after_the_oscillation(rig):
    client, supervisor, professor = rig
    original = client.snapshot

    def snapshot():
        snap = original()
        snap.settings_at = client.settings_received
        return snap

    client.settings_received = 0.5
    client.snapshot = snapshot
    cid, t0 = _to_the_hot_dwell(client, supervisor, professor)
    client.inject_fault()
    client.sent_settings = dict(OPERATOR_TABLE)
    client.feed(t0 + 6, 600, swing=0.7)
    real_send = client.send_text_cmd

    def send(command):
        if command != "get_settings":
            real_send(command)
        else:
            client.commands.append(command)

    client.send_text_cmd = send
    tick(supervisor, client, t0 + 620)
    assert cs.open_hold_of(cid) is None
    assert client.commands[-1] == "get_settings"
    client.sent_settings = dict(client.plc_settings)
    client.settings_received = t0 + 621
    tick(supervisor, client, t0 + 621)
    hold = cs.open_hold_of(cid)
    assert hold["kind"] == "oscillation" and "in use: P 2, I 50, D 1" in hold["detail"]
    assert hold["recommendation"]["option"] == "revert_pid"
