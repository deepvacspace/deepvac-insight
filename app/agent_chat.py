"""Chat with the DeepVac crew (the n8n agent orchestration, ../agents).

A round button floats at the bottom right of the main window; it opens a
chat panel above it. Messages go to the crew workflow's chat webhook
(n8n Chat Trigger, public in "webhook" mode): POST
{"action": "sendMessage", "sessionId", "chatInput"}. The session id keeps
the Professor's memory of this conversation; "New chat" starts a fresh one.

While the crew works the panel shows its progress:
- the answer streams in as the Professor writes it: the Chat node in
  "streaming" mode answers with one JSON object per line
  ({"type": "begin" | "item" | "end" | "error", "content", "metadata":
  {"nodeName"}}), and the begin / end of each agent become progress lines;
  a non-streaming {"output": text} answer is accepted too;
- what each agent is doing, from the agent API's activity feed
  (GET /activity, app/api/activity.py): "Analyst: comparing runs #12, #13".
When the answer is complete the steps stay above it as a short summary.

Chamber actions the crew asks for still need approving in Agent Requests:
the chat only talks. It follows the requests its crew made (the activity
feed names them) through GET /requests: when one is decided, sent, or its
job ends, a notice appears here and, unless the crew already saw that state
through its own tools, the chat sends the crew an update in the same session
(UPDATE_PREFIX), so the Professor can tell the user what came of it.

Chart images the analyst exported (export_chart) and the answer links to are
shown under it as thumbnails; a click opens the file.
"""

import json
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.api.activity import request_state
from app.common import _svg_icon
from app.services import settings_service

REPLY_TIMEOUT_MS = 5 * 60 * 1000  # the crew can take a while: the Professor waits for both agents
ACTIVITY_POLL_MS = 1000
REQUEST_POLL_MS = 3000
UPDATE_PREFIX = "[Update from DeepVac Insight, not typed by the user]"
CLOSED = ("rejected", "failed", "expired")  # request statuses after which nothing happens
MARGIN = 26  # from the window corner (clear of the size grip)
PANEL_SIZE = QSize(400, 560)

ANSWER_NODE = (
    "Professor"  # the orchestrator: its text is the answer; the specialists' text is for it
)
AGENT_LABELS = {"Professor": "Professor", "analyst": "Analyst", "worker": "Worker"}
MARKS = {"running": "…", "done": "✓", "error": "✗"}
# local image files an answer links to: file:///C:/... or C:\...
IMAGE_LINK = re.compile(
    r"(file:///[^\s)\]>\"']+?\.(?:png|jpe?g|bmp)|[A-Za-z]:[\\/][^\n)\]>\"']+?\.(?:png|jpe?g|bmp))",
    re.IGNORECASE,
)
MAX_THUMBNAILS = 4


class ImageThumbnail(QLabel):
    """A chart image in the chat; a click opens the file."""

    def __init__(self, path: Path, width: int, parent=None):
        super().__init__(parent)
        self.path = path
        self.setObjectName("chatImage")
        self.setPixmap(QPixmap(str(path)).scaledToWidth(width, Qt.SmoothTransformation))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(str(path))

    def mousePressEvent(self, event):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))
        super().mousePressEvent(event)


