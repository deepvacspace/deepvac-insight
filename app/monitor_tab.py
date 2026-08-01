"""MonitorTabPage -- the Live Monitoring tab for one connected chamber.

One of these is opened per app.services.chamber_session.ChamberSession
(see app/views/monitoring.py's MonitoringMixin, which creates the session
and this page together and hosts them in a shared EditorArea -- the same
tab-bar/split-pane component app/tab_system.py already provides for
Analysis). Everything here -- live data, the live trend chart, alarm
rules, and "Save Session as Run" -- is scoped to this one chamber; the
session keeps buffering samples and evaluating alarms even while a
different chamber's tab is the one currently in front, so switching tabs
never pauses anything."""

from datetime import datetime, timezone

import pyqtgraph as pg
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
    QWidget,
)

from app.common import COLORS, fmt

# Internal English keys stay stable for storage/comparison; only the label
# shown to the user is translated (built fresh, per language, where used).
_CONDITIONS = ["above", "below", "outside range"]
_SEVERITIES = ["Info", "Warning", "Critical"]

# How many most-recent points the live chart redraws each sample -- the
# full session (for "Save Session as Run") is kept in session.mon_buffer
# uncapped; this only bounds what's actively plotted.
_LIVE_CHART_WINDOW = 500


class MonitorTabPage(QWidget):
    def __init__(self, session, current_user=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.current_user = current_user or {"id": None, "name": "Unknown", "email": ""}
        self.dark = True
        self._mon_curves = {}

        self._build_ui()
        self._wire_session()
        self._refresh_connection_status()
        self._refresh_alarms_table()

    def update_theme(self, dark):
        self.dark = dark

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)

        chamber = self.session.chamber

        status_card = QFrame()
        status_card.setObjectName("card")
        sl = QHBoxLayout(status_card)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setSpacing(10)
        self._mon_dot = QLabel("●")
        self._mon_dot.setObjectName("chamberIconOff")
        sl.addWidget(self._mon_dot)
        self._mon_status_lbl = QLabel(
            self.tr("Connecting to {0} ({1}:{2})…").format(
                chamber["name"], chamber["host"], chamber["port"]
            )
        )
        self._mon_status_lbl.setObjectName("statusText")
        sl.addWidget(self._mon_status_lbl, 1)
        self._mon_recording_lbl = QLabel(self.tr("Recording…"))
        self._mon_recording_lbl.setObjectName("sectionLabel")
        sl.addWidget(self._mon_recording_lbl)
        self._mon_save_session_btn = QPushButton(self.tr("Save Session as Run…"))
        self._mon_save_session_btn.setEnabled(False)
        self._mon_save_session_btn.clicked.connect(self._save_monitoring_session)
        sl.addWidget(self._mon_save_session_btn)
        self._mon_connect_btn = QPushButton(self.tr("Disconnect"))
        self._mon_connect_btn.clicked.connect(self._on_mon_connect_toggle)
        sl.addWidget(self._mon_connect_btn)
        outer.addWidget(status_card)

        # ── Live data + chart ────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        live_card = QFrame()
        live_card.setObjectName("card")
        ll = QVBoxLayout(live_card)
        ll.setContentsMargins(14, 14, 14, 14)
        ll.setSpacing(8)
        live_hdr = QHBoxLayout()
        live_lbl = QLabel(self.tr("LIVE DATA"))
        live_lbl.setObjectName("sectionLabel")
        live_hdr.addWidget(live_lbl)
        live_hdr.addStretch(1)
        self._mon_update_lbl = QLabel("—")
        self._mon_update_lbl.setObjectName("sectionLabel")
        live_hdr.addWidget(self._mon_update_lbl)
        ll.addLayout(live_hdr)

        self._mon_live_table = QTableWidget()
        self._mon_live_table.setColumnCount(2)
        self._mon_live_table.setHorizontalHeaderLabels([self.tr("Variable"), self.tr("Value")])
        self._mon_live_table.verticalHeader().setVisible(False)
        self._mon_live_table.horizontalHeader().setStretchLastSection(True)
        self._mon_live_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._mon_live_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._mon_live_table.setAlternatingRowColors(True)
        self._mon_live_table.setShowGrid(False)
        self._mon_live_table.setMinimumHeight(240)
        ll.addWidget(self._mon_live_table, 1)
        top_row.addWidget(live_card, 1)
        outer.addLayout(top_row)

        trend_card = QFrame()
        trend_card.setObjectName("card")
        tl = QVBoxLayout(trend_card)
        tl.setContentsMargins(14, 14, 14, 14)
        tl.setSpacing(8)
        trend_lbl = QLabel(self.tr("LIVE TREND"))
        trend_lbl.setObjectName("sectionLabel")
        tl.addWidget(trend_lbl)

        self._mon_plot_widget = pg.PlotWidget()
        self._mon_plot_widget.setBackground(None)
        self._mon_plot_widget.showGrid(x=True, y=True, alpha=0.15)
        self._mon_plot_widget.addLegend()
        self._mon_plot_widget.setMinimumHeight(220)
        tl.addWidget(self._mon_plot_widget)
        outer.addWidget(trend_card)

        # ── Alarms ───────────────────────────────────────────────────────────
        alarms_card = QFrame()
        alarms_card.setObjectName("card")
        al = QVBoxLayout(alarms_card)
        al.setContentsMargins(14, 14, 14, 14)
        al.setSpacing(10)

        alarms_hdr = QHBoxLayout()
        albl = QLabel(self.tr("ALARMS"))
        albl.setObjectName("sectionLabel")
        alarms_hdr.addWidget(albl)
        adesc = QLabel(self.tr("Thresholds for this chamber only."))
        adesc.setObjectName("sectionLabel")
        adesc.setWordWrap(True)
        alarms_hdr.addWidget(adesc, 1)
        history_btn = QPushButton(self.tr("History…"))
        history_btn.clicked.connect(self._open_alarm_history)
        alarms_hdr.addWidget(history_btn)
        add_alarm_btn = QPushButton(self.tr("+ Add Alarm"))
        add_alarm_btn.setObjectName("primaryButton")
        add_alarm_btn.clicked.connect(self._toggle_alarm_form)
        alarms_hdr.addWidget(add_alarm_btn)
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
        self._alarm_var_combo.addItems(
            ["temp", "temp_ref", "kp", "ki", "kd", "temp_u", "temp_u_p", "temp_u_i", "temp_u_d"]
        )
        self._alarm_var_combo.setEditable(True)
        self._alarm_var_combo.setFixedWidth(100)
        self._alarm_cond_combo = QComboBox()
        cond_labels = {
            "above": self.tr("above"),
            "below": self.tr("below"),
            "outside range": self.tr("outside range"),
        }
        for cond in _CONDITIONS:
            self._alarm_cond_combo.addItem(cond_labels[cond], cond)
        self._alarm_cond_combo.setFixedWidth(110)
        self._alarm_val_ed = QLineEdit()
        self._alarm_val_ed.setPlaceholderText(self.tr("threshold"))
        self._alarm_val_ed.setFixedWidth(80)
        self._alarm_val2_ed = QLineEdit()
        self._alarm_val2_ed.setPlaceholderText(self.tr("upper (range)"))
        self._alarm_val2_ed.setFixedWidth(100)
        self._alarm_sev_combo = QComboBox()
        sev_labels = {
            "Info": self.tr("Info"),
            "Warning": self.tr("Warning"),
            "Critical": self.tr("Critical"),
        }
        for sev in _SEVERITIES:
            self._alarm_sev_combo.addItem(sev_labels[sev], sev)
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
        self._alarms_table.setMinimumHeight(180)
        self._alarms_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._alarms_table.setSelectionMode(QAbstractItemView.SingleSelection)
        al.addWidget(self._alarms_table)
        outer.addWidget(alarms_card, 1)

    # ── Session wiring ───────────────────────────────────────────────────────

    def _wire_session(self):
        s = self.session
        s.connected.connect(self._on_session_connected)
        s.disconnected.connect(self._on_session_disconnected)
        s.connection_error.connect(self._on_session_error)
        s.reconnecting.connect(self._on_session_reconnecting)
        s.reconnect_failed.connect(self._on_session_reconnect_failed)
        s.sample_received.connect(self._on_sample)
        s.alarms_changed.connect(self._refresh_alarms_table)

    def _on_mon_connect_toggle(self):
        if self.session.is_connected():
            self.session.disconnect()
        else:
            self.session.connect()
            self._mon_status_lbl.setText(
                self.tr("Connecting to {0} ({1}:{2})…").format(
                    self.session.chamber["name"],
                    self.session.chamber["host"],
                    self.session.chamber["port"],
                )
            )

    def _refresh_connection_status(self):
        connected = self.session.is_connected()
        self._set_dot(connected)
        self._mon_connect_btn.setText(self.tr("Disconnect") if connected else self.tr("Connect"))
        if connected:
            chamber = self.session.chamber
            self._mon_status_lbl.setText(
                self.tr("Online — {0} ({1}:{2})").format(
                    chamber["name"], chamber["host"], chamber["port"]
                )
            )
            self._mon_recording_lbl.setText(self.tr("Recording…"))
            self._mon_save_session_btn.setEnabled(False)
        else:
            self._mon_save_session_btn.setEnabled(bool(self.session.mon_buffer))
            if self.session.mon_buffer:
                self._mon_recording_lbl.setText(
                    self.tr("Stopped -- {0} sample(s) recorded").format(
                        len(self.session.mon_buffer)
                    )
                )
            else:
                self._mon_recording_lbl.setText(self.tr("Not recording"))

    def _set_dot(self, connected):
        self._mon_dot.setObjectName("chamberIconOn" if connected else "chamberIconOff")
        self._mon_dot.style().unpolish(self._mon_dot)
        self._mon_dot.style().polish(self._mon_dot)

    def _on_session_connected(self):
        self._refresh_connection_status()

    def _on_session_disconnected(self):
        self._mon_status_lbl.setText(self.tr("Offline — not connected"))
        self._refresh_connection_status()

    def _on_session_error(self, msg):
        self._mon_status_lbl.setText(self.tr("Connection error: {0}").format(msg))

    def _on_session_reconnecting(self, attempt, max_attempts):
        self._mon_status_lbl.setText(
            self.tr("Connection lost -- reconnecting (attempt {0}/{1})…").format(
                attempt, max_attempts
            )
        )

    def _on_session_reconnect_failed(self):
        self._mon_status_lbl.setText(
            self.tr("Offline — reconnect failed. Click Connect to try again.")
        )

    def _save_monitoring_session(self):
        from PySide6.QtWidgets import QInputDialog

        from app.services import data_service

        if not self.session.mon_buffer:
            return
        default_name = f"monitoring-{len(self.session.mon_buffer)}-samples"
        name, ok = QInputDialog.getText(
            self, self.tr("Save Session as Run"), self.tr("Name:"), text=default_name
        )
        if not ok or not name.strip():
            return
        try:
            result = data_service.save_monitoring_session(
                name.strip(),
                self.session.mon_buffer,
                chamber=self.session.chamber["name"],
                test_profile=self.session.session_test_profile_name,
            )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Save session failed"), str(exc))
            return
        self.session.clear_buffer()
        self._mon_save_session_btn.setEnabled(False)
        self._mon_recording_lbl.setText(self.tr("Not recording"))
        window = self.window()
        if hasattr(window, "render_runs"):
            window.runs = result["runs"]
            window.render_runs()
            window._refresh_dashboard()
            window._refresh_reports()
        QMessageBox.information(
            self,
            self.tr("Session saved"),
            self.tr("Saved as run '{0}'.").format(result["id"]),
        )

    # ── Live data / chart ────────────────────────────────────────────────────

    def _on_sample(self, sample):
        self._mon_update_lbl.setText(datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
        self._render_live_sample(sample)
        self._render_live_chart()

    def _render_live_sample(self, sample):
        keys = list(sample.keys())
        self._mon_live_table.setUpdatesEnabled(False)
        self._mon_live_table.setRowCount(len(keys))
        for i, key in enumerate(keys):
            value = sample.get(key)
            text = fmt(value) if isinstance(value, (int, float)) else str(value)
            self._mon_live_table.setItem(i, 0, QTableWidgetItem(str(key)))
            self._mon_live_table.setItem(i, 1, QTableWidgetItem(text))
        self._mon_live_table.resizeColumnsToContents()
        self._mon_live_table.setUpdatesEnabled(True)

    def _render_live_chart(self):
        window = self.session.mon_buffer[-_LIVE_CHART_WINDOW:]
        if not window:
            return
        has_timestamps = all(isinstance(s.get("timestamp"), (int, float)) for s in window)
        if has_timestamps:
            first_t = window[0]["timestamp"]
            xs = [s["timestamp"] - first_t for s in window]
        else:
            xs = list(range(len(window)))

        numeric_keys = sorted(
            {
                key
                for s in window
                for key, value in s.items()
                if isinstance(value, (int, float)) and key != "timestamp"
            }
        )
        for index, key in enumerate(numeric_keys):
            ys = [s.get(key) for s in window]
            plot_xs = [x for x, y in zip(xs, ys, strict=False) if isinstance(y, (int, float))]
            plot_ys = [y for y in ys if isinstance(y, (int, float))]
            if not plot_xs:
                continue
            color = COLORS[index % len(COLORS)]
            curve = self._mon_curves.get(key)
            if curve is None:
                curve = self._mon_plot_widget.plot(
                    plot_xs, plot_ys, pen=pg.mkPen(color, width=1.6), name=key
                )
                self._mon_curves[key] = curve
            else:
                curve.setData(plot_xs, plot_ys)

    # ── Alarms ───────────────────────────────────────────────────────────────

    def _toggle_alarm_form(self):
        self._alarm_form.setVisible(not self._alarm_form.isVisible())

    def _open_alarm_history(self):
        from app.alarm_history_dialog import AlarmHistoryDialog

        dlg = AlarmHistoryDialog(
            current_user=self.current_user, chamber_id=self.session.chamber["id"], parent=self
        )
        dlg.exec()

    def _save_alarm(self):
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
        try:
            self.session.add_alarm_rule(
                name,
                var,
                cond,
                value,
                value2,
                sev,
                deadband=deadband,
                delay_s=delay_s,
                created_by=self.current_user.get("name") or "Unknown",
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

    def _delete_alarm(self, idx):
        self.session.delete_alarm_rule(idx)

    def _refresh_alarms_table(self):
        cols = [
            self.tr("Name"),
            self.tr("Variable"),
            self.tr("Condition"),
            self.tr("Value / Range"),
            self.tr("Severity"),
            self.tr("Status"),
        ]
        cond_labels = {
            "above": self.tr("above"),
            "below": self.tr("below"),
            "outside range": self.tr("outside range"),
        }
        sev_labels = {
            "Info": self.tr("Info"),
            "Warning": self.tr("Warning"),
            "Critical": self.tr("Critical"),
        }
        tbl = self._alarms_table
        tbl.setUpdatesEnabled(False)
        tbl.setAlternatingRowColors(True)
        tbl.setShowGrid(False)
        tbl.setWordWrap(False)
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setColumnCount(len(cols) + 1)
        tbl.setRowCount(len(self.session.alarms))
        tbl.setHorizontalHeaderLabels(cols + [""])
        connected = self.session.is_connected()
        for ri, alarm in enumerate(self.session.alarms):
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
                cond_labels.get(alarm["condition"], alarm["condition"]),
                val_str,
                sev_labels.get(sev, sev),
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
