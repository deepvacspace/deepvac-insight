"""MonitoringMixin — builds the Live Monitoring page shell.

Up to MAX_CONNECTED_CHAMBERS (services/chamber_session.py) chambers can be
connected at once, each as its own tab in self.monitor_editor_area (the
same tab_system.EditorArea component Analysis uses for run tabs). This
module only owns the "pick a saved chamber and connect it" header and the
housekeeping around opening/closing those tabs -- everything about one
chamber's live data, chart, and alarms lives in app/monitor_tab.py's
MonitorTabPage, and the actual ChamberSession lifecycle (connect/
disconnect/teardown, the 5-chamber cap) lives on DeepVacDesktop itself
(_connect_chamber/_disconnect_chamber in main_window.py) since Controller
and the OPC Server page also need to reach it.

Test Profiles and manual setpoints (multi-step setpoint schedules run
against a connected chamber) live on the separate Controller page -- see
views/controller.py -- not here.
"""

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import app.services.chambers_service as chambers_service
from app.services.chamber_session import MAX_CONNECTED_CHAMBERS
from app.tab_system import EditorArea


class MonitoringMixin:
    def _monitoring_view(self):
        container = QWidget()
        container.setObjectName("workspaceBody")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)

        hdr = QLabel(self.tr("Live Monitoring"))
        hdr.setObjectName("pageTitle")
        outer.addWidget(hdr)
        sub = QLabel(self.tr("Connect up to {0} chambers at once.").format(MAX_CONNECTED_CHAMBERS))
        sub.setObjectName("sectionLabel")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        conn_row = QHBoxLayout()
        conn_row.setSpacing(6)
        lbl = QLabel(self.tr("Chamber"))
        lbl.setObjectName("sectionLabel")
        conn_row.addWidget(lbl)
        self._mon_chamber_combo = QComboBox()
        self._mon_chamber_combo.setMinimumWidth(220)
        self._mon_chamber_combo.setToolTip(self.tr("Which saved chamber to connect to."))
        conn_row.addWidget(self._mon_chamber_combo)
        manage_chambers_btn = QPushButton("…")
        manage_chambers_btn.setFixedWidth(26)
        manage_chambers_btn.setToolTip(self.tr("Manage saved chambers…"))
        manage_chambers_btn.clicked.connect(self._open_chambers_dialog)
        conn_row.addWidget(manage_chambers_btn)
        self._mon_connect_new_btn = QPushButton(self.tr("Connect"))
        self._mon_connect_new_btn.setObjectName("primaryButton")
        self._mon_connect_new_btn.clicked.connect(self._on_mon_connect_new)
        conn_row.addWidget(self._mon_connect_new_btn)
        conn_row.addStretch(1)
        self._mon_count_lbl = QLabel()
        self._mon_count_lbl.setObjectName("sectionLabel")
        conn_row.addWidget(self._mon_count_lbl)
        outer.addLayout(conn_row)

        self.monitor_editor_area = EditorArea()
        self.monitor_editor_area.page_closed.connect(self._on_monitor_tab_closed)
        outer.addWidget(self.monitor_editor_area, 1)

        self._load_chamber_choices()
        self._update_mon_count_label()
        return container

    # ── Connect / disconnect ─────────────────────────────────────────────────

    def _load_chamber_choices(self):
        previous = (
            self._mon_chamber_combo.currentData() if self._mon_chamber_combo.count() else None
        )
        self._mon_chamber_combo.blockSignals(True)
        self._mon_chamber_combo.clear()
        try:
            chambers = chambers_service.list_chambers()
        except Exception:
            chambers = []
        available = [c for c in chambers if c["id"] not in self._chamber_sessions]
        for chamber in available:
            self._mon_chamber_combo.addItem(
                f"{chamber['name']}  ({chamber['host']}:{chamber['port']})", chamber
            )
        if previous is not None:
            idx = next(
                (
                    i
                    for i in range(self._mon_chamber_combo.count())
                    if self._mon_chamber_combo.itemData(i)["id"] == previous["id"]
                ),
                -1,
            )
            if idx >= 0:
                self._mon_chamber_combo.setCurrentIndex(idx)
        self._mon_chamber_combo.blockSignals(False)

        at_cap = len(self._chamber_sessions) >= MAX_CONNECTED_CHAMBERS
        has_choice = self._mon_chamber_combo.count() > 0
        self._mon_connect_new_btn.setEnabled(not at_cap and has_choice)
        if at_cap:
            tip = self.tr("Up to {0} chambers can be connected at once.").format(
                MAX_CONNECTED_CHAMBERS
            )
        elif not has_choice:
            tip = self.tr("Add a chamber first (click … next to the Chamber field).")
        else:
            tip = ""
        self._mon_connect_new_btn.setToolTip(tip)

    def _update_mon_count_label(self):
        self._mon_count_lbl.setText(
            self.tr("{0} / {1} connected").format(
                len(self._chamber_sessions), MAX_CONNECTED_CHAMBERS
            )
        )

    def _open_chambers_dialog(self):
        from app.chambers_dialog import ChambersDialog

        dlg = ChambersDialog(parent=self)
        dlg.exec()
        if dlg.changed:
            self._load_chamber_choices()

    def _on_mon_connect_new(self):
        chamber = self._mon_chamber_combo.currentData()
        if not chamber:
            QMessageBox.warning(
                self,
                self.tr("Connect"),
                self.tr("Add a chamber first (click … next to the Chamber field)."),
            )
            return
        # _connect_chamber also refreshes this combo/label -- see _refresh_chamber_ui()
        try:
            self._connect_chamber(chamber)
        except RuntimeError as exc:
            QMessageBox.warning(self, self.tr("Connect"), str(exc))

    def _on_monitor_tab_closed(self, key):
        if not key.startswith("chamber-"):
            return
        chamber_id = int(key.split("-", 1)[1])
        self._disconnect_chamber(chamber_id)  # also refreshes this combo/label
