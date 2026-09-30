"""CollabDialog — host or join a local-network collaboration group and sync shared data."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.services import (
    collab_client,
    collab_host_service,
    collab_sync_service,
    collab_tls,
    settings_service,
)
from app.services.collab_server import local_addresses

_LOOPBACK_PREFIX = "https://127.0.0.1:"


class CollabDialog(QDialog):
    def __init__(self, server, current_user=None, parent=None):
        super().__init__(parent)
        self._server = server
        self._user_name = (current_user or {}).get("name") or "Unknown"
        self.setWindowTitle(self.tr("Collaboration"))
        self.setMinimumWidth(480)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        host_lbl = QLabel(self.tr("HOST ON THIS COMPUTER"))
        host_lbl.setObjectName("sectionLabel")
        root.addWidget(host_lbl)

        self._host_status_lbl = QLabel()
        self._host_status_lbl.setWordWrap(True)
        self._host_status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self._host_status_lbl)

        self._code_lbl = QLabel()
        self._code_lbl.setAlignment(Qt.AlignCenter)
        self._code_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        code_font = self._code_lbl.font()
        code_font.setPointSize(18)
        code_font.setBold(True)
        self._code_lbl.setFont(code_font)
        root.addWidget(self._code_lbl)

        host_buttons = QHBoxLayout()
        self._host_btn = QPushButton()
        self._host_btn.clicked.connect(self._toggle_hosting)
        self._new_code_btn = QPushButton(self.tr("New join code"))
        self._new_code_btn.clicked.connect(self._new_join_code)
        host_buttons.addWidget(self._host_btn)
        host_buttons.addWidget(self._new_code_btn)
        host_buttons.addStretch(1)
        root.addLayout(host_buttons)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)

        connect_lbl = QLabel(self.tr("CONNECT TO A HOST"))
        connect_lbl.setObjectName("sectionLabel")
        root.addWidget(connect_lbl)

        self._connection_lbl = QLabel()
        self._connection_lbl.setWordWrap(True)
        root.addWidget(self._connection_lbl)

        form = QFormLayout()
        self._address_ed = QLineEdit()
        self._address_ed.setPlaceholderText(self.tr("192.168.1.20:8765"))
        self._join_ed = QLineEdit()
        self._join_ed.setPlaceholderText(self.tr("XXXX-XXXX"))
        form.addRow(self.tr("Host address"), self._address_ed)
        form.addRow(self.tr("Join code"), self._join_ed)
        root.addLayout(form)

        connect_buttons = QHBoxLayout()
        self._connect_btn = QPushButton(self.tr("Connect"))
        self._connect_btn.clicked.connect(self._connect)
        self._disconnect_btn = QPushButton(self.tr("Disconnect"))
        self._disconnect_btn.clicked.connect(self._disconnect)
        connect_buttons.addWidget(self._connect_btn)
        connect_buttons.addWidget(self._disconnect_btn)
        connect_buttons.addStretch(1)
        root.addLayout(connect_buttons)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        root.addWidget(sep2)

        sync_buttons = QHBoxLayout()
        self._sync_btn = QPushButton(self.tr("Sync now"))
        self._sync_btn.setObjectName("primaryButton")
        self._sync_btn.clicked.connect(self._sync)
        shared_runs_btn = QPushButton(self.tr("Shared runs…"))
        shared_runs_btn.clicked.connect(self._show_shared_runs)
        sync_buttons.addWidget(self._sync_btn)
        sync_buttons.addWidget(shared_runs_btn)
        sync_buttons.addStretch(1)
        root.addLayout(sync_buttons)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    def _refresh(self):
        hosting = self._server.is_running()
        current = collab_client.connection()

        if hosting:
            addresses = ", ".join(local_addresses(self._server.port)) or self.tr("no network found")
            self._host_status_lbl.setText(
                self.tr(
                    "Hosting on: {0}\n{1} paired device(s).\nCertificate fingerprint: {2}"
                ).format(
                    addresses,
                    collab_host_service.peer_count(),
                    collab_tls.fingerprint(self._server.cert_pem),
                )
            )
            self._host_btn.setText(self.tr("Stop hosting"))
        else:
            self._host_status_lbl.setText(
                self.tr(
                    "Not hosting. Start hosting to let other Insight installations on your "
                    "network share data with this computer."
                )
            )
            self._host_btn.setText(self.tr("Start hosting"))
            self._code_lbl.setText("")
        self._new_code_btn.setEnabled(hosting)

        if current is None:
            self._connection_lbl.setText(self.tr("Not connected."))
        elif hosting:
            self._connection_lbl.setText(self.tr("Connected to this computer's own host."))
        else:
            self._connection_lbl.setText(
                self.tr("Connected to {0} ({1}).").format(current["name"], current["url"])
            )
        for widget in (self._address_ed, self._join_ed, self._connect_btn):
            widget.setEnabled(not hosting)
        self._disconnect_btn.setEnabled(current is not None and not hosting)
        self._sync_btn.setEnabled(current is not None)

    def _toggle_hosting(self):
        if self._server.is_running():
            self._stop_hosting()
        else:
            self._start_hosting()
        self._refresh()

    def _start_hosting(self):
        try:
            self._server.start(settings_service.load_collab_port())
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Collaboration"), str(exc))
            return
        settings_service.save_collab_host_enabled(True)
        collab_client.connect_local(
            self._server.port, self._server.host_name, self._user_name, self._server.cert_pem
        )
        self._code_lbl.setText(self._server.new_join_code())

    def _stop_hosting(self):
        self._server.stop()
        settings_service.save_collab_host_enabled(False)
        current = collab_client.connection()
        if current is not None and current["url"].startswith(_LOOPBACK_PREFIX):
            collab_client.disconnect()

    def _new_join_code(self):
        self._code_lbl.setText(self._server.new_join_code())

    def _connect(self):
        address = self._address_ed.text()
        try:
            cert_pem = collab_client.fetch_host_certificate(address)
        except collab_client.CollabError as exc:
            QMessageBox.warning(self, self.tr("Collaboration"), str(exc))
            return
        confirmed = QMessageBox.question(
            self,
            self.tr("Verify host"),
            self.tr(
                "The host's certificate fingerprint is:\n\n{0}\n\n"
                "Only continue if it matches the fingerprint shown on the host's screen."
            ).format(collab_tls.fingerprint(cert_pem)),
        )
        if confirmed != QMessageBox.Yes:
            return
        try:
            collab_client.pair(address, self._join_ed.text(), self._user_name, cert_pem)
        except collab_client.CollabError as exc:
            QMessageBox.warning(self, self.tr("Collaboration"), str(exc))
            return
        self._join_ed.clear()
        self._refresh()

    def _disconnect(self):
        collab_client.disconnect()
        self._refresh()

    def _sync(self):
        result = collab_sync_service.sync(self._user_name)
        labels = {
            "annotation": self.tr("Annotations"),
            "variable_rule": self.tr("Variable rules"),
            "run_metadata": self.tr("Run metadata"),
            "alarm_event": self.tr("Alarm events"),
        }
        lines = [
            self.tr("{0}: shared {1}, received {2}, removed {3}, skipped {4}").format(
                labels[kind], r.pushed, r.pulled, r.removed, r.skipped
            )
            for kind, r in result.kinds.items()
        ]
        if result.errors:
            lines += ["", *result.errors]
        box = QMessageBox.warning if result.errors else QMessageBox.information
        box(self, self.tr("Collaboration"), "\n".join(lines))

    def _show_shared_runs(self):
        from app.shared_runs_dialog import SharedRunsDialog

        SharedRunsDialog(self).exec()
