"""ObserverDialog: lists Observer events, acknowledges them and edits the
Observer settings."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services import observer_service as obs

PUSH_TEXT = {
    obs.PUSH_OFF: "—",
    obs.PUSH_PENDING: "waiting",
    obs.PUSH_SENT: "sent",
    obs.PUSH_FAILED: "failed",
}


class ObserverDialog(QDialog):
    REFRESH_MS = 3000
    COLUMNS = 9

    def __init__(self, current_user=None, parent=None):
        super().__init__(parent)
        self.current_user = current_user or {"name": "Unknown"}
        self.setWindowTitle(self.tr("Observer Events"))
        self.setMinimumSize(1000, 560)
        self._build_ui()
        self._load_settings()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(self.REFRESH_MS)

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            self.tr(
                "Events Insight detected on the chambers while the Agent API runs. The Observer "
                "workflow reads each one, writes a summary and notifies people."
            )
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._table = QTableWidget()
        self._table.setColumnCount(self.COLUMNS)
        self._table.setHorizontalHeaderLabels(
            [
                self.tr("#"),
                self.tr("Time"),
                self.tr("Chamber"),
                self.tr("Severity"),
                self.tr("Event"),
                self.tr("Observer's summary"),
                self.tr("Sent to n8n"),
                self.tr("Acknowledged"),
                "",
            ]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        root.addWidget(self._table, 1)

        filters = QHBoxLayout()
        self._open_only = QCheckBox(self.tr("Not acknowledged only"))
        self._open_only.toggled.connect(self._refresh)
        self._show_info = QCheckBox(self.tr("Show information events"))
        self._show_info.toggled.connect(self._refresh)
        filters.addWidget(self._open_only)
        filters.addWidget(self._show_info)
        filters.addStretch(1)
        root.addLayout(filters)

        box = QGroupBox(self.tr("Settings"))
        form = QFormLayout(box)
        self._watch = QCheckBox(self.tr("Watch the chambers and record events"))
        self._push = QCheckBox(self.tr("Send events to the Observer workflow (n8n)"))
        self._url = QLineEdit()
        self._severity = QComboBox()
        for severity in obs.SEVERITIES:
            self._severity.addItem(severity, severity)
        self._drift_c = self._spin(0.5, 50.0, 1, " °C")
        self._drift_min = self._spin(0.5, 240.0, 1, " min")
        self._settled_min = self._spin(0.0, 240.0, 1, " min")
        self._waiting_min = self._spin(0.0, 240.0, 1, " min")
        form.addRow(self._watch)
        form.addRow(self._push)
        form.addRow(self.tr("Webhook URL"), self._url)
        form.addRow(self.tr("Send events from"), self._severity)
        drift = QWidget()
        drift_row = QHBoxLayout(drift)
        drift_row.setContentsMargins(0, 0, 0, 0)
        drift_row.addWidget(self._drift_c)
        drift_row.addWidget(QLabel(self.tr("for")))
        drift_row.addWidget(self._drift_min)
        drift_row.addWidget(QLabel(self.tr("once the setpoint is steady for")))
        drift_row.addWidget(self._settled_min)
        drift_row.addStretch(1)
        form.addRow(self.tr("Drift"), drift)
        form.addRow(self.tr("Report waiting requests after"), self._waiting_min)
        root.addWidget(box)

        buttons = QHBoxLayout()
        save = QPushButton(self.tr("Save Settings"))
        save.clicked.connect(self._save_settings)
        test = QPushButton(self.tr("Send Test Event"))
        test.clicked.connect(self._send_test)
        close = QPushButton(self.tr("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(save)
        buttons.addWidget(test)
        buttons.addStretch(1)
        buttons.addWidget(close)
        root.addLayout(buttons)

    @staticmethod
    def _spin(low, high, decimals, suffix):
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        return spin

    def _load_settings(self):
        config = obs.load_config()
        self._watch.setChecked(bool(config["watch"]))
        self._push.setChecked(bool(config["push"]))
        self._url.setText(config["webhook_url"])
        self._severity.setCurrentIndex(max(0, self._severity.findData(config["min_severity"])))
        self._drift_c.setValue(float(config["drift_c"]))
        self._drift_min.setValue(float(config["drift_min"]))
        self._settled_min.setValue(float(config["settled_min"]))
        self._waiting_min.setValue(float(config["waiting_min"]))

    def _save_settings(self):
        obs.save_config(
            {
                "watch": self._watch.isChecked(),
                "push": self._push.isChecked(),
                "webhook_url": self._url.text().strip() or obs.DEFAULT_WEBHOOK_URL,
                "min_severity": self._severity.currentData(),
                "drift_c": self._drift_c.value(),
                "drift_min": self._drift_min.value(),
                "settled_min": self._settled_min.value(),
                "waiting_min": self._waiting_min.value(),
            }
        )
        self._load_settings()

    def _send_test(self):
        """Records a test event to be pushed to the Observer workflow."""
        config = obs.load_config()
        event = obs.record(
            obs.TEST,
            obs.WARNING,
            self.tr("Test event from DeepVac Insight"),
            {"sent_by": self.current_user.get("name") or "Unknown"},
            push=bool(config["push"]),
        )
        if not config["push"]:
            QMessageBox.information(
                self,
                self.tr("Observer Events"),
                self.tr(
                    "Test event {0} recorded. Sending to n8n is off: switch on “Send events to the "
                    "Observer workflow” and save to send events."
                ).format(event["id"]),
            )
        self._refresh()

    def _refresh(self):
        events = obs.list_events(
            200,
            obs.INFO if self._show_info.isChecked() else obs.WARNING,
            unacknowledged=self._open_only.isChecked(),
        )
        self._table.setRowCount(len(events))
        for row, event in enumerate(events):
            summary = event["summary"] or ""
            if event["notify"]:
                summary += self.tr("  → notified: {0}").format(event["message"])
            ack = (
                f"{event['acknowledged_by']} {event['acknowledged_at'][11:16]}"
                if event["acknowledged_at"]
                else ""
            )
            push = PUSH_TEXT.get(event["push_state"], event["push_state"])
            if event["push_error"] and event["push_state"] != obs.PUSH_SENT:
                push += f" ({event['push_error']})"
            values = [
                event["id"],
                event["created_at"][:19].replace("T", " "),
                event["chamber_name"],
                event["severity"],
                event["title"],
                summary,
                push,
                ack,
            ]
            for col, value in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem(str(value)))
            if event["acknowledged_at"] is None:
                button = QPushButton(self.tr("Acknowledge"))
                button.clicked.connect(lambda _=False, e=event: self._acknowledge(e))
                self._table.setCellWidget(row, self.COLUMNS - 1, button)
            else:
                self._table.removeCellWidget(row, self.COLUMNS - 1)
        self._table.resizeColumnsToContents()

    def _acknowledge(self, event):
        obs.acknowledge(event["id"], self.current_user.get("name") or "Unknown")
        self._refresh()
