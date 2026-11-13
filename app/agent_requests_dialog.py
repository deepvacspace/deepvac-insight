"""Dialog to approve or reject the chamber actions requested by agents."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services import agent_requests_service


class AgentRequestsDialog(QDialog):
    REFRESH_MS = 3000

    def __init__(self, current_user=None, parent=None):
        super().__init__(parent)
        self.current_user = current_user or {"name": "Unknown"}
        self.setWindowTitle(self.tr("Agent Requests"))
        self.setMinimumSize(900, 440)
        self._build_ui()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(self.REFRESH_MS)

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            self.tr(
                "Chamber actions requested by the AI agents. Nothing is sent to a chamber "
                "until you approve it. Requests not decided within 30 minutes expire."
            )
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._table = QTableWidget()
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels(
            [
                self.tr("#"),
                self.tr("Requested"),
                self.tr("Action"),
                self.tr("Reason"),
                self.tr("By"),
                self.tr("Status"),
                self.tr("Decision / result"),
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
        self._pending_only = QCheckBox(self.tr("Pending only"))
        self._pending_only.toggled.connect(self._refresh)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._pending_only)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

    def _refresh(self):
        agent_requests_service.expire_stale()
        status = agent_requests_service.PENDING if self._pending_only.isChecked() else None
        requests = agent_requests_service.list_requests(status=status, limit=100)
        requests.sort(key=lambda r: (r["status"] != agent_requests_service.PENDING, -r["id"]))
        self._table.setRowCount(len(requests))
        for row_idx, request in enumerate(requests):
            if request["decided_by"]:
                outcome = self.tr("{0} by {1}").format(request["status"], request["decided_by"])
                if request["decision_comment"]:
                    outcome += f": {request['decision_comment']}"
                if request["result"]:
                    outcome += f" — {request['result']}"
            else:
                outcome = request["result"] or ""
            values = [
                request["id"],
                (request["created_at"] or "")[:19].replace("T", " "),
                request["summary"],
                request["reason"],
                request["requested_by"],
                f"{request['status']} · job {request['job_state']}"
                if request.get("job_state")
                else request["status"],
                outcome,
            ]
            for col, value in enumerate(values):
                self._table.setItem(row_idx, col, QTableWidgetItem(str(value)))
            if request["status"] == agent_requests_service.PENDING:
                cell = QWidget()
                lay = QHBoxLayout(cell)
                lay.setContentsMargins(2, 0, 2, 0)
                approve = QPushButton(self.tr("Approve"))
                approve.setObjectName("primaryButton")
                approve.clicked.connect(lambda _=False, r=request: self._decide(r, True))
                reject = QPushButton(self.tr("Reject"))
                reject.clicked.connect(lambda _=False, r=request: self._decide(r, False))
                lay.addWidget(approve)
                lay.addWidget(reject)
                self._table.setCellWidget(row_idx, 7, cell)
            else:
                self._table.removeCellWidget(row_idx, 7)
        self._table.resizeColumnsToContents()

    def _decide(self, request, approve):
        if approve:
            answer = QMessageBox.question(
                self,
                self.tr("Approve Request"),
                self.tr("Send this to the chamber now?\n\n{0}\n\nReason given: {1}").format(
                    request["summary"], request["reason"]
                ),
            )
            if answer != QMessageBox.Yes:
                return
            comment = ""
        else:
            comment, ok = QInputDialog.getText(
                self, self.tr("Reject Request"), self.tr("Why (optional, the agent sees it):")
            )
            if not ok:
                return
        try:
            agent_requests_service.decide(
                request["id"], approve, self.current_user.get("name") or "Unknown", comment
            )
        except agent_requests_service.AgentRequestError as exc:
            QMessageBox.warning(self, self.tr("Agent Requests"), str(exc))
        self._refresh()
