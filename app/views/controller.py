"""ControllerMixin — builds the Controller page: a one-off Manual Setpoint
sender and Test Profiles (multi-step temperature/pressure schedules), both
targeting whichever connected chamber is selected in this page's own
Chamber combo.

Each connected chamber runs its own manual setpoint / test profile
independently and concurrently -- the step-sequencer and all of that
running state live on app.services.chamber_session.ChamberSession, not on
this page, specifically so switching which chamber is selected here never
stops anything: a test profile keeps advancing on chamber A in the
background while this page is showing chamber B. This page is just a
read/write view onto whichever session is currently selected.

The chamber connections themselves (self._chamber_sessions) are shared
app-wide and owned by DeepVacDesktop (_connect_chamber/_disconnect_chamber
in main_window.py) -- Live Monitoring (views/monitoring.py) is what
creates/destroys them; this page only looks them up.
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

import app.services.test_profiles_service as test_profiles_service


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

        chamber_row = QHBoxLayout()
        chamber_lbl = QLabel(self.tr("Chamber"))
        chamber_lbl.setObjectName("sectionLabel")
        chamber_row.addWidget(chamber_lbl)
        self._ctrl_chamber_combo = QComboBox()
        self._ctrl_chamber_combo.setMinimumWidth(200)
        self._ctrl_chamber_combo.currentIndexChanged.connect(self._refresh_controller_page)
        chamber_row.addWidget(self._ctrl_chamber_combo)
        chamber_row.addStretch(1)
        self._ctrl_chamber_dot = QLabel("●")
        self._ctrl_chamber_dot.setObjectName("chamberIconOff")
        chamber_row.addWidget(self._ctrl_chamber_dot)
        self._ctrl_chamber_status_lbl = QLabel(
            self.tr("No chambers connected — connect one in Live Monitoring.")
        )
        self._ctrl_chamber_status_lbl.setObjectName("statusText")
        chamber_row.addWidget(self._ctrl_chamber_status_lbl)
        outer.addLayout(chamber_row)

        # ── Manual Setpoint ──────────────────────────────────────────────────
        manual_card = QFrame()
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
        profiles_card = QFrame()
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
        outer.addStretch(1)

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

    def _refresh_controller_chamber_choices(self):
        """Called from main_window whenever a chamber connects/disconnects
        (see monitoring.py's _on_mon_connect_new/_on_monitor_tab_closed) so
        this page's Chamber combo always reflects what's actually open in
        Live Monitoring."""
        previous_id = self._ctrl_chamber_combo.currentData()
        self._ctrl_chamber_combo.blockSignals(True)
        self._ctrl_chamber_combo.clear()
        for session in self._chamber_sessions.values():
            self._ctrl_chamber_combo.addItem(session.chamber["name"], session.chamber["id"])
        if self._ctrl_chamber_combo.count():
            idx = next(
                (
                    i
                    for i in range(self._ctrl_chamber_combo.count())
                    if self._ctrl_chamber_combo.itemData(i) == previous_id
                ),
                0,
            )
            self._ctrl_chamber_combo.setCurrentIndex(idx)
        self._ctrl_chamber_combo.blockSignals(False)
        self._refresh_controller_page()

    def _on_controller_session_changed(self, session):
        """Connected (in main_window._connect_chamber) to every session's
        manual_state_changed/test_state_changed -- only actually redraws
        when the emitting session is the one currently selected here."""
        if session is self._selected_controller_session():
            self._refresh_controller_page()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _refresh_controller_page(self):
        session = self._selected_controller_session()
        connected = session.is_connected() if session else False
        self._ctrl_chamber_dot.setObjectName("chamberIconOn" if connected else "chamberIconOff")
        self._ctrl_chamber_dot.style().unpolish(self._ctrl_chamber_dot)
        self._ctrl_chamber_dot.style().polish(self._ctrl_chamber_dot)
        if session:
            chamber = session.chamber
            text = (
                self.tr("Connected — {0} ({1}:{2})")
                if connected
                else self.tr("Offline — {0} ({1}:{2})")
            )
            self._ctrl_chamber_status_lbl.setText(
                text.format(chamber["name"], chamber["host"], chamber["port"])
            )
        else:
            self._ctrl_chamber_status_lbl.setText(
                self.tr("No chambers connected — connect one in Live Monitoring.")
            )
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
