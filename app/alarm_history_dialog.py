"""AlarmHistoryDialog — view/acknowledge past alarm events and export the
log to CSV (app/services/alarms_service.py). Pass chamber_id to scope to
one chamber's history (used by a chamber tab's "History…" button);
omit it for the Dashboard's unfiltered, all-chambers view."""

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.common import fmt
from app.services import alarms_service


class AlarmHistoryDialog(QDialog):
    def __init__(self, current_user=None, chamber_id=None, parent=None):
        super().__init__(parent)
        self.current_user = current_user or {"name": "Unknown"}
        self.chamber_id = chamber_id
        self.setWindowTitle(self.tr("Alarm History"))
        self.setMinimumSize(760, 420)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)

        self._table = QTableWidget()
        self._table.setColumnCount(9)
        self._table.setHorizontalHeaderLabels(
            [
                self.tr("Rule"),
                self.tr("Chamber"),
                self.tr("Severity"),
                self.tr("Value"),
                self.tr("Triggered"),
                self.tr("Cleared"),
                self.tr("Acknowledged"),
                self.tr("Comment"),
                "",
            ]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        root.addWidget(self._table, 1)

        button_row = QHBoxLayout()
        export_btn = QPushButton(self.tr("Export CSV…"))
        export_btn.clicked.connect(self._export)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(export_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

    def _refresh(self):
        self._events = alarms_service.list_events(chamber_id=self.chamber_id)
        self._table.setRowCount(len(self._events))
        for row_idx, event in enumerate(self._events):
            values = [
                event["rule_name"],
                event.get("chamber_name") or "—",
                event["severity"],
                fmt(event["trigger_value"]),
                event["triggered_at"] or "",
                event["cleared_at"] or self.tr("still active"),
                event["acknowledged_at"] or self.tr("not acknowledged"),
                event["comment"] or "",
            ]
            for col, value in enumerate(values):
                self._table.setItem(row_idx, col, QTableWidgetItem(str(value)))
            if event["acknowledged_at"]:
                ack_btn = QPushButton(self.tr("Acknowledged"))
                ack_btn.setEnabled(False)
            else:
                ack_btn = QPushButton(self.tr("Acknowledge"))
                ack_btn.clicked.connect(lambda _=False, e=event: self._acknowledge(e))
            self._table.setCellWidget(row_idx, 8, ack_btn)
        self._table.resizeColumnsToContents()

    def _acknowledge(self, event):
        comment, ok = QInputDialog.getText(
            self, self.tr("Acknowledge Alarm"), self.tr("Comment (optional):")
        )
        if not ok:
            return
        alarms_service.acknowledge_event(
            event["id"], self.current_user.get("name") or "Unknown", comment
        )
        self._refresh()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Export alarm log"), "alarm_log.csv", self.tr("CSV (*.csv)")
        )
        if not path:
            return
        try:
            alarms_service.export_events_csv(path, self._events)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Export failed"), str(exc))
            return
        QMessageBox.information(
            self, self.tr("Export complete"), self.tr("Alarm log saved to {0}").format(path)
        )
