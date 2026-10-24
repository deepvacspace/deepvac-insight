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


def _streaming_server(events, delay_s=0.15):
    """A stand-in for the crew's chat webhook in "streaming" mode: one JSON event per line."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            import time

            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for event in events:
                line = (json.dumps(event) + "\n").encode()
                self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()
                time.sleep(delay_s)
            self.wfile.write(b"0\r\n\r\n")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _event(kind, node, content=None):
    event = {"type": kind, "metadata": {"nodeName": node, "nodeId": "x"}}
    if content is not None:
        event["content"] = content
    return event


def test_streams_the_answer_and_shows_the_crew_working(chat, qtbot, monkeypatch):
    events = [
        _event("begin", "Professor"),
        _event("end", "Professor"),
        _event("begin", "analyst"),
        _event(
            "item", "analyst", "Run #12 has the lowest tail MAE."
        ),  # for the Professor, not shown
        _event("end", "analyst"),
        _event("begin", "Professor"),
        _event("item", "Professor", "**Run #12** "),
        _event("item", "Professor", "settled best."),
        _event("end", "Professor"),
    ]
    server = _streaming_server(events)
    url = f"http://127.0.0.1:{server.server_port}/webhook/test/chat"
    monkeypatch.setattr(settings_service, "load_agent_chat_url", lambda: url)
    panel = chat.panel
    panel.input.setText("which run settled best?")
    panel.send()
    qtbot.waitUntil(
        lambda: any(r == "steps" and "Analyst: working" in t for r, t in panel.messages()),
        timeout=10_000,
    )
    qtbot.waitUntil(lambda: panel._reply is None, timeout=10_000)
    server.shutdown()

    roles = [role for role, _ in panel.messages()]
    assert roles[-3:] == ["user", "steps", "agent"]  # the steps stay above the answer
    steps = panel.messages()[-2][1]
    assert "✓ Analyst: working" in steps and "Professor" in steps and "working…" not in steps
    answer = panel.messages()[-1][1]
    assert answer == "**Run #12** settled best."  # the specialist's text is not part of it


def test_activity_feed_adds_what_each_agent_does(chat):
    panel = chat.panel
    panel._start_turn()
    panel.show_activity(
        [
            {
                "id": 1,
                "agent": "Analyst",
                "text": "reading the summary of run #12",
                "status": "done",
            },
            {
                "id": 2,
                "agent": "Worker",
                "text": "reading the chamber's live status",
                "status": "running",
            },
        ]
    )
    steps = panel.messages()[-1][1]
    assert "✓ Analyst: reading the summary of run #12" in steps
    assert "… Worker: reading the chamber's live status" in steps
    panel._set_working(False)
    assert "… " not in panel.messages()[-1][1]  # all marked done in the summary


def test_tells_the_crew_what_became_of_its_requests(chat, qtbot, crew_webhook, monkeypatch):
    from app.agent_chat import UPDATE_PREFIX

    url, received = crew_webhook
    monkeypatch.setattr(settings_service, "load_agent_chat_url", lambda: url)
    panel = chat.panel
    # the worker filed request 7 and saw it pending; it saw request 8 already sent
    panel.show_activity(
        [
            {"id": 1, "agent": "Worker", "text": "requesting 30 °C", "status": "done"}
            | {"request": 7, "request_state": "pending"},
            {"id": 2, "agent": "Worker", "text": "waiting", "status": "done"}
            | {"request": 8, "request_state": "executed/starting"},
        ]
    )
    request = {
        "id": 7,
        "kind": "setpoint",
        "status": "executed",
        "summary": "Set PLC to 30 °C, hold 10 min",
        "decided_by": "Ana",
        "job_state": "running",
    }
    known = {**request, "id": 8, "summary": "Set PLC to 25 °C"}
    panel.show_requests([request, known])
    qtbot.waitUntil(lambda: len(received) == 1, timeout=5000)
    update = received[0]["chatInput"]
    assert update.startswith(UPDATE_PREFIX)
    assert (
        "Request 7 (Set PLC to 30 °C, hold 10 min): approved by Ana; the job is running" in update
    )
    assert "Request 8" not in update  # the crew already knows
    qtbot.waitUntil(lambda: panel._reply is None and not panel._wrap_up, timeout=5000)

    panel.show_requests([request, known])  # nothing new: nothing sent
    # the job ends while the crew answers something else: told once that turn is over
    panel.input.setText("and now?")
    panel.send()
    panel.show_requests([{**request, "job_state": "finished"}, known])
    qtbot.waitUntil(lambda: len(received) == 3, timeout=5000)
    assert received[1]["chatInput"] == "and now?"
    assert (
        "Request 7 (Set PLC to 30 °C, hold 10 min): the job finished." in received[2]["chatInput"]
    )
    qtbot.waitUntil(lambda: panel._reply is None, timeout=5000)
    notices = [text for role, text in panel.messages() if role == "notice"]
    assert len(notices) == 2 and notices[1].startswith("Insight · Request 7")
