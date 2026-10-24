"""Chat bubble with the agent crew (app/agent_chat.py): the panel opens from the
button and talks to the crew's chat webhook (here a stand-in HTTP server)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PySide6.QtWidgets import QWidget

from app.agent_chat import AgentChat
from app.services import settings_service

pytestmark = pytest.mark.ui


@pytest.fixture
def crew_webhook():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            received.append(body)
            reply = json.dumps({"output": f"**Run #12** looks best ({body['chatInput']})"}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/webhook/test/chat", received
    server.shutdown()


@pytest.fixture
def chat(qapp, qtbot, qsettings_isolated):
    host = QWidget()
    host.resize(1200, 800)
    qtbot.addWidget(host)
    host.show()
    return AgentChat(host)


def test_button_opens_the_panel_in_the_corner(chat):
    assert not chat.panel.isVisible()
    chat.button.click()
    assert chat.panel.isVisible()
    host = chat.host
    assert (
        chat.button.geometry().right() < host.width()
        and chat.button.geometry().bottom() < host.height()
    )
    assert chat.panel.geometry().bottom() < chat.button.geometry().top()
    chat.button.click()
    assert not chat.panel.isVisible()


def test_sends_to_the_crew_and_shows_the_answer(chat, qtbot, crew_webhook, monkeypatch):
    url, received = crew_webhook
    monkeypatch.setattr(settings_service, "load_agent_chat_url", lambda: url)
    chat.toggle()
    chat.panel.input.setText("compare the last runs")
    chat.panel.send()
    qtbot.waitUntil(lambda: chat.panel._reply is None, timeout=10_000)
    assert (
        received[0]["action"] == "sendMessage"
        and received[0]["chatInput"] == "compare the last runs"
    )
    roles = [role for role, _ in chat.panel.messages()]
    assert roles[-2:] == ["user", "agent"]
    assert "Run #12" in chat.panel.messages()[-1][1]

    session = received[0]["sessionId"]
    chat.panel.input.setText("and the one before?")
    chat.panel.send()
    qtbot.waitUntil(lambda: len(received) == 2, timeout=10_000)
    assert received[1]["sessionId"] == session  # same conversation: the crew remembers it
    qtbot.waitUntil(lambda: chat.panel._reply is None, timeout=10_000)
    chat.panel.new_chat()
    assert chat.panel._session_id != session


def test_explains_when_the_crew_is_not_there(chat):
    chat.panel.show_reply(False, 404, "", "Not Found")
    role, text = chat.panel.messages()[-1]
    assert role == "error" and "published" in text
