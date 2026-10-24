"""Chat with the DeepVac crew (the n8n agent orchestration, ../agents).

A round button floats at the bottom right of the main window; it opens a
chat panel above it. Messages go to the crew workflow's chat webhook
(n8n Chat Trigger, public in "webhook" mode): POST
{"action": "sendMessage", "sessionId", "chatInput"} -> {"output": text}.
The session id keeps the Professor's memory of this conversation; "New
chat" starts a fresh one.

Chamber actions the crew asks for still need approving in Agent Requests:
the chat only talks.
"""

import json
import time
import uuid

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl
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

from app.common import _svg_icon
from app.services import settings_service

REPLY_TIMEOUT_MS = 5 * 60 * 1000  # the crew can take a while: the Professor waits for both agents
MARGIN = 26  # from the window corner (clear of the size grip)
PANEL_SIZE = QSize(400, 560)


class AgentChatPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setFixedSize(PANEL_SIZE)
        self._network = QNetworkAccessManager(self)
        self._reply = None
        self._session_id = str(uuid.uuid4())
        self._started = 0.0
        self._working_label = None
        self._working_timer = QTimer(self)
        self._working_timer.timeout.connect(self._tick)
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
        url = settings_service.load_agent_chat_url()
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        request.setTransferTimeout(REPLY_TIMEOUT_MS)
        body = {"action": "sendMessage", "sessionId": self._session_id, "chatInput": text}
        self._reply = self._network.post(request, json.dumps(body).encode("utf-8"))
        self._reply.finished.connect(self._on_reply)
        self._set_working(True)

    def _on_reply(self):
        reply = self._reply
        self._reply = None
        self._set_working(False)
        if reply is None:
            return
        reply.deleteLater()
        if reply.error() == QNetworkReply.OperationCanceledError:
            return
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
        self.show_reply(reply.error() == QNetworkReply.NoError, status, raw, reply.errorString())

    def show_reply(self, ok, status, raw, error_text=""):
        """Turns the webhook's answer into a message (separate for tests)."""
        if ok:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {"output": raw}
            if isinstance(data, list) and data:
                data = data[0]
            text = data.get("output") if isinstance(data, dict) else None
            self._add_message("agent", text or self.tr("(The crew sent an empty answer.)"))
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

    # ── messages ─────────────────────────────────────────────────────────────

    def _add_message(self, role, text):
        bubble = QLabel(text)
        bubble.setObjectName(
            {"user": "chatUserMsg", "agent": "chatAgentMsg"}.get(role, "chatErrorMsg")
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

    def _set_working(self, working):
        self.send_btn.setEnabled(not working)
        if working:
            self._started = time.monotonic()
            self._working_label = self._add_message("agent", self.tr("The crew is working…"))
            self._working_timer.start(1000)
        else:
            self._working_timer.stop()
            if self._working_label is not None:
                self._working_label.parentWidget().deleteLater()
                self._working_label = None

    def _tick(self):
        if self._working_label is not None:
            seconds = int(time.monotonic() - self._started)
            self._working_label.setText(self.tr("The crew is working… {0} s").format(seconds))

    def messages(self):
        """(role, text) of the conversation shown, oldest first (for tests)."""
        result = []
        for i in range(1, self._messages.count()):
            holder = self._messages.itemAt(i).widget()
            label = holder.findChild(QLabel) if holder else None
            if label:
                role = {"chatUserMsg": "user", "chatAgentMsg": "agent"}.get(
                    label.objectName(), "error"
                )
                result.append((role, label.text()))
        return result


class AgentChat(QObject):
    """The floating chat button and its panel, kept at the bottom right of `host`."""

    def __init__(self, host: QWidget):
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
        self.panel = AgentChatPanel(host)
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
