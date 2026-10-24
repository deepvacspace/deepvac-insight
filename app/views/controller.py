"""ControllerMixin — builds the Controller page: chamber connections and their
settings, a one-off Manual Setpoint sender, Test Profiles (multi-step
temperature/pressure schedules) and the chamber's alarm rules, all for the
chamber selected in this page's Chamber combo.

Each connected chamber runs its own manual setpoint / test profile
independently and concurrently -- the step-sequencer and all of that
running state live on app.services.chamber_session.ChamberSession, not on
this page, specifically so switching which chamber is selected here never
stops anything: a test profile keeps advancing on chamber A in the
background while this page is showing chamber B. This page is just a
read/write view onto whichever session is currently selected.

The chamber connections themselves (self._chamber_sessions) are shared
app-wide and owned by DeepVacDesktop (_connect_chamber/_disconnect_chamber
in main_window.py); this page's Connect / Disconnect button drives them.
Their live data is followed in Analysis' Live tab (app/live_tab.py).
Chambers with protocol "plc" are not connected here: the Live tab and the
Agent API reach them over the PLC gate.
"""

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import app.services.chambers_service as chambers_service
import app.services.test_profiles_service as test_profiles_service
from app.chamber_alarms import ChamberAlarmsPanel
from app.plc_control import PlcControlPanel
from app.services.chamber_session import MAX_CONNECTED_CHAMBERS


