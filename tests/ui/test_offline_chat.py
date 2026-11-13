"""Tests of the offline chat."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.agent_chat import AgentChatPanel
from app.services import ai_mode

pytestmark = pytest.mark.ui


class _Model(BaseHTTPRequestHandler):
    bodies = []
    content = "La cámara está a +55 °C, en la meseta caliente."

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Model.bodies.append(body)
        data = json.dumps({"choices": [{"message": {"content": _Model.content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def offline(deepvac_ui):
    server = HTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Model.bodies = []
    ai_mode.save(mode="offline", local_url=f"http://127.0.0.1:{server.server_port}/v1")
    yield server
    server.shutdown()


def test_the_local_model_answers_from_insights_context(offline, qtbot):
    panel = AgentChatPanel(context_source=lambda: "Chamber Simulator: work, 55.02 °C.")
    qtbot.addWidget(panel)
    panel.refresh_mode()
    assert panel.subtitle.text() == "Offline: local model on this PC"
    panel.input.setText("¿Cómo va la cámara?")
    panel.send()
    qtbot.waitUntil(
        lambda: panel.messages()[-1][0] == "agent" and "+55 °C" in panel.messages()[-1][1],
        timeout=10000,
    )
    body = _Model.bodies[-1]
    assert "Chamber Simulator: work, 55.02 °C." in body["messages"][0]["content"]
    assert body["messages"][-1] == {"role": "user", "content": "¿Cómo va la cámara?"}

    _Model.content = "Sí."
    panel.input.setText("¿Y la presión?")
    panel.send()
    qtbot.waitUntil(lambda: panel.messages()[-1] == ("agent", "Sí."), timeout=10000)
    sent = [m["content"] for m in _Model.bodies[-1]["messages"][1:]]
    assert sent == [
        "¿Cómo va la cámara?",
        "La cámara está a +55 °C, en la meseta caliente.",
        "¿Y la presión?",
    ]


def test_without_the_local_model_the_chat_says_how_to_start_it(deepvac_ui, qtbot):
    ai_mode.save(mode="offline", local_url="http://127.0.0.1:9/v1")
    panel = AgentChatPanel()
    qtbot.addWidget(panel)
    panel.input.setText("hola")
    panel.send()
    qtbot.waitUntil(lambda: panel.messages()[-1][0] == "error", timeout=15000)
    assert "AI Mode" in panel.messages()[-1][1]


def test_offline_campaign_news_are_notices_only(offline, qtbot):
    panel = AgentChatPanel()
    qtbot.addWidget(panel)
    panel.follow_campaign(7)
    panel.show_campaigns(
        [
            {
                "id": 7,
                "title": "CubeSat",
                "state": "holding",
                "news": "holding|3|",
                "hold_point": {
                    "kind": "oscillation",
                    "title": "The chamber oscillates.",
                    "detail": "",
                    "options": [{"key": "revert_pid", "label": "Revert"}],
                },
            }
        ]  # fmt: skip
    )
    assert panel.messages()[-1][0] == "notice" and "oscillates" in panel.messages()[-1][1]
    assert panel._reply is None and not _Model.bodies
