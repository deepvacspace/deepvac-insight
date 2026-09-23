"""OrgDirectoryDialog — read-only list of the organization's active members, pulled from Deepvac Hub."""

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.services import licensing_client, org_directory_service
from app.services import org_directory_sync_service as sync_service

_COLUMNS = ["Name", "Email", "Role"]


class OrgDirectoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Organization Directory"))
        self.setMinimumSize(480, 400)
        self._build_ui()
        self._refresh_table()

    def _build_ui(self):
        root = QVBoxLayout(self)

        self._status_lbl = QLabel()
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        self._table = QTableWidget()
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in _COLUMNS])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self._table, 1)

        button_row = QHBoxLayout()
        update_btn = QPushButton(self.tr("Update"))
        update_btn.setObjectName("primaryButton")
        update_btn.clicked.connect(self._update_from_hub)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(update_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

    def _refresh_table(self):
        members = org_directory_service.list_members()
        self._table.setRowCount(len(members))
        for row, member in enumerate(members):
            for col, value in enumerate([member["display_name"], member["email"], member["role"]]):
                self._table.setItem(row, col, QTableWidgetItem(str(value)))

        synced_at = org_directory_service.last_synced_at()
        if synced_at:
            self._status_lbl.setText(self.tr("Last updated: {0}").format(synced_at))
        else:
            self._status_lbl.setText(self.tr("Never updated yet — click Update to fetch it."))

    def _update_from_hub(self):
        try:
            sync_service.pull()
        except (sync_service.SyncError, licensing_client.LicensingError) as exc:
            QMessageBox.warning(self, self.tr("Update"), str(exc))
            return
        self._refresh_table()
