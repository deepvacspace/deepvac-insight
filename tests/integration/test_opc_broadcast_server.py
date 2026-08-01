"""Integration test for the OPC UA server (app/services/opc_broadcast_server.py)
-- exercised against a real asyncua Client over loopback, not a mock, so
what's asserted is a genuine OPC UA read of a published variable node.
Each chamber gets its own sub-object node under ChamberVariables, keyed by
the chamber_label passed to broadcast()."""

import socket
import sys

import pytest
from asyncua import ua
from asyncua.sync import Client

from app.services.opc_broadcast_server import OpcBroadcastServer

pytestmark = pytest.mark.integration


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def opc_server(qapp):
    server = OpcBroadcastServer()
    yield server
    if server.is_running():
        server.stop()


def test_start_broadcast_stop_roundtrip(qtbot, opc_server):
    port = _free_port()
    started = []
    opc_server.started.connect(started.append)

    assert opc_server.start(port, namespace="urn:test:opc") is True
    qtbot.waitUntil(lambda: started == [port], timeout=5000)
    assert opc_server.is_running() is True

    opc_server.set_update_rate(50)
    opc_server.broadcast("Chamber 1", {"temp": 42.5, "temp_ref": 45.0})
    opc_server.broadcast("Chamber 2", {"temp": 10.0, "temp_ref": 15.0})

    client = Client(url=f"opc.tcp://127.0.0.1:{port}/deepvac/insight/")
    client.connect()
    try:
        idx = client.get_namespace_index("urn:test:opc")
        objects = client.get_objects_node()

        def _chamber_node(label):
            # ChamberVariables/<label> (and each variable under it) is only
            # added once the first sample for that chamber arrives, so
            # get_child() legitimately errors (BadNoMatch) until then --
            # that's the condition being waited on, not a real failure.
            try:
                return objects.get_child([f"{idx}:ChamberVariables", f"{idx}:{label}"])
            except ua.UaStatusCodeError:
                return None

        qtbot.waitUntil(lambda: _chamber_node("Chamber 1") is not None, timeout=5000)
        chamber1 = _chamber_node("Chamber 1")
        qtbot.waitUntil(lambda: _chamber_node("Chamber 2") is not None, timeout=5000)
        chamber2 = _chamber_node("Chamber 2")

        def _temp_value(chamber):
            try:
                return chamber.get_child([f"{idx}:temp"]).get_value()
            except ua.UaStatusCodeError:
                return None

        qtbot.waitUntil(lambda: _temp_value(chamber1) == 42.5, timeout=5000)
        assert chamber1.get_child([f"{idx}:temp_ref"]).get_value() == 45.0

        # The second chamber gets its own, independent set of nodes -- not
        # overwriting the first chamber's values.
        qtbot.waitUntil(lambda: _temp_value(chamber2) == 10.0, timeout=5000)
        assert chamber2.get_child([f"{idx}:temp_ref"]).get_value() == 15.0

        # A later sample updates the same chamber's nodes in place rather
        # than creating new ones.
        opc_server.broadcast("Chamber 1", {"temp": 50.0, "temp_ref": 45.0})
        qtbot.waitUntil(lambda: _temp_value(chamber1) == 50.0, timeout=5000)
        assert _temp_value(chamber2) == 10.0
    finally:
        client.disconnect()

    stopped = []
    opc_server.stopped.connect(lambda: stopped.append(True))
    opc_server.stop()
    qtbot.waitUntil(lambda: opc_server.is_running() is False, timeout=5000)
    assert len(stopped) == 1


def test_start_reports_error_when_port_already_bound(qtbot, opc_server):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        # Windows lets a new listener silently steal a port from an active
        # listener that didn't request this (a well-known asyncio/Windows
        # footgun -- SO_REUSEADDR is enabled by asyncio's server by
        # default), so a plain bind()+listen() here wouldn't reliably
        # conflict. SO_EXCLUSIVEADDRUSE opts this socket out of that.
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    # 0.0.0.0, not 127.0.0.1: the server always binds the wildcard address
    # (see opc_broadcast_server.py's set_endpoint call), and on Windows a
    # specific-address listener with SO_EXCLUSIVEADDRUSE does not actually
    # conflict with a later wildcard bind on the same port -- confirmed by
    # hand; only matching the wildcard address reproduces a real conflict.
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        errors = []
        opc_server.server_error.connect(errors.append)

        assert opc_server.start(port) is False
        qtbot.waitUntil(lambda: len(errors) == 1, timeout=5000)
        assert opc_server.is_running() is False
    finally:
        blocker.close()
