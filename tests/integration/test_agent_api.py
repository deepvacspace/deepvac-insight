"""Agent API (app/api): analyst tools over the run database, worker tools and
the request executor against the fake PLC (scripts/fake_plc.py), and the
bearer token on the HTTP app."""

import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.api import server
from app.api.analyst_tools import AnalystTools
from app.api.chambers import ChamberError, ChamberLinks
from app.api.executor import RequestExecutor
from app.api.runs import RunCatalogue, RunReferenceError
from app.api.worker_tools import WorkerTools
from app.services import agent_requests_service, chambers_service, data_service

pytestmark = pytest.mark.integration


def _insert_run(key, name, start_epoch, setpoint, source="import"):
    rows = []
    temp = 20.0
    for i in range(300):
        temp += (setpoint - temp) * 0.05
        rows.append(
            {"timestamp": start_epoch + 2 * i, "temp": temp, "temp_ref": setpoint, "temp_u": 40.0}
        )
    conn = data_service.connect_cache()
    try:
        conn.execute(
            "INSERT INTO runs (key, id, group_name, root_path, run_path, samples_path, source_mtime, samples, "
            "duration_s, mae, start_time, end_time, columns_json, numeric_columns_json, summary_json, bands_json, "
            "annotations_json, samples_json, cached_at, source, start_epoch) "
            "VALUES (?, ?, 'tests', '', '', '', 0, ?, 598, 1.0, ?, '', '[]', '[]', '{}', '[]', '[]', ?, datetime('now'), ?, ?)",
            (
                key,
                name,
                len(rows),
                data_service.format_ts(start_epoch),
                json.dumps(rows),
                source,
                start_epoch,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def analyst(deepvac_data_dir):
    _insert_run("control/older", "older run", 1_780_000_000, 40.0)
    _insert_run("control/newer", "newer run", 1_780_100_000, 30.0)
    return AnalystTools(RunCatalogue())


def test_runs_are_numbered_oldest_first(analyst):
    listed = analyst.list_runs(newest_first=False)
    assert [r["name"] for r in listed["runs"]] == ["older run", "newer run"]
    assert listed["runs"][0]["run"] == "#1"
    assert analyst.catalogue.resolve("latest").name == "newer run"
    assert analyst.catalogue.resolve("previous").name == "older run"
    assert analyst.catalogue.resolve("#2").name == "newer run"
    with pytest.raises(RunReferenceError, match="numbered #1 to #2"):
        analyst.catalogue.resolve("#9")


def test_summary_and_comparison_come_from_the_analysis(analyst):
    summary = analyst.get_run_summary("latest")
    assert summary["header"]["run"] == "#2"
    assert summary["configuration"]["setpoints_c"] == [30.0]
    assert summary["kpis"]["final_error"] < 0.1
    comparison = analyst.compare_runs(["#1", "#2"])
    assert comparison["reference"] == "#1"
    assert any(row["kpi"] == "MAE" for row in comparison["kpis"])
    stats = analyst.get_statistics("#1", ["temp", "nope"], 0, 100)
    assert stats["statistics"]["temp"]["n"] == 51 and stats["unknown_channels"] == ["nope"]


def test_folder_copies_of_imported_runs_are_listed_once(deepvac_data_dir):
    _insert_run(
        "control/2026_run_1780000000_abcdef12", "run_1780000000_abcdef12", 1_780_000_000, 40.0
    )
    _insert_run(
        "run_history/run_1780000000_abcdef12",
        "run_1780000000_abcdef12",
        1_780_000_000,
        40.0,
        source="folder",
    )
    entries = RunCatalogue().entries()
    assert [e.key for e in entries] == ["control/2026_run_1780000000_abcdef12"]


def _load_fake_plc():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_plc.py"
    spec = importlib.util.spec_from_file_location("fake_plc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_plc():
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
    yield chamber, port
    server_socket.close()


@pytest.fixture
def worker(deepvac_data_dir, fake_plc):
    _, port = fake_plc
    chambers_service.add_chamber("Fake PLC", "127.0.0.1", port, protocol="plc")
    links = ChamberLinks(connect_timeout_s=5)
    yield WorkerTools(links), links
    links.close()


def test_requests_reach_the_plc_only_after_approval(worker, fake_plc):
    tools, links = worker
    chamber, _ = fake_plc
    status = tools.get_live_status("Fake PLC")
    assert status["connected"] and status["status"] == "idle"

    with pytest.raises(ChamberError, match="outside the chamber's setpoint range"):
        tools.request_setpoint(500, "too hot", "Fake PLC")
    request = tools.request_setpoint(30, "soak test", "Fake PLC", hold_minutes=10)
    assert request["status"] == "pending"

    executor = RequestExecutor(links)
    assert executor.run_once() == 0  # not approved: nothing is sent
    assert chamber.status == 1

    agent_requests_service.decide(request["id"], True, "Operator")
    assert executor.run_once() == 1
    assert tools.get_request(request["id"])["status"] == "executed"
    deadline = time.monotonic() + 3  # the fake PLC applies the setpoint on its next tick
    while (chamber.status != 2 or chamber.ref != 30.0) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert chamber.status == 2 and chamber.ref == 30.0


def test_json_chambers_are_not_controllable(deepvac_data_dir):
    tools = WorkerTools(ChamberLinks())
    with pytest.raises(ChamberError, match="monitoring feed"):
        tools.get_live_status("Chamber 1")  # the seeded newline-JSON chamber


def test_http_app_needs_the_token(deepvac_data_dir):
    app = server.build_app("secret", AnalystTools(), WorkerTools(ChamberLinks()))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/analyst/mcp", json={}).status_code == 401
        assert (
            client.post(
                "/worker/mcp", json={}, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
