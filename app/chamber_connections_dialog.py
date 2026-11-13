"""Dialog to connect and disconnect saved chambers."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import app.services.chambers_service as chambers_service
from app.services.chamber_session import MAX_CONNECTED_CHAMBERS


class ChamberConnectionsDialog(QDialog):
    def __init__(self, host, parent=None):
        super().__init__(parent or host)
        self.host = host
        self.setWindowTitle(self.tr("Chamber Connections"))
        self.setMinimumWidth(620)
        self._chambers = {}
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(self.tr("CHAMBER"))
        title.setObjectName("sectionLabel")
        head.addWidget(title)
        head.addStretch(1)
        self.count_lbl = QLabel()
        self.count_lbl.setObjectName("sectionLabel")
        head.addWidget(self.count_lbl)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(300)
        self.combo.setToolTip(self.tr("Saved chambers. ● connected."))
        self.combo.currentIndexChanged.connect(self.refresh_status)
        row.addWidget(self.combo, 1)
        settings_btn = QPushButton(self.tr("Chamber Settings…"))
        settings_btn.setToolTip(self.tr("Add, edit or delete saved chambers"))
        settings_btn.clicked.connect(self._open_chambers_dialog)
        row.addWidget(settings_btn)
        self.connect_btn = QPushButton(self.tr("Connect"))
        self.connect_btn.setObjectName("primaryButton")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        row.addWidget(self.connect_btn)
        lay.addLayout(row)

        status = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setObjectName("chamberIconOff")
        status.addWidget(self.dot)
        self.status_lbl = QLabel()
        self.status_lbl.setObjectName("statusText")
        self.status_lbl.setWordWrap(True)
        status.addWidget(self.status_lbl, 1)
        lay.addLayout(status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self.refresh()

    def _sessions(self):
        return self.host._chamber_sessions

    def selected_session(self):
        chamber_id = self.combo.currentData()
        return self._sessions().get(chamber_id) if chamber_id is not None else None

    def selected_chamber(self):
        chamber_id = self.combo.currentData()
        session = self._sessions().get(chamber_id)
        return session.chamber if session is not None else self._chambers.get(chamber_id)

    def select(self, chamber_id):
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == chamber_id:
                self.combo.setCurrentIndex(i)
                return

    def refresh(self):
        """Every saved chamber, connected ones marked ●."""
        previous = self.combo.currentData()
        try:
            saved = chambers_service.list_chambers()
        except Exception:
            saved = []
        sessions = self._sessions()
        self._chambers = {c["id"]: c for c in saved}
        for chamber_id, session in sessions.items():
            self._chambers.setdefault(chamber_id, session.chamber)
        self.combo.blockSignals(True)
        self.combo.clear()
        for chamber in self._chambers.values():
            mark = "● " if chamber["id"] in sessions else ""
            kind = "PLC" if chamber.get("protocol") == "plc" else "JSON"
            self.combo.addItem(
                f"{mark}{chamber['name']}  ({chamber['host']}:{chamber['port']} · {kind})",
                chamber["id"],
            )
        if self.combo.count():
            ids = [self.combo.itemData(i) for i in range(self.combo.count())]
            fallback = next((i for i, cid in enumerate(ids) if cid in sessions), 0)
            self.combo.setCurrentIndex(ids.index(previous) if previous in ids else fallback)
        self.combo.blockSignals(False)
        self.count_lbl.setText(
            self.tr("{0} / {1} connected").format(len(sessions), MAX_CONNECTED_CHAMBERS)
        )
        self.refresh_status()

    def refresh_status(self, *_):
        session = self.selected_session()
        chamber = self.selected_chamber()
        connected = session.is_connected() if session else False
        is_plc = chamber is not None and chamber.get("protocol") == "plc"
        self.dot.setObjectName("chamberIconOn" if connected else "chamberIconOff")
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        if chamber is None:
            status = self.tr("No saved chambers — add one with Chamber Settings….")
        elif is_plc:
            status = self.tr(
                "{0} is a chamber PLC: it connects by itself, is controlled on the Controller page "
                "and followed in Analysis → Live. Agents' requests for it wait in Agent Requests."
            ).format(chamber["name"])
        elif session is None:
            status = self.tr("Not connected — {0} ({1}:{2})").format(
                chamber["name"], chamber["host"], chamber["port"]
            )
        elif connected:
            status = self.tr(
                "Connected — {0} ({1}:{2}) · recording {3} sample(s); follow it in Analysis → Live"
            ).format(chamber["name"], chamber["host"], chamber["port"], len(session.mon_buffer))
        else:
            note = session.connection_note or self.tr("connecting…")
            status = self.tr("Offline — {0} ({1}:{2}): {3}").format(
                chamber["name"], chamber["host"], chamber["port"], note
            )
        self.status_lbl.setText(status)
        at_cap = len(self._sessions()) >= MAX_CONNECTED_CHAMBERS
        self.connect_btn.setText(self.tr("Disconnect") if session else self.tr("Connect"))
        self.connect_btn.setEnabled(
            chamber is not None and not is_plc and (session is not None or not at_cap)
        )
        self.connect_btn.setToolTip(
            self.tr("Up to {0} chambers can be connected at once.").format(MAX_CONNECTED_CHAMBERS)
            if at_cap and session is None
            else ""
        )

    def _open_chambers_dialog(self):
        from app.chambers_dialog import ChambersDialog

        dlg = ChambersDialog(parent=self)
        dlg.exec()
        if dlg.changed:
            self.host._refresh_chamber_ui()

    def _on_connect_clicked(self):
        chamber = self.selected_chamber()
        if chamber is None:
            QMessageBox.warning(
                self, self.tr("Connect"), self.tr("Add a chamber first (Chamber Settings…).")
            )
            return
        session = self._sessions().get(chamber["id"])
        if session is None:
            try:
                self.host._connect_chamber(chamber)
            except RuntimeError as exc:
                QMessageBox.warning(self, self.tr("Connect"), str(exc))
            return
        if session.mon_buffer:
            answer = QMessageBox.question(
                self,
                self.tr("Disconnect"),
                self.tr(
                    "{0} sample(s) recorded from {1} are not saved. Save them as a run first?"
                ).format(len(session.mon_buffer), chamber["name"]),
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save and not self._save_session_as_run(session):
                return
        self.host._disconnect_chamber(chamber["id"])

    def _save_session_as_run(self, session):
        """Saves a chamber's recorded samples as a run."""
        from app.services import data_service

        started = datetime.fromtimestamp(session.mon_buffer[0]["timestamp"]).strftime(
            "%Y-%m-%d %H:%M"
        )
        try:
            result = data_service.save_monitoring_session(
                f"{started} {session.chamber['name']}",
                session.mon_buffer,
                chamber=session.chamber["name"],
                test_profile=session.session_test_profile_name,
            )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Save failed"), str(exc))
            return False
        session.clear_buffer()
        self.host._on_live_run_saved(result["key"])
        return True
