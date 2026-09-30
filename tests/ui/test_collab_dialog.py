"""Smoke tests for CollabDialog (app/collab_dialog.py): hosting on/off and
the resulting join code and connection state."""

import pytest

from app.collab_dialog import CollabDialog
from app.services import collab_client, settings_service
from app.services.collab_server import CollabServer

pytestmark = pytest.mark.ui


@pytest.fixture
def server(deepvac_ui):
    settings_service.save_collab_port(0)
    instance = CollabServer()
    yield instance
    instance.stop()


def test_dialog_opens_not_hosting_and_not_connected(qtbot, server):
    dlg = CollabDialog(server, {"name": "Ada"})
    qtbot.addWidget(dlg)

    assert dlg._host_btn.text() == "Start hosting"
    assert dlg._connection_lbl.text() == "Not connected."
    assert not dlg._sync_btn.isEnabled()


def test_start_and_stop_hosting_updates_state(qtbot, server):
    dlg = CollabDialog(server, {"name": "Ada"})
    qtbot.addWidget(dlg)

    dlg._toggle_hosting()

    assert server.is_running()
    assert dlg._host_btn.text() == "Stop hosting"
    assert len(dlg._code_lbl.text()) == 9
    assert collab_client.connection()["url"].startswith("https://127.0.0.1:")
    assert "Certificate fingerprint" in dlg._host_status_lbl.text()
    assert dlg._sync_btn.isEnabled()

    dlg._toggle_hosting()

    assert not server.is_running()
    assert collab_client.connection() is None
    assert dlg._code_lbl.text() == ""
