"""Tests of the demo launcher."""

import json
import socket
import time

import pytest

import app.common as common
from app.api.chambers import ChamberLinks
from app.services import ai_mode, chambers_service, demo_launcher

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(demo_launcher.simulator_exe() is None, reason="no chambersim.exe"),
]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Host:
    def __init__(self):
        self.plc_links = ChamberLinks()
        self.agent_api = None
        self.local_model = type("Model", (), {"status": "ready"})()
        self.modes = []

    def _toggle_agent_api(self, on, quiet=False):
        self.agent_api = type("Api", (), {"supervisor": object()})()

    def set_ai_mode(self, mode):
        ai_mode.set_mode(mode)
        self.modes.append(mode)


@pytest.fixture
def launcher(deepvac_data_dir):
    settings = demo_launcher.chamber_settings()
    if settings is None:
        pytest.skip("no chamber configuration for the simulator")
    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (common.DATA_DIR / "control_settings.json").write_bytes(settings.read_bytes())
    port = _free_port()
    (common.DATA_DIR / "demo.json").write_text(
        json.dumps({"port": port, "n8n_url": f"http://127.0.0.1:{_free_port()}/healthz"}),
        encoding="utf-8",
    )
    host = Host()
    launcher = demo_launcher.DemoLauncher(host)
    yield launcher, host, port
    launcher.stop()
    host.plc_links.close()


def _wait(launcher, timeout=40):
    deadline = time.monotonic() + timeout
    while launcher.starting and time.monotonic() < deadline:
        time.sleep(0.2)


def test_one_step_starts_the_whole_demo(launcher):
    launcher, host, port = launcher
    launcher.start("online")
    _wait(launcher)
    states = {s["key"]: (s["state"], s["detail"]) for s in launcher.status()["steps"]}
    assert launcher.running and launcher.process is not None
    assert states["simulator"] == ("ok", f"port {port}")
    assert states["chamber"] == ("ok", "Deepvac Chamber added")
    assert states["agent_api"][0] == "ok" and host.agent_api is not None
    assert states["connected"][0] == "ok"
    assert states["ai"][0] == "warn" and "n8n doesn't answer" in states["ai"][1]
    assert states["awake"][0] == "ok"
    assert host.modes == ["online"] and ai_mode.mode() == "online"
    chamber = next(c for c in chambers_service.list_chambers() if c["id"] == launcher.chamber_id)
    assert (chamber["protocol"], int(chamber["port"])) == ("plc", port)

    launcher.running = False
    launcher.start("offline")
    _wait(launcher)
    states = {s["key"]: (s["state"], s["detail"]) for s in launcher.status()["steps"]}
    assert "already running" in states["simulator"][1] and states["ai"] == (
        "ok",
        "offline: local model on this PC",
    )
    assert len([c for c in chambers_service.list_chambers() if int(c["port"]) == port]) == 1

    launcher.stop()
    time.sleep(0.5)
    assert not demo_launcher.port_open(port) and not launcher.running
