"""ChambersDialog — add/edit/delete saved chamber connections
(app/services/chambers_service.py) from Live Monitoring's chamber picker."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.services import chambers_service


class ChambersDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.changed = False
        self._editing_id = None

        self.setWindowTitle(self.tr("Manage Chambers"))
        self.setMinimumWidth(420)
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        root = QVBoxLayout(self)

        info = QLabel(
            self.tr(
                "Saved chambers you can pick from in Live Monitoring."
                " Double-click one below to edit it."
            )
        )
        info.setWordWrap(True)
        root.addWidget(info)

        self._list = QListWidget()
        self._list.setMinimumHeight(120)
        self._list.itemDoubleClicked.connect(self._edit_selected)
        root.addWidget(self._list)

        form = QFormLayout()
        self._name_ed = QLineEdit()
        self._name_ed.setPlaceholderText(self.tr("e.g. Chamber 2"))
        form.addRow(self.tr("Name"), self._name_ed)

        host_row = QHBoxLayout()
        self._host_ed = QLineEdit("127.0.0.1")
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(5555)
        self._port_spin.setFixedWidth(80)
        host_row.addWidget(self._host_ed)
        host_row.addWidget(self._port_spin)
        form.addRow(self.tr("Host / Port"), host_row)
        root.addLayout(form)

        button_row = QHBoxLayout()
        self._save_btn = QPushButton(self.tr("Add"))
        self._save_btn.setObjectName("primaryButton")
        self._save_btn.clicked.connect(self._save)
        self._delete_btn = QPushButton(self.tr("Delete Selected"))
        self._delete_btn.clicked.connect(self._delete_selected)
        cancel_edit_btn = QPushButton(self.tr("Cancel Edit"))
        cancel_edit_btn.clicked.connect(self._reset_form)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._save_btn)
        button_row.addWidget(self._delete_btn)
        button_row.addWidget(cancel_edit_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

    def _refresh_list(self):
        self._list.clear()
        for chamber in chambers_service.list_chambers():
            item = QListWidgetItem(f"{chamber['name']}  —  {chamber['host']}:{chamber['port']}")
            item.setData(Qt.UserRole, chamber)
            self._list.addItem(item)

    def _edit_selected(self, item):
        chamber = item.data(Qt.UserRole)
        self._editing_id = chamber["id"]
        self._name_ed.setText(chamber["name"])
        self._host_ed.setText(chamber["host"])
        self._port_spin.setValue(chamber["port"])
        self._save_btn.setText(self.tr("Save Changes"))

    def _reset_form(self):
        self._editing_id = None
        self._name_ed.clear()
        self._host_ed.setText("127.0.0.1")
        self._port_spin.setValue(5555)
        self._save_btn.setText(self.tr("Add"))

    def _save(self):
        name = self._name_ed.text().strip()
        host = self._host_ed.text().strip()
        port = self._port_spin.value()
        try:
            if self._editing_id is not None:
                chambers_service.update_chamber(self._editing_id, name, host, port)
            else:
                chambers_service.add_chamber(name, host, port)
        except chambers_service.ChamberError as exc:
            QMessageBox.critical(self, self.tr("Could not save chamber"), str(exc))
            return
        self.changed = True
        self._reset_form()
        self._refresh_list()

    def _delete_selected(self):
        item = self._list.currentItem()
        if not item:
            return
        chamber = item.data(Qt.UserRole)
        chambers_service.delete_chamber(chamber["id"])
        if self._editing_id == chamber["id"]:
            self._reset_form()
        self.changed = True
        self._refresh_list()
