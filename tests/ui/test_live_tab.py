"""Analysis Live tab (app/live_tab.py) against the fake PLC (scripts/fake_plc.py):
it follows the running job, analyses it, and saves it as a run when it ends."""

import importlib.util
import socket
import threading
from pathlib import Path

import pytest

from app.api.chambers import ChamberLinks
from app.live_tab import LiveTabPage
from app.services import chambers_service, data_service
from app.services import plc_gate as pg

pytestmark = pytest.mark.ui

CONTROL_PARAMS = [
    {"name": "duration", "type": 256},
    {"name": "temp", "type": 0},
    {"name": "flags", "type": 257},
]


def _load_fake_plc():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_plc.py"
    spec = importlib.util.spec_from_file_location("fake_plc_live", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plc_chamber(deepvac_data_dir):
    fake = _load_fake_plc()
    chamber = fake.Chamber(24.0)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)

    def accept():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(
                target=fake.serve_client, args=(conn, chamber, 0.05), daemon=True
            ).start()

    threading.Thread(target=accept, daemon=True).start()
    chambers_service.add_chamber("Fake PLC", "127.0.0.1", server.getsockname()[1], protocol="plc")
    links = ChamberLinks(connect_timeout_s=5)
    yield chamber, links
    links.close()
    server.close()


def test_idle_without_a_chamber(deepvac_data_dir, qapp, qtbot, qsettings_isolated):
    links = ChamberLinks()
    page = LiveTabPage(links)
    qtbot.addWidget(page)
    assert "No chamber to follow" in page.idle_label.text()
    assert not page.save_btn.isEnabled()
    links.close()


def test_follows_a_job_and_saves_it_when_it_ends(plc_chamber, qapp, qtbot, qsettings_isolated):
    chamber, links = plc_chamber
    page = LiveTabPage(links)
    qtbot.addWidget(page)
    client = links.client(chamber_by_name("Fake PLC"))
    qtbot.waitUntil(lambda: "IDLE" in page.status_label.text(), timeout=8000)

    client.send_job(
        pg.PROG_TYPE_SETPOINT, pg.manual_setpoint_intervals(30.0, 24.0, 0b011), CONTROL_PARAMS
    )
    qtbot.waitUntil(lambda: "Job running" in page.status_label.text(), timeout=8000)
    qtbot.waitUntil(
        lambda: len(client.history(since=client.snapshot().job_started_at)) > 20, timeout=8000
    )
    page._start_analysis()
    qtbot.waitUntil(lambda: page._analysis is not None, timeout=8000)
    assert page.kpi_values["stage"].text() != "—"
    assert page.now_values["setpoint"].text().startswith("30")
    assert page.save_btn.isEnabled()

    before = len(data_service.load_cached_runs())
    saved = []
    page.run_saved.connect(saved.append)
    client.send_text_cmd("stop")  # the job ends: saved as a run by itself
    qtbot.waitUntil(lambda: bool(saved), timeout=8000)
    runs = data_service.load_cached_runs()
    assert len(runs) == before + 1
    assert any(r["key"] == saved[0] and r["chamber"] == "Fake PLC" for r in runs)


def chamber_by_name(name):
    return next(c for c in chambers_service.list_chambers() if c["name"] == name)