def _format_hms(seconds):
    seconds = int(max(0.0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class ControllerMixin:
    def _controller_view(self):
        container = QWidget()
        container.setObjectName("workspaceBody")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)

        hdr = QLabel(self.tr("Controller"))
        hdr.setObjectName("pageTitle")
        outer.addWidget(hdr)

        self._ctrl_chambers = {}  # saved chambers by id, see _refresh_controller_chamber_choices()
        self._ctrl_alarms = ChamberAlarmsPanel(current_user=self.current_user)
        self._ctrl_plc_panel = PlcControlPanel(self.plc_links, current_user=self.current_user)
        self._ctrl_plc_panel.setVisible(False)

        # ── Chamber: pick, connect, settings ─────────────────────────────────
        chamber_card = QFrame()
        chamber_card.setObjectName("card")
        ccl = QVBoxLayout(chamber_card)
        ccl.setContentsMargins(14, 14, 14, 14)
        ccl.setSpacing(10)
        chamber_hdr = QHBoxLayout()
        chamber_lbl = QLabel(self.tr("CHAMBER"))
        chamber_lbl.setObjectName("sectionLabel")
        chamber_hdr.addWidget(chamber_lbl)
        chamber_hdr.addStretch(1)
        self._ctrl_count_lbl = QLabel()
        self._ctrl_count_lbl.setObjectName("sectionLabel")
        chamber_hdr.addWidget(self._ctrl_count_lbl)
        ccl.addLayout(chamber_hdr)

        chamber_row = QHBoxLayout()
        chamber_row.setSpacing(8)
        self._ctrl_chamber_combo = QComboBox()
        self._ctrl_chamber_combo.setMinimumWidth(300)
        self._ctrl_chamber_combo.setToolTip(self.tr("Saved chambers. ● connected."))
        self._ctrl_chamber_combo.currentIndexChanged.connect(self._refresh_controller_page)
        chamber_row.addWidget(self._ctrl_chamber_combo)
        settings_btn = QPushButton(self.tr("Chamber Settings…"))
        settings_btn.setToolTip(self.tr("Add, edit or delete saved chambers"))
        settings_btn.clicked.connect(self._open_chambers_dialog)
        chamber_row.addWidget(settings_btn)
        self._ctrl_connect_btn = QPushButton(self.tr("Connect"))
        self._ctrl_connect_btn.setObjectName("primaryButton")
        self._ctrl_connect_btn.clicked.connect(self._on_ctrl_connect_clicked)
        chamber_row.addWidget(self._ctrl_connect_btn)
        chamber_row.addStretch(1)
        ccl.addLayout(chamber_row)

        status_row = QHBoxLayout()
        self._ctrl_chamber_dot = QLabel("●")
        self._ctrl_chamber_dot.setObjectName("chamberIconOff")
        status_row.addWidget(self._ctrl_chamber_dot)
        self._ctrl_chamber_status_lbl = QLabel()
        self._ctrl_chamber_status_lbl.setObjectName("statusText")
        self._ctrl_chamber_status_lbl.setWordWrap(True)
        status_row.addWidget(self._ctrl_chamber_status_lbl, 1)
        ccl.addLayout(status_row)
        outer.addWidget(chamber_card)

        # ── Manual Setpoint ──────────────────────────────────────────────────
        manual_card = self._ctrl_manual_card = QFrame()
        manual_card.setObjectName("card")
        mnl = QVBoxLayout(manual_card)
        mnl.setContentsMargins(14, 14, 14, 14)
        mnl.setSpacing(10)

        manual_hdr = QLabel(self.tr("MANUAL SETPOINT"))
        manual_hdr.setObjectName("sectionLabel")
        mnl.addWidget(manual_hdr)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel(self.tr("Temp (°C)")))
        self._manual_temp_ed = QLineEdit()
        self._manual_temp_ed.setPlaceholderText(self.tr("optional"))
        self._manual_temp_ed.setFixedWidth(90)
        manual_row.addWidget(self._manual_temp_ed)
        manual_row.addWidget(QLabel(self.tr("Pressure")))
        self._manual_pressure_ed = QLineEdit()
        self._manual_pressure_ed.setPlaceholderText(self.tr("optional"))
        self._manual_pressure_ed.setFixedWidth(90)
        manual_row.addWidget(self._manual_pressure_ed)
        self._manual_send_btn = QPushButton(self.tr("Start"))
        self._manual_send_btn.setObjectName("primaryButton")
        self._manual_send_btn.setEnabled(False)
        self._manual_send_btn.clicked.connect(self._on_manual_button_clicked)
        manual_row.addWidget(self._manual_send_btn)
        manual_row.addStretch(1)
        mnl.addLayout(manual_row)

        self._manual_status_lbl = QLabel(self.tr("Not running"))
        self._manual_status_lbl.setObjectName("statusText")
        self._manual_status_lbl.setWordWrap(True)
        mnl.addWidget(self._manual_status_lbl)

        outer.addWidget(manual_card)

        # ── Test Profiles ────────────────────────────────────────────────────
        profiles_card = self._ctrl_profiles_card = QFrame()
        profiles_card.setObjectName("card")
        pfl = QVBoxLayout(profiles_card)
        pfl.setContentsMargins(14, 14, 14, 14)
        pfl.setSpacing(10)

        profiles_hdr = QHBoxLayout()
        pflbl = QLabel(self.tr("TEST PROFILES"))
        pflbl.setObjectName("sectionLabel")
        profiles_hdr.addWidget(pflbl)
        profiles_hdr.addStretch(1)
        manage_profiles_btn = QPushButton(self.tr("Manage Test Profiles"))
        manage_profiles_btn.clicked.connect(self._open_test_profiles_dialog)
        profiles_hdr.addWidget(manage_profiles_btn)
        pfl.addLayout(profiles_hdr)

        picker_row = QHBoxLayout()
        self._test_profile_combo = QComboBox()
        self._test_profile_combo.setMinimumWidth(220)
        self._test_profile_combo.currentIndexChanged.connect(self._refresh_controller_page)
        picker_row.addWidget(self._test_profile_combo, 1)
        self._test_start_btn = QPushButton(self.tr("Start Test"))
        self._test_start_btn.setObjectName("primaryButton")
        self._test_start_btn.setEnabled(False)
        self._test_start_btn.clicked.connect(self._test_start)
        picker_row.addWidget(self._test_start_btn)
        self._test_stop_btn = QPushButton(self.tr("Stop Test"))
        self._test_stop_btn.setEnabled(False)
        self._test_stop_btn.clicked.connect(self._test_stop_clicked)
        picker_row.addWidget(self._test_stop_btn)
        pfl.addLayout(picker_row)

        self._test_status_lbl = QLabel(self.tr("Not running"))
        self._test_status_lbl.setObjectName("statusText")
        self._test_status_lbl.setWordWrap(True)
        pfl.addWidget(self._test_status_lbl)

        self._load_test_profiles()
        outer.addWidget(profiles_card)

        # ── Chamber PLC: manual setpoint and program control ─────────────────
        outer.addWidget(self._ctrl_plc_panel, 1)

        # ── Alarms (sample-feed chambers) ────────────────────────────────────
        outer.addWidget(self._ctrl_alarms, 1)

        # A cheap 1s tick keeps the elapsed-time labels moving even between
        # a session's own state-change signals (which only fire on actual
        # transitions plus its own once-a-second timer -- this just makes
        # sure *this* page redraws promptly whenever it's the visible one).
        self._ctrl_display_timer = QTimer(self)
        self._ctrl_display_timer.timeout.connect(self._refresh_controller_page)
        self._ctrl_display_timer.start(1000)

        self._refresh_controller_chamber_choices()
        return container

    # ── Chamber selection ────────────────────────────────────────────────────

    def _selected_controller_session(self):
        chamber_id = self._ctrl_chamber_combo.currentData()
        return self._chamber_sessions.get(chamber_id) if chamber_id is not None else None

    def _selected_controller_chamber(self):
        chamber_id = self._ctrl_chamber_combo.currentData()
        session = self._chamber_sessions.get(chamber_id)
        if session is not None:
            return session.chamber
        return self._ctrl_chambers.get(chamber_id)

    def _refresh_controller_chamber_choices(self):
        """Every saved chamber, connected ones marked ●. Called from main_window
        whenever a chamber connects/disconnects or the saved list changes."""
        previous_id = self._ctrl_chamber_combo.currentData()
        try:
            saved = chambers_service.list_chambers()
        except Exception:
            saved = []
        # connected chambers stay listed even if their saved entry went away
        self._ctrl_chambers = {c["id"]: c for c in saved}
        for chamber_id, session in self._chamber_sessions.items():
            self._ctrl_chambers.setdefault(chamber_id, session.chamber)
        self._ctrl_chamber_combo.blockSignals(True)
        self._ctrl_chamber_combo.clear()
        for chamber in self._ctrl_chambers.values():
            mark = "● " if chamber["id"] in self._chamber_sessions else ""
            kind = "PLC" if chamber.get("protocol") == "plc" else "JSON"
            self._ctrl_chamber_combo.addItem(
                f"{mark}{chamber['name']}  ({chamber['host']}:{chamber['port']} · {kind})",
                chamber["id"],
            )
        if self._ctrl_chamber_combo.count():
            # keep the selection; otherwise prefer a connected chamber
            fallback = next(
                (
                    i
                    for i in range(self._ctrl_chamber_combo.count())
                    if self._ctrl_chamber_combo.itemData(i) in self._chamber_sessions
                ),
                0,
            )
            idx = next(
                (
                    i
                    for i in range(self._ctrl_chamber_combo.count())
                    if self._ctrl_chamber_combo.itemData(i) == previous_id
                ),
                fallback,
            )
            self._ctrl_chamber_combo.setCurrentIndex(idx)
        self._ctrl_chamber_combo.blockSignals(False)
        self._ctrl_count_lbl.setText(
            self.tr("{0} / {1} connected").format(
                len(self._chamber_sessions), MAX_CONNECTED_CHAMBERS
            )
        )
        self._refresh_controller_page()

    def _open_chambers_dialog(self):
        from app.chambers_dialog import ChambersDialog

        dlg = ChambersDialog(parent=self)
        dlg.exec()
        if dlg.changed:
            self._refresh_chamber_ui()  # this combo, and the Live tab's chamber list

    def _on_ctrl_connect_clicked(self):
        chamber = self._selected_controller_chamber()
        if chamber is None:
            QMessageBox.warning(
                self, self.tr("Connect"), self.tr("Add a chamber first (Chamber Settings…).")
            )
            return
        session = self._chamber_sessions.get(chamber["id"])
        if session is None:
            try:
                self._connect_chamber(chamber)  # also refreshes this page
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
        self._disconnect_chamber(chamber["id"])

    def _save_session_as_run(self, session):
        """Saves a connected chamber's recorded samples as a run; False if that failed."""
        from datetime import datetime

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
        self._on_live_run_saved(result["key"])
        return True

    def _on_controller_session_changed(self, session):
        """Connected (in main_window._connect_chamber) to every session's
        manual_state_changed/test_state_changed -- only actually redraws
        when the emitting session is the one currently selected here."""
        if session is self._selected_controller_session():
            self._refresh_controller_page()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _refresh_controller_page(self):
        session = self._selected_controller_session()
        chamber = self._selected_controller_chamber()
        connected = session.is_connected() if session else False
        is_plc = chamber is not None and chamber.get("protocol") == "plc"
        self._ctrl_chamber_dot.setObjectName("chamberIconOn" if connected else "chamberIconOff")
        self._ctrl_chamber_dot.style().unpolish(self._ctrl_chamber_dot)
        self._ctrl_chamber_dot.style().polish(self._ctrl_chamber_dot)
        if chamber is None:
            status = self.tr("No saved chambers — add one with Chamber Settings….")
        elif is_plc:
            status = self.tr(
                "{0} is a chamber PLC: it connects by itself, is controlled below and followed in "
                "Analysis → Live. Agents' requests for it wait in Agent Requests."
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
        self._ctrl_chamber_status_lbl.setText(status)
        at_cap = len(self._chamber_sessions) >= MAX_CONNECTED_CHAMBERS
        self._ctrl_connect_btn.setText(self.tr("Disconnect") if session else self.tr("Connect"))
        self._ctrl_connect_btn.setEnabled(
            chamber is not None and not is_plc and (session is not None or not at_cap)
        )
        self._ctrl_connect_btn.setToolTip(
            self.tr("Up to {0} chambers can be connected at once.").format(MAX_CONNECTED_CHAMBERS)
            if at_cap and session is None
            else ""
        )
        alarms_target = None if is_plc else chamber
        if (self._ctrl_alarms.chamber, self._ctrl_alarms.session) != (alarms_target, session):
            self._ctrl_alarms.set_target(alarms_target, session)
        self._ctrl_alarms.setVisible(not is_plc)
        # a PLC has its own manual and program control; the cards above drive sample feeds
        self._ctrl_manual_card.setVisible(not is_plc)
        self._ctrl_profiles_card.setVisible(not is_plc)
        self._ctrl_plc_panel.setVisible(is_plc)
        if is_plc and self._ctrl_plc_panel.chamber != chamber:
            self._ctrl_plc_panel.set_chamber(chamber)
        self._update_manual_status_label(session)
        self._update_test_status_label(session)
        self._update_controller_buttons(session)

    def _update_manual_status_label(self, session):
        if session is None:
            self._manual_status_lbl.setText(self.tr("Not running"))
            return
        if session.manual_running:
            elapsed = time.monotonic() - session.manual_started_at
            self._manual_status_lbl.setText(self.tr("Running for {0}").format(_format_hms(elapsed)))
        elif session.last_manual_stop_error:
            self._manual_status_lbl.setText(
                self.tr("Stopped: {0}").format(session.last_manual_stop_error)
            )
        elif session.last_manual_elapsed_s is not None:
            self._manual_status_lbl.setText(
                self.tr("Stopped after {0}").format(_format_hms(session.last_manual_elapsed_s))
            )
        else:
            self._manual_status_lbl.setText(self.tr("Not running"))

    def _update_test_status_label(self, session):
        if session is None or session.test_running_profile is None:
            if session and session.last_test_profile_name:
                if session.last_test_finished:
                    self._test_status_lbl.setText(
                        self.tr("Test '{0}' complete.").format(session.last_test_profile_name)
                    )
                elif session.last_test_stop_error:
                    self._test_status_lbl.setText(
                        self.tr("Test stopped: {0}").format(session.last_test_stop_error)
                    )
                else:
                    self._test_status_lbl.setText(
                        self.tr("Test '{0}' stopped.").format(session.last_test_profile_name)
                    )
            else:
                self._test_status_lbl.setText(self.tr("Not running"))
            return
        profile = session.test_running_profile
        step = profile["steps"][session.test_step_index]
        elapsed = time.monotonic() - session.test_step_started_at
        remaining = max(0.0, step["duration_s"] - elapsed)
        parts = []
        if step["setpoint_temp"] is not None:
            parts.append(f"T={step['setpoint_temp']:g}")
        if step["setpoint_pressure"] is not None:
            parts.append(f"P={step['setpoint_pressure']:g}")
        setpoint_str = ", ".join(parts)
        label = step["label"] or self.tr("Step {0}").format(session.test_step_index + 1)
        self._test_status_lbl.setText(
            self.tr("Running '{0}' — step {1}/{2}: {3} ({4}) — {5:.0f}s remaining").format(
                profile["name"],
                session.test_step_index + 1,
                len(profile["steps"]),
                label,
                setpoint_str,
                remaining,
            )
        )

    def _update_controller_buttons(self, session):
        connected = session.is_connected() if session else False
        manual_running = session.manual_running if session else False
        no_test_running = session.test_running_profile is None if session else True
        self._manual_send_btn.setText(self.tr("Stop") if manual_running else self.tr("Start"))
        can_start_test = (
            connected
            and self._test_profile_combo.count() > 0
            and self._test_profile_combo.currentData() is not None
            and bool(self._test_profile_combo.currentData()["steps"])
            and no_test_running
            and not manual_running
        )
        self._test_start_btn.setEnabled(can_start_test)
        # Stop must always be clickable while a test is running (even if the
        # chamber just dropped), so the user is never stuck with a
        # "running" test they can't get out of.
        self._test_stop_btn.setEnabled(not no_test_running)
        self._test_profile_combo.setEnabled(no_test_running)
        self._manual_send_btn.setEnabled(
            bool(session) and (manual_running or (connected and no_test_running))
        )
        self._manual_temp_ed.setEnabled(not manual_running)
        self._manual_pressure_ed.setEnabled(not manual_running)

    # ── Manual Setpoint ──────────────────────────────────────────────────────

    def _on_manual_button_clicked(self):
        session = self._selected_controller_session()
        if session is None:
            return
        if session.manual_running:
            session.stop_manual_setpoint()
        else:
            self._start_manual_setpoint(session)
        self._refresh_controller_page()

    def _start_manual_setpoint(self, session):
        temp_text = self._manual_temp_ed.text().strip()
        pressure_text = self._manual_pressure_ed.text().strip()
        if not temp_text and not pressure_text:
            QMessageBox.warning(
                self, self.tr("Start"), self.tr("Enter a temperature and/or pressure.")
            )
            return
        try:
            temp = float(temp_text) if temp_text else None
            pressure = float(pressure_text) if pressure_text else None
        except ValueError:
            QMessageBox.warning(
                self, self.tr("Start"), self.tr("Temperature/pressure must be numbers.")
            )
            return
        try:
            session.start_manual_setpoint(temp, pressure)
        except RuntimeError as exc:
            QMessageBox.warning(self, self.tr("Start"), str(exc))

    # ── Test Profiles ────────────────────────────────────────────────────────

    def _load_test_profiles(self):
        previous = (
            self._test_profile_combo.currentData() if self._test_profile_combo.count() else None
        )
        self._test_profile_combo.blockSignals(True)
        self._test_profile_combo.clear()
        try:
            profiles = test_profiles_service.list_profiles()
        except Exception:
            profiles = []
        for profile in profiles:
            n = len(profile["steps"])
            self._test_profile_combo.addItem(f"{profile['name']}  ({n} step(s))", profile)
        if previous is not None:
            idx = next(
                (
                    i
                    for i in range(self._test_profile_combo.count())
                    if self._test_profile_combo.itemData(i)["id"] == previous["id"]
                ),
                0,
            )
            self._test_profile_combo.setCurrentIndex(idx)
        self._test_profile_combo.blockSignals(False)
        self._refresh_controller_page()

    def _open_test_profiles_dialog(self):
        from app.test_profiles_dialog import TestProfilesDialog

        dlg = TestProfilesDialog(current_user=self.current_user, parent=self)
        dlg.exec()
        if dlg.changed:
            self._load_test_profiles()

    def _test_start(self):
        session = self._selected_controller_session()
        if session is None:
            return
        profile = self._test_profile_combo.currentData()
        if not profile or not profile["steps"]:
            return
        try:
            session.start_test(profile)
        except RuntimeError as exc:
            QMessageBox.warning(self, self.tr("Start Test"), str(exc))
            return
        self._refresh_controller_page()

    def _test_stop_clicked(self):
        session = self._selected_controller_session()
        if session is None:
            return
        session.stop_test()
        self._refresh_controller_page()
