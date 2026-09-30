"""SharedRunsDialog — read-only catalog of runs shared through the collaboration host."""

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.services import run_identity, shared_runs_service

_COLUMNS = [
    "Run",
    "Group",
    "Samples",
    "Duration (s)",
    "MAE",
    "Start",
    "Shared by",
    "On this computer",
]


class SharedRunsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Shared Runs"))
        self.setMinimumSize(760, 400)
        self._build_ui()
        self._refresh_table()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self._table = QTableWidget()
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in _COLUMNS])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self._table, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

    def _refresh_table(self):
        runs = shared_runs_service.list_runs()
        local_uids = set(run_identity.run_fingerprints().values())
        self._table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            values = [
                run.get("id"),
                run.get("group"),
                run.get("samples"),
                run.get("duration_s"),
                run.get("mae"),
                run.get("start_time"),
                run.get("shared_by"),
                self.tr("Yes") if run["run_uid"] in local_uids else self.tr("No"),
            ]
            for col, value in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
