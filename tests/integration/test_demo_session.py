"""Tests of the demo session lifecycle: closing and restarting Insight end the demo's campaigns."""

import json

import pytest

from app.api import control_settings
from app.services import campaign_service as cs
from app.services import chambers_service, demo_launcher
from app.views.demo_bridge import DemoBridge
from tests.integration.test_demo_bridge import _draft, j, rig  # noqa: F401

pytestmark = pytest.mark.integration


def _real_chamber_campaign():
    chambers_service.add_chamber("Lab chamber", "10.0.0.5", 4321, protocol="plc")
    chamber = next(c for c in chambers_service.list_chambers() if c["name"] == "Lab chamber")
    limits = control_settings.temperature_limits(control_settings.load())
    requirement = {"article": "6U CubeSat", "op_min_c": -10, "op_max_c": 45, "level": "acceptance"}
    return cs.draft(requirement, chamber, "Ana", limits)["id"]


def test_restarting_ends_only_the_demo_campaigns(rig):  # noqa: F811
    bridge, client, supervisor, chamber = rig
    demo = _draft(bridge, chamber)
    assert j(bridge.submit(demo))["ok"]
    assert cs.open_hold_of(demo)["kind"] == "approval"
    real = _real_chamber_campaign()

    assert demo_launcher.end_demo_campaigns("Insight was restarted") == 1
    ended = cs.require(demo)
    assert ended["state"] == cs.CANCELLED and ended["end_reason"] == "Insight was restarted"
    assert cs.open_hold_of(demo) is None
    assert cs.log_entries(demo)[-1]["action"] == "Campaign cancelled"
    assert cs.require(real)["state"] == cs.DRAFT
    assert demo_launcher.end_demo_campaigns("again") == 0


def test_a_running_demo_campaign_is_aborted(rig):  # noqa: F811
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber)
    bridge.submit(cid)
    assert j(bridge.decide(cs.open_hold_of(cid)["id"], "approve"))["ok"]
    supervisor.run_once(client.now)
    assert cs.require(cid)["state"] in (cs.RUNNING, cs.HOLDING)
    demo_launcher.end_demo_campaigns("Insight was closed")
    assert cs.require(cid)["state"] == cs.ABORTED
    supervisor.run_once(client.now + 1)
    assert cs.require(cid)["state"] == cs.ABORTED


def test_the_page_starts_fresh_after_a_restart(rig):  # noqa: F811
    bridge, client, supervisor, chamber = rig
    cid = _draft(bridge, chamber)
    demo_launcher.end_demo_campaigns("Insight was restarted")
    assert j(bridge.state(chamber["id"], 0))["campaign"]["id"] == cid
    restarted = DemoBridge(host=bridge._host)
    assert j(restarted.state(chamber["id"], 0))["campaign"] is None
    second = _draft(restarted, chamber)
    assert j(restarted.state(chamber["id"], 0))["campaign"]["id"] == second


def test_closing_stops_only_a_demo_that_ran(monkeypatch, deepvac_data_dir):
    calls = []
    monkeypatch.setattr(demo_launcher, "simulator_pid_on", lambda port: calls.append(port) or 4242)
    monkeypatch.setattr(demo_launcher, "end_demo_campaigns", lambda reason: calls.append(reason))
    monkeypatch.setattr(
        demo_launcher.subprocess, "run", lambda args, **kw: calls.append(" ".join(args))
    )
    launcher = demo_launcher.DemoLauncher(host=None)
    launcher.stop()
    assert calls == []

    launcher.chamber_id = 1
    launcher.stop()
    assert calls == ["Insight was closed", 4321, "taskkill /PID 4242 /F"]
    assert launcher.chamber_id is None


def test_the_simulator_is_reset_when_the_demo_connects(rig):  # noqa: F811
    bridge, client, supervisor, chamber = rig
    client.connected = True

    class Host:
        plc_links = bridge.links()

    launcher = demo_launcher.DemoLauncher(Host())
    launcher.chamber_id = chamber["id"]
    original = client.snapshot

    def snapshot():
        snap = original()
        snap.settings = {"x": 1.0}
        return snap

    client.snapshot = snapshot
    launcher._wait_connected({"speed": 300})
    assert client.commands[:2] == ["sim_reset", "sim_speed 300"]
    assert json.dumps(launcher.steps["connected"]).count("ok") == 1