class AgentChatPanel(QFrame):
    def __init__(self, api_source=None, parent=None):
        """api_source: callable returning (agent API base URL, bearer token), or None, for
        the activity feed; without it the progress shows the agents only."""
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setFixedSize(PANEL_SIZE)
        self._api_source = api_source
        self._network = QNetworkAccessManager(self)
        self._reply = None
        self._buffer = b""
        self._raw = b""
        self._streamed = False
        self._answer_label = None
        self._answer_text = ""
        self._progress = OrderedDict()  # key -> {"text", "status"}
        self._progress_label = None
        self._activity_reply = None
        self._activity_since = 0.0
        self._session_id = str(uuid.uuid4())
        self._started = 0.0
        self._working = False
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._wrap_up = False  # the turn ended: one last look at the activity, then updates
        self._final_activity = None
        self._requests = {}  # request id -> {"request", "seen", "told"}
        self._requests_reply = None
        self._requests_timer = QTimer(self)
        self._requests_timer.timeout.connect(self._poll_requests)
        self._build_ui()
        self._add_message(
            "agent",
            self.tr(
                "Hi! I'm the Professor of the DeepVac crew. Ask me about past experiments, "
                "the chamber now, or what to try next. Chamber changes always wait for your "
                "approval in Agent Requests."
            ),
        )

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("chatHeader")
        head = QHBoxLayout(header)
        head.setContentsMargins(14, 10, 8, 10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(self.tr("DeepVac Crew"))
        title.setObjectName("chatTitle")
        self.subtitle = QLabel(self.tr("Professor · Worker · Analyst"))
        self.subtitle.setObjectName("chatSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        new_btn = QPushButton(self.tr("New chat"))
        new_btn.setObjectName("chatHeaderButton")
        new_btn.setToolTip(self.tr("Start a new conversation (the crew forgets this one)"))
        new_btn.clicked.connect(self.new_chat)
        head.addWidget(new_btn)
        close_btn = QPushButton()
        close_btn.setObjectName("chatHeaderButton")
        close_btn.setIcon(_svg_icon("x-lg", "#ffffff", 14))
        close_btn.setToolTip(self.tr("Close"))
        close_btn.clicked.connect(self.hide)
        head.addWidget(close_btn)
        root.addWidget(header)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("chatScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("chatBody")
        self._messages = QVBoxLayout(body)
        self._messages.setContentsMargins(12, 12, 12, 12)
        self._messages.setSpacing(8)
        self._messages.addStretch(1)
        self._scroll.setWidget(body)
        root.addWidget(self._scroll, 1)

        footer = QFrame()
        footer.setObjectName("chatFooter")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(10, 10, 10, 10)
        self.input = QLineEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText(self.tr("Ask the crew…"))
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton(self.tr("Send"))
        self.send_btn.setObjectName("primaryButton")
        self.send_btn.clicked.connect(self.send)
        foot.addWidget(self.input, 1)
        foot.addWidget(self.send_btn)
        root.addWidget(footer)

    # ── conversation ─────────────────────────────────────────────────────────

    def new_chat(self):
        if self._reply is not None:
            self._reply.abort()
        self._session_id = str(uuid.uuid4())
        self._requests = {}  # the new conversation's crew doesn't know them
        self._requests_timer.stop()
        while self._messages.count() > 1:  # keep the stretch
            item = self._messages.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        self._add_message("agent", self.tr("New conversation. What shall we look at?"))

    def send(self):
        text = self.input.text().strip()
        if not text or self._reply is not None:
            return
        self.input.clear()
        self._add_message("user", text)
        self._post(text)

    def _post(self, text):
        url = settings_service.load_agent_chat_url()
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        request.setTransferTimeout(REPLY_TIMEOUT_MS)
        body = {"action": "sendMessage", "sessionId": self._session_id, "chatInput": text}
        self._start_turn()
        self._reply = self._network.post(request, json.dumps(body).encode("utf-8"))
        self._reply.readyRead.connect(self._on_ready_read)
        self._reply.finished.connect(self._on_reply)

    def _start_turn(self):
        self._buffer = b""
        self._raw = b""
        self._streamed = False
        self._answer_label = None
        self._answer_text = ""
        self._progress = OrderedDict()
        self._activity_since = time.time() - 1.0
        self._set_working(True)

    def _on_ready_read(self):
        if self._reply is not None:
            self._consume(self._reply)

    def _consume(self, reply):
        """Reads what arrived; streamed answers are handled line by line as they come."""
        data = bytes(reply.readAll())
        if not data:
            return
        self._raw += data
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle_line(line)

    def _handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if isinstance(event, dict) and event.get("type") in ("begin", "item", "end", "error"):
            self._streamed = True
            self.handle_event(event)

    def handle_event(self, event):
        """One streamed event from the crew's chat webhook (separate for tests)."""
        kind = event.get("type")
        node = (event.get("metadata") or {}).get("nodeName") or ""
        label = AGENT_LABELS.get(node, node or self.tr("Crew"))
        key = f"node:{node}"
        if kind == "begin":
            # an agent begins once per model call: its line keeps the place it first got
            if key in self._progress:
                self._progress[key]["status"] = "running"
            else:
                doing = self.tr("thinking") if node == ANSWER_NODE else self.tr("working")
                self._progress[key] = {"text": f"{label}: {doing}", "status": "running"}
        elif kind == "end":
            if key in self._progress:
                self._progress[key]["status"] = "done"
        elif kind == "item":
            content = event.get("content")
            if node in (ANSWER_NODE, "") and isinstance(content, str) and content:
                if not self._answer_text and key in self._progress:
                    self._progress[key]["text"] = f"{label}: " + self.tr("writing the answer")
                self._answer_text += content
                if self._answer_label is None:
                    self._answer_label = self._add_message("agent", self._answer_text)
                else:
                    self._answer_label.setText(self._answer_text)
                    QTimer.singleShot(0, self._scroll_to_end)
        elif kind == "error":
            self._add_message(
                "error", str(event.get("content") or self.tr("The crew reported an error."))
            )
        self._render_progress()

    def _on_reply(self):
        reply = self._reply
        self._reply = None
        if reply is None:
            return
        reply.deleteLater()
        self._consume(reply)
        if self._buffer.strip():
            self._handle_line(self._buffer)
            self._buffer = b""
        cancelled = reply.error() == QNetworkReply.OperationCanceledError
        ok = reply.error() == QNetworkReply.NoError
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if not cancelled:
            if self._streamed and ok:
                if not self._answer_text:
                    self._add_message("agent", self.tr("(The crew sent an empty answer.)"))
                self.show_images(self._answer_text)
            else:
                raw = self._raw.decode("utf-8", errors="replace")
                self.show_reply(ok, status, raw, reply.errorString())
        self._set_working(False)

    def show_reply(self, ok, status, raw, error_text=""):
        """Turns a non-streamed answer or an error into a message (separate for tests)."""
        if ok:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {"output": raw}
            if isinstance(data, list) and data:
                data = data[0]
            text = data.get("output") if isinstance(data, dict) else None
            self._add_message("agent", text or self.tr("(The crew sent an empty answer.)"))
            self.show_images(text or "")
            return
        if status == 404:
            hint = self.tr(
                "The crew's chat isn't available. Check that n8n is running and the DeepVac Crew "
                "workflow is published (agents/README.md)."
            )
        elif status is None:
            hint = self.tr("Can't reach n8n at {0}: {1}").format(
                settings_service.load_agent_chat_url(), error_text
            )
        else:
            hint = self.tr("The crew answered with an error ({0}): {1}").format(
                status, raw[:300] or error_text
            )
        self._add_message("error", hint)

    # ── progress ─────────────────────────────────────────────────────────────

    def _set_working(self, working):
        self._working = working
        self.send_btn.setEnabled(not working)
        if working:
            self._started = time.monotonic()
            self._progress_label = self._add_message("steps", self.tr("The crew is working…"))
            self._tick_timer.start(ACTIVITY_POLL_MS)
            return
        self._tick_timer.stop()
        self._render_progress()
        self._wrap_up = True
        if self._activity_reply is None:
            self._poll_activity()  # its answer (or none) closes the summary, then _maybe_send_update

    def _close_summary(self):
        """The steps stay above the answer as a summary, once the last activity is in."""
        if self._progress_label is None:
            return
        if self._progress:
            for entry in self._progress.values():
                if entry["status"] == "running":
                    entry["status"] = "done"
            self._render_progress()
        else:
            self._progress_label.parentWidget().deleteLater()
        self._progress_label = None

    def _tick(self):
        self._render_progress()
        self._poll_activity()

    def _render_progress(self):
        if self._progress_label is None:
            return
        lines = []
        if self._working:
            seconds = int(time.monotonic() - self._started)
            lines.append(self.tr("The crew is working… {0} s").format(seconds))
        else:
            lines.append(self.tr("Steps"))
        for entry in self._progress.values():
            lines.append(f"{MARKS.get(entry['status'], '·')} {entry['text']}")
        self._progress_label.setText("\n".join(lines))

    def _api_get(self, path):
        """GET on the agent API, or None when there is none."""
        if self._api_source is None:
            return None
        try:
            source = self._api_source()
        except Exception:
            source = None
        if not source:
            return None
        base, token = source
        request = QNetworkRequest(QUrl(f"{base}{path}"))
        request.setRawHeader(b"Authorization", f"Bearer {token}".encode())
        request.setTransferTimeout(3000)
        return self._network.get(request)

    def _poll_activity(self):
        if self._activity_reply is not None:
            return
        self._activity_reply = self._api_get(f"/activity?since={self._activity_since:.3f}")
        if self._activity_reply is None:
            self._finish_wrap_up()
            return
        if self._wrap_up:
            self._final_activity = self._activity_reply
        self._activity_reply.finished.connect(self._on_activity)

    def _on_activity(self):
        reply = self._activity_reply
        self._activity_reply = None
        if reply is None:
            return
        reply.deleteLater()
        events = None
        if reply.error() == QNetworkReply.NoError:  # else no agent API: the agents only
            try:
                events = json.loads(bytes(reply.readAll()).decode("utf-8")).get("events", [])
            except ValueError:
                events = None
        if events:
            self.show_activity(events)
        if self._wrap_up:
            if reply is self._final_activity:
                self._finish_wrap_up()
            else:
                self._poll_activity()  # asked before the turn ended: look once more

    def _finish_wrap_up(self):
        if self._wrap_up:
            self._wrap_up = False
            self._final_activity = None
            self._close_summary()
            self._maybe_send_update()

    def show_activity(self, events):
        """Agent API activity events (app/api/activity.py) as progress lines (separate for tests)."""
        for event in events:
            key = f"tool:{event['id']}"
            self._progress[key] = {
                "text": f"{event.get('agent', '')}: {event.get('text', '')}",
                "status": event.get("status", "running"),
            }
            if event.get("request") is not None and event.get("status") == "done":
                self._track(event["request"], event.get("request_state"))
        self._render_progress()

    # ── the crew's chamber requests ──────────────────────────────────────────

    def _track(self, request_id, told):
        """A request the crew made or checked; told: the state its tool reported."""
        entry = self._requests.setdefault(
            int(request_id), {"request": None, "seen": None, "told": None}
        )
        entry["told"] = _same_news(told)
        entry["seen"] = entry["seen"] or entry["told"]
        if not self._requests_timer.isActive():
            self._requests_timer.start(REQUEST_POLL_MS)

    def _open_ids(self):
        return [
            rid
            for rid, entry in self._requests.items()
            if entry["request"] is None or _is_open(entry["request"])
        ]

    def _poll_requests(self):
        ids = self._open_ids()
        if not ids:
            self._requests_timer.stop()
            return
        if self._requests_reply is not None:
            return
        self._requests_reply = self._api_get("/requests?ids=" + ",".join(map(str, ids)))
        if self._requests_reply is not None:
            self._requests_reply.finished.connect(self._on_requests)

    def _on_requests(self):
        reply = self._requests_reply
        self._requests_reply = None
        if reply is None:
            return
        reply.deleteLater()
        if reply.error() != QNetworkReply.NoError:
            return
        try:
            requests = json.loads(bytes(reply.readAll()).decode("utf-8")).get("requests", [])
        except ValueError:
            return
        self.show_requests(requests)

    def show_requests(self, requests):
        """Current state of followed requests (GET /requests): notices for what changed, then an
        update to the crew if it doesn't know yet (separate for tests)."""
        for request in requests:
            entry = self._requests.get(int(request["id"]))
            if entry is None:
                continue
            entry["request"] = request
            state = _same_news(request_state(request))
            if state != entry["seen"]:
                entry["seen"] = state
                news = describe_request(request)
                if news:
                    self._add_message("notice", "Insight · " + news)
        if not self._open_ids():
            self._requests_timer.stop()
        self._maybe_send_update()

    def _maybe_send_update(self):
        """Tells the crew, as a message in this conversation, what it doesn't know yet."""
        if self._working or self._reply is not None or self._wrap_up:
            return  # after the turn
        news = []
        for entry in self._requests.values():
            request = entry["request"]
            if request is None or entry["seen"] == entry["told"]:
                continue
            text = describe_request(request)
            if text:
                news.append(text)
                entry["told"] = entry["seen"]
        if news:
            lines = [UPDATE_PREFIX, *[f"- {text}" for text in news]]
            lines.append("Tell the user briefly what this means for what they asked.")
            self._post("\n".join(lines))

    def show_images(self, text):
        """Thumbnails of the local images an answer links to (separate for tests)."""
        shown = []
        for match in IMAGE_LINK.findall(text or ""):
            link = match.strip()
            path = (
                Path(QUrl(link).toLocalFile()) if link.lower().startswith("file:") else Path(link)
            )
            if path in shown or not path.is_file():
                continue
            shown.append(path)
            thumbnail = ImageThumbnail(path, int(PANEL_SIZE.width() * 0.82))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(thumbnail)
            row.addStretch(1)
            holder = QWidget()
            holder.setLayout(row)
            self._messages.addWidget(holder)
            if len(shown) == MAX_THUMBNAILS:
                break
        if shown:
            QTimer.singleShot(0, self._scroll_to_end)
        return shown

    # ── messages ─────────────────────────────────────────────────────────────

    def _add_message(self, role, text):
        bubble = QLabel(text)
        bubble.setObjectName(
            {
                "user": "chatUserMsg",
                "agent": "chatAgentMsg",
                "steps": "chatStepsMsg",
                "notice": "chatNoticeMsg",
            }.get(role, "chatErrorMsg")
        )
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        bubble.setOpenExternalLinks(True)
        bubble.setTextFormat(Qt.MarkdownText if role == "agent" else Qt.PlainText)
        bubble.setMaximumWidth(int(PANEL_SIZE.width() * 0.82))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        self._messages.addWidget(holder)
        QTimer.singleShot(0, self._scroll_to_end)
        return bubble

    def _scroll_to_end(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def messages(self):
        """(role, text) of the conversation shown, oldest first (for tests)."""
        roles = {
            "chatUserMsg": "user",
            "chatAgentMsg": "agent",
            "chatStepsMsg": "steps",
            "chatNoticeMsg": "notice",
        }
        result = []
        for i in range(1, self._messages.count()):
            holder = self._messages.itemAt(i).widget()
            label = holder.findChild(QLabel) if holder else None
            if isinstance(label, ImageThumbnail):
                result.append(("image", str(label.path)))
            elif label:
                result.append((roles.get(label.objectName(), "error"), label.text()))
        return result


def _same_news(state):
    """A job that is starting and one that is running are the same news for the crew."""
    return state.replace("/starting", "/running") if state else state


def _is_open(request):
    """Can anything still happen to this request?"""
    if request.get("status") in CLOSED:
        return False
    if request.get("status") == "executed":
        return request.get("job_state") in ("starting", "running")
    return True


def describe_request(request):
    """What became of a request, in a sentence (None while nothing worth telling happened)."""
    status, job = request.get("status"), request.get("job_state")
    head = f"Request {request['id']} ({request.get('summary', '')})"
    by = request.get("decided_by") or "the operator"
    if status == "rejected":
        comment = request.get("decision_comment")
        return f"{head}: rejected by {by}" + (f" ({comment})." if comment else ".")
    if status == "failed":
        return f"{head}: approved, but sending it to the chamber failed: {request.get('result')}."
    if status == "expired":
        return f"{head}: expired; nobody decided on it within 30 minutes."
    if status != "executed" or job == "starting":
        return None
    texts = {
        None: f"approved by {by} and sent to the chamber.",
        "running": f"approved by {by}; the job is running on the chamber.",
        "finished": "the job finished.",
        "stopped": "the job was stopped before it finished.",
        "alarm": "the job ended in an alarm.",
        "replaced": "another job replaced it on the chamber.",
        "unknown": "sent to the chamber, but the PLC never reported the job running.",
    }
    return f"{head}: {texts.get(job, job)}"


class AgentChat(QObject):
    """The floating chat button and its panel, kept at the bottom right of `host`."""

    def __init__(self, host: QWidget, api_source=None):
        super().__init__(host)
        self.host = host
        self.button = QPushButton(host)
        self.button.setObjectName("chatBubble")
        self.button.setFixedSize(56, 56)
        self.button.setIcon(_svg_icon("chat-dots", "#ffffff", 26))
        self.button.setIconSize(QSize(26, 26))
        self.button.setToolTip(self.tr("Chat with the DeepVac crew"))
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(self.toggle)
        self.panel = AgentChatPanel(api_source, host)
        self.panel.hide()
        host.installEventFilter(self)
        self.reposition()
        self.button.show()

    def toggle(self):
        self.panel.setVisible(not self.panel.isVisible())
        self.reposition()
        if self.panel.isVisible():
            self.panel.input.setFocus()

    def reposition(self):
        w, h = self.host.width(), self.host.height()
        bx = w - self.button.width() - MARGIN
        by = h - self.button.height() - MARGIN
        self.button.move(bx, by)
        self.panel.move(
            max(8, w - PANEL_SIZE.width() - MARGIN), max(8, by - PANEL_SIZE.height() - 12)
        )
        self.panel.raise_()
        self.button.raise_()

    def eventFilter(self, watched, event):
        if watched is self.host and event.type() in (QEvent.Resize, QEvent.Show):
            QTimer.singleShot(0, self.reposition)
        return False

    def configure_url(self, parent):
        url, ok = QInputDialog.getText(
            parent,
            self.tr("Agent Chat"),
            self.tr("Chat webhook of the DeepVac Crew workflow (n8n):"),
            text=settings_service.load_agent_chat_url(),
        )
        if ok and url.strip():
            settings_service.save_agent_chat_url(url)
