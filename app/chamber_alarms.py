"""Panel for a chamber's alarm rules and history."""

import contextlib

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.services import alarm_rule_sync_service as sync_service
from app.services import alarms_service, licensing_client

_CONDITIONS = ["above", "below", "outside range"]
_SEVERITIES = ["Info", "Warning", "Critical"]
_VARIABLES = ["temp", "temp_ref", "kp", "ki", "kd", "temp_u", "temp_u_p", "temp_u_i", "temp_u_d"]


class ChamberAlarmsPanel(QFrame):
    def __init__(self, current_user=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.current_user = current_user or {"id": None, "name": "Unknown", "email": ""}
        self.chamber = None
        self.session = None
        self._build_ui()
        self.set_target(None, None)

    def set_target(self, chamber, session):
        """The chamber whose rules are shown, and its session when connected."""
        if self.session is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self.session.alarms_changed.disconnect(self.refresh)
        self.chamber = chamber
        self.session = session
        if session is not None:
            session.alarms_changed.connect(self.refresh)
        enabled = chamber is not None
        for widget in (self._history_btn, self._add_btn, self._sync_btn):
            widget.setEnabled(enabled)
        if not enabled:
            self._alarm_form.setVisible(False)
        self.refresh()

    def _rules(self):
        if self.session is not None:
            return self.session.alarms
        if self.chamber is None:
            return []
        try:
            return alarms_service.list_rules(chamber_id=self.chamber["id"])
        except Exception:
            return []

    def _build_ui(self):
        al = QVBoxLayout(self)
        al.setContentsMargins(14, 14, 14, 14)
        al.setSpacing(10)

        alarms_hdr = QHBoxLayout()
        albl = QLabel(self.tr("ALARMS"))
        albl.setObjectName("sectionLabel")
        alarms_hdr.addWidget(albl)
        self._desc = QLabel()
        self._desc.setObjectName("sectionLabel")
        self._desc.setWordWrap(True)
        alarms_hdr.addWidget(self._desc, 1)
        self._history_btn = QPushButton(self.tr("History…"))
        self._history_btn.clicked.connect(self._open_alarm_history)
        alarms_hdr.addWidget(self._history_btn)
        self._add_btn = QPushButton(self.tr("+ Add Alarm"))
        self._add_btn.setObjectName("primaryButton")
        self._add_btn.clicked.connect(self._toggle_alarm_form)
        alarms_hdr.addWidget(self._add_btn)
        self._sync_btn = QPushButton(self.tr("Sync with Hub"))
        self._sync_btn.clicked.connect(self._sync_alarms_with_hub)
        alarms_hdr.addWidget(self._sync_btn)
        al.addLayout(alarms_hdr)

        self._alarm_form = QFrame()
        self._alarm_form.setObjectName("ruleRow")
        afl = QHBoxLayout(self._alarm_form)
        afl.setContentsMargins(8, 8, 8, 8)
        afl.setSpacing(8)

        self._alarm_name_ed = QLineEdit()
        self._alarm_name_ed.setPlaceholderText(self.tr("Alarm name"))
        self._alarm_name_ed.setFixedWidth(130)
        self._alarm_var_combo = QComboBox()
        self._alarm_var_combo.addItems(_VARIABLES)
        self._alarm_var_combo.setEditable(True)
        self._alarm_var_combo.setFixedWidth(100)
        self._alarm_cond_combo = QComboBox()
        for cond in _CONDITIONS:
            self._alarm_cond_combo.addItem(self._cond_label(cond), cond)
        self._alarm_cond_combo.setFixedWidth(110)
        self._alarm_val_ed = QLineEdit()
        self._alarm_val_ed.setPlaceholderText(self.tr("threshold"))
        self._alarm_val_ed.setFixedWidth(80)
        self._alarm_val2_ed = QLineEdit()
        self._alarm_val2_ed.setPlaceholderText(self.tr("upper (range)"))
        self._alarm_val2_ed.setFixedWidth(100)
        self._alarm_sev_combo = QComboBox()
        for sev in _SEVERITIES:
            self._alarm_sev_combo.addItem(self._sev_label(sev), sev)
        self._alarm_sev_combo.setFixedWidth(90)

        self._alarm_deadband_ed = QLineEdit("0")
        self._alarm_deadband_ed.setPlaceholderText(self.tr("deadband"))
        self._alarm_deadband_ed.setToolTip(
            self.tr(
                "Once active, the value must return past the threshold by at least this "
                "much before the alarm clears -- prevents rapid on/off flicker right at "
                "the edge of the threshold."
            )
        )
        self._alarm_deadband_ed.setFixedWidth(70)

        self._alarm_delay_ed = QLineEdit("0")
        self._alarm_delay_ed.setPlaceholderText(self.tr("delay (s)"))
        self._alarm_delay_ed.setToolTip(
            self.tr(
                "The condition must hold continuously for this many seconds before the "
                "alarm actually triggers -- prevents a single noisy sample from firing it."
            )
        )
        self._alarm_delay_ed.setFixedWidth(70)

        save_alarm_btn = QPushButton(self.tr("Add"))
        save_alarm_btn.setObjectName("primaryButton")
        save_alarm_btn.clicked.connect(self._save_alarm)
        cancel_alarm_btn = QPushButton(self.tr("Cancel"))
        cancel_alarm_btn.clicked.connect(self._toggle_alarm_form)

        for cap, w in [
            (self.tr("Name"), self._alarm_name_ed),
            (self.tr("Variable"), self._alarm_var_combo),
            (self.tr("Condition"), self._alarm_cond_combo),
            (self.tr("Value"), self._alarm_val_ed),
            ("", self._alarm_val2_ed),
            (self.tr("Severity"), self._alarm_sev_combo),
            (self.tr("Deadband"), self._alarm_deadband_ed),
            (self.tr("Delay"), self._alarm_delay_ed),
            ("", save_alarm_btn),
            ("", cancel_alarm_btn),
        ]:
            if cap:
                lbl = QLabel(cap)
                lbl.setObjectName("sectionLabel")
                afl.addWidget(lbl)
            afl.addWidget(w)
        afl.addStretch(1)
        self._alarm_form.setVisible(False)
        al.addWidget(self._alarm_form)

        self._alarms_table = QTableWidget()
        self._alarms_table.setMinimumHeight(140)
        self._alarms_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._alarms_table.setSelectionMode(QAbstractItemView.SingleSelection)
        al.addWidget(self._alarms_table)

    def _cond_label(self, cond):
        return {
            "above": self.tr("above"),
            "below": self.tr("below"),
            "outside range": self.tr("outside range"),
        }.get(cond, cond)

    def _sev_label(self, sev):
        return {
            "Info": self.tr("Info"),
            "Warning": self.tr("Warning"),
            "Critical": self.tr("Critical"),
        }.get(sev, sev)

    def _toggle_alarm_form(self):
        self._alarm_form.setVisible(not self._alarm_form.isVisible())

    def _open_alarm_history(self):
        from app.alarm_history_dialog import AlarmHistoryDialog

        if self.chamber is None:
            return
        dlg = AlarmHistoryDialog(
            current_user=self.current_user, chamber_id=self.chamber["id"], parent=self
        )
        dlg.exec()

    def _save_alarm(self):
        if self.chamber is None:
            return
        name = self._alarm_name_ed.text().strip()
        var = self._alarm_var_combo.currentText().strip()
        cond = self._alarm_cond_combo.currentData()
        val_text = self._alarm_val_ed.text().strip()
        val2_text = self._alarm_val2_ed.text().strip()
        sev = self._alarm_sev_combo.currentData()
        if not name or not var or not val_text:
            return
        try:
            value = float(val_text)
            value2 = float(val2_text) if val2_text else None
            deadband = float(self._alarm_deadband_ed.text().strip() or "0")
            delay_s = float(self._alarm_delay_ed.text().strip() or "0")
        except ValueError:
            return
        if deadband < 0 or delay_s < 0:
            QMessageBox.warning(
                self, self.tr("Add Alarm"), self.tr("Deadband and delay must not be negative.")
            )
            return
        created_by = self.current_user.get("name") or "Unknown"
        try:
            if self.session is not None:
                self.session.add_alarm_rule(
                    name,
                    var,
                    cond,
                    value,
                    value2,
                    sev,
                    deadband=deadband,
                    delay_s=delay_s,
                    created_by=created_by,
                )
            else:
                alarms_service.add_rule(
                    name,
                    var,
                    cond,
                    value,
                    value2,
                    sev,
                    deadband=deadband,
                    delay_s=delay_s,
                    created_by=created_by,
                    chamber_id=self.chamber["id"],
                    chamber_name=self.chamber["name"],
                )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Add Alarm"), str(exc))
            return
        self._alarm_name_ed.clear()
        self._alarm_val_ed.clear()
        self._alarm_val2_ed.clear()
        self._alarm_deadband_ed.setText("0")
        self._alarm_delay_ed.setText("0")
        self._alarm_form.setVisible(False)
        self.refresh()

    def _delete_alarm(self, idx):
        if self.session is not None:
            self.session.delete_alarm_rule(idx)
        else:
            rules = self._rules()
            if 0 <= idx < len(rules):
                alarms_service.delete_rule(rules[idx]["id"])
        self.refresh()

    def _sync_alarms_with_hub(self):
        if self.chamber is None:
            return
        try:
            result = sync_service.sync(self.chamber)
        except (sync_service.SyncError, licensing_client.LicensingError) as exc:
            QMessageBox.warning(self, self.tr("Sync with Hub"), str(exc))
            return

        for conflict in result.conflicts:
            self._resolve_alarm_conflict(conflict)

        if self.session is not None:
            self.session.reload_alarm_rules()
        self.refresh()

        summary = self.tr("Pushed {0}, pulled {1}.").format(len(result.pushed), len(result.pulled))
        if result.errors:
            summary += "\n\n" + self.tr("Some alarm rules could not be synced:")
            summary += "\n" + "\n".join(result.errors)
        QMessageBox.information(self, self.tr("Sync with Hub"), summary)

    def _resolve_alarm_conflict(self, conflict):
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Sync Conflict"))
        box.setText(
            self.tr(
                "'{0}' differs between this computer and Deepvac Hub. "
                "Which version do you want to keep?"
            ).format(conflict.local["name"])
        )
        keep_local_btn = box.addButton(self.tr("Keep Local"), QMessageBox.AcceptRole)
        keep_hub_btn = box.addButton(self.tr("Keep Hub"), QMessageBox.AcceptRole)
        box.addButton(self.tr("Skip"), QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep_local_btn:
            keep = "local"
        elif clicked is keep_hub_btn:
            keep = "hub"
        else:
            return
        try:
            sync_service.resolve_conflict(conflict, keep, self.chamber)
        except licensing_client.LicensingError as exc:
            QMessageBox.warning(self, self.tr("Sync Conflict"), str(exc))

    def refresh(self):
        if self.chamber is None:
            self._desc.setText(self.tr("Select a chamber to see its alarms."))
        elif self.session is not None and self.session.is_connected():
            self._desc.setText(
                self.tr("Thresholds for {0}, checked on every sample.").format(self.chamber["name"])
            )
        else:
            self._desc.setText(
                self.tr("Thresholds for {0}; checked while it is connected.").format(
                    self.chamber["name"]
                )
            )
        cols = [
            self.tr("Name"),
            self.tr("Variable"),
            self.tr("Condition"),
            self.tr("Value / Range"),
            self.tr("Severity"),
            self.tr("Status"),
        ]
        rules = self._rules()
        tbl = self._alarms_table
        tbl.setUpdatesEnabled(False)
        tbl.setAlternatingRowColors(True)
        tbl.setShowGrid(False)
        tbl.setWordWrap(False)
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setColumnCount(len(cols) + 1)
        tbl.setRowCount(len(rules))
        tbl.setHorizontalHeaderLabels(cols + [""])
        connected = self.session is not None and self.session.is_connected()
        for ri, alarm in enumerate(rules):
            val_str = str(alarm["value"])
            if alarm.get("value2") is not None:
                val_str += f" – {alarm['value2']}"
            sev = alarm["severity"]
            if not connected:
                is_active, status_display = False, self.tr("Inactive (not connected)")
            elif alarm.get("_active"):
                is_active, status_display = True, self.tr("Active")
            else:
                is_active, status_display = False, self.tr("Inactive")
            values = [
                alarm["name"],
                alarm["variable"],
                self._cond_label(alarm["condition"]),
                val_str,
                self._sev_label(sev),
                status_display,
            ]
            for ci, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if ci == 4:
                    color = {"Info": "#60a5fa", "Warning": "#f2bd52", "Critical": "#ff6f7d"}.get(
                        sev, "#94a3b8"
                    )
                    item.setForeground(QColor(color))
                elif ci == 5 and is_active:
                    item.setForeground(QColor("#ff6f7d"))
                tbl.setItem(ri, ci, item)
            del_btn = QPushButton("✕")
            del_btn.setObjectName("tabClose")
            del_btn.setFixedSize(24, 24)
            del_btn.clicked.connect(lambda _=False, i=ri: self._delete_alarm(i))
            tbl.setCellWidget(ri, len(cols), del_btn)
        tbl.resizeColumnsToContents()
        tbl.setUpdatesEnabled(True)
