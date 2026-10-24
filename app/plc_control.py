"""PlcControlPanel — operator control of a chamber PLC from the Controller page,
like control2-client's manual and program control.

- Manual setpoint: a temperature, optionally ramped at a rate and held for a
  time (otherwise held until stopped).
- Program: a test profile run as a PLC program (app/services/plc_programs.py),
  with Next interval.
- Stop, and the PLC's confirmations: job finished, alarm.

Commands go straight to the PLC over the PLC gate: the person at this screen
is the one deciding. (Agents never come through here; their requests wait in
Agent Requests.)
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api import control_settings
from app.common import fmt
from app.services import plc_gate, plc_programs, test_profiles_service


def _duration(seconds):
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class PlcControlPanel(QWidget):
    def __init__(self, links, current_user=None, parent=None):
        super().__init__(parent)
        self.links = links
        self.current_user = current_user or {"name": "Unknown"}
        self.chamber = None
        self._build_ui()
        self.load_profiles()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _card(self, title):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("sectionLabel")
        lay.addWidget(label)
        return card, lay

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        # status + always-available actions
        status_card, sl = self._card(self.tr("CHAMBER PLC"))
        row = QHBoxLayout()
        self.status_lbl = QLabel("—")
        self.status_lbl.setObjectName("statusText")
        self.status_lbl.setWordWrap(True)
        row.addWidget(self.status_lbl, 1)
        self.next_btn = QPushButton(self.tr("Next Interval"))
        self.next_btn.setToolTip(self.tr("End the current program interval and go to the next one"))
        self.next_btn.clicked.connect(self._next_interval)
        self.stop_btn = QPushButton(self.tr("Stop"))
        self.stop_btn.clicked.connect(self._stop)
        self.finish_btn = QPushButton(self.tr("Confirm Finish"))
        self.finish_btn.setObjectName("primaryButton")
        self.finish_btn.setToolTip(
            self.tr("The job finished: acknowledge it so the chamber returns to idle")
        )
        self.finish_btn.clicked.connect(lambda: self._command("confirm_finish"))
        self.alarm_btn = QPushButton(self.tr("Confirm Alarm"))
        self.alarm_btn.clicked.connect(lambda: self._command("confirm_alarm"))
        for btn in (self.next_btn, self.stop_btn, self.finish_btn, self.alarm_btn):
            row.addWidget(btn)
        sl.addLayout(row)
        root.addWidget(status_card)

        # manual setpoint
        manual_card, ml = self._card(self.tr("MANUAL SETPOINT"))
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel(self.tr("Temperature")))
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setDecimals(1)
        self.temp_spin.setSuffix(" °C")
        self.temp_spin.setFixedWidth(110)
        mrow.addWidget(self.temp_spin)
        self.ramp_check = QCheckBox(self.tr("Ramp at"))
        self.ramp_spin = QDoubleSpinBox()
        self.ramp_spin.setRange(0.1, 20.0)
        self.ramp_spin.setValue(2.0)
        self.ramp_spin.setSuffix(" °C/min")
        self.ramp_spin.setFixedWidth(120)
        self.ramp_check.toggled.connect(self.ramp_spin.setEnabled)
        self.ramp_spin.setEnabled(False)
        mrow.addWidget(self.ramp_check)
        mrow.addWidget(self.ramp_spin)
        self.hold_check = QCheckBox(self.tr("Hold for"))
        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(1, 7 * 24 * 60)
        self.hold_spin.setDecimals(0)
        self.hold_spin.setValue(30)
        self.hold_spin.setSuffix(" min")
        self.hold_spin.setFixedWidth(110)
        self.hold_check.toggled.connect(self.hold_spin.setEnabled)
        self.hold_spin.setEnabled(False)
        self.hold_check.setToolTip(self.tr("Otherwise the setpoint is held until stopped"))
        mrow.addWidget(self.hold_check)
        mrow.addWidget(self.hold_spin)
        self.manual_btn = QPushButton(self.tr("Start"))
        self.manual_btn.setObjectName("primaryButton")
        self.manual_btn.clicked.connect(self._start_manual)
        mrow.addWidget(self.manual_btn)
        mrow.addStretch(1)
        ml.addLayout(mrow)
        root.addWidget(manual_card)

        # program
        program_card, pl = self._card(self.tr("PROGRAM"))
        prow = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(260)
        self.profile_combo.currentIndexChanged.connect(self._show_program)
        prow.addWidget(self.profile_combo, 1)
        manage_btn = QPushButton(self.tr("Manage Test Profiles"))
        manage_btn.clicked.connect(self._manage_profiles)
        prow.addWidget(manage_btn)
        self.program_btn = QPushButton(self.tr("Start Program"))
        self.program_btn.setObjectName("primaryButton")
        self.program_btn.clicked.connect(self._start_program)
        prow.addWidget(self.program_btn)
        pl.addLayout(prow)
        self.program_info = QLabel()
        self.program_info.setObjectName("statusText")
        self.program_info.setWordWrap(True)
        pl.addWidget(self.program_info)
        self.intervals_table = QTableWidget()
        self.intervals_table.setColumnCount(4)
        self.intervals_table.setHorizontalHeaderLabels(
            [self.tr("#"), self.tr("Mode"), self.tr("Target"), self.tr("Duration")]
        )
        self.intervals_table.verticalHeader().setVisible(False)
        self.intervals_table.horizontalHeader().setStretchLastSection(True)
        self.intervals_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.intervals_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.intervals_table.setAlternatingRowColors(True)
        self.intervals_table.setMinimumHeight(140)
        pl.addWidget(self.intervals_table, 1)
        root.addWidget(program_card, 1)

    # ── target ───────────────────────────────────────────────────────────────

    def set_chamber(self, chamber):
        self.chamber = chamber
        settings = control_settings.load()
        low, high = control_settings.temperature_limits(settings)
        self.temp_spin.setRange(low, high)
        if chamber is not None:
            self._client()  # starts connecting in the background
        self.refresh()

    def _client(self):
        return self.links.client(self.chamber, wait=False) if self.chamber else None

    def load_profiles(self):
        previous = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        try:
            profiles = test_profiles_service.list_profiles()
        except Exception:
            profiles = []
        for profile in profiles:
            kind = (
                self.tr("PLC program")
                if profile.get("program")
                else self.tr("{0} step(s)").format(len(profile["steps"]))
            )
            self.profile_combo.addItem(f"{profile['name']}  ({kind})", profile)
        if previous is not None:
            index = next(
                (
                    i
                    for i in range(self.profile_combo.count())
                    if self.profile_combo.itemData(i)["id"] == previous["id"]
                ),
                0,
            )
            self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self._show_program()

    def _manage_profiles(self):
        from app.test_profiles_dialog import TestProfilesDialog

        dlg = TestProfilesDialog(current_user=self.current_user, parent=self)
        dlg.exec()
        if dlg.changed:
            self.load_profiles()

    def _program(self):
        """(intervals, cycle) of the selected profile; raises ProgramError."""
        profile = self.profile_combo.currentData()
        if profile is None:
            raise plc_programs.ProgramError(self.tr("No test profile selected."))
        return plc_programs.program_for_profile(
            profile, control_settings.enable_flags(control_settings.load())
        )

    def _show_program(self, *_):
        table = self.intervals_table
        try:
            intervals, cycle = self._program()
        except plc_programs.ProgramError as exc:
            table.setRowCount(0)
            self.program_info.setText(str(exc))
            return
        mode_names = {
            plc_programs.RAMP: self.tr("Ramp to"),
            plc_programs.HOLD: self.tr("Go to and hold"),
        }
        table.setRowCount(len(intervals))
        for row, interval in enumerate(intervals):
            values = [
                str(row + 1),
                mode_names.get(plc_programs.interval_mode(interval), "—"),
                f"{fmt(interval.get('temp'), 1)} °C",
                _duration(interval.get("duration")),
            ]
            for col, text in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(text))
        table.resizeColumnsToContents()
        text = self.tr("{0} interval(s) · {1}").format(
            len(intervals), _duration(plc_programs.total_duration_s(intervals, cycle))
        )
        self.refresh()  # Start Program depends on there being intervals
        if int(cycle.get("count", 0)) > 1:
            text += self.tr(" · intervals {0}–{1} repeated {2}×").format(
                cycle.get("start"), cycle.get("end"), cycle.get("count")
            )
        self.program_info.setText(text)

    # ── state ────────────────────────────────────────────────────────────────

    def refresh(self):
        client = self._client()
        if client is None or not self.isVisible():
            if client is None:
                self.status_lbl.setText(self.tr("Select a chamber PLC."))
            return
        snap = client.snapshot()
        connected = snap.connected and bool(snap.values)
        v = snap.values
        status = snap.status
        if not connected:
            reason = f": {snap.last_error}" if snap.last_error else "…"
            self.status_lbl.setText(self.tr("Connecting to {0}{1}").format(snap.address, reason))
        else:
            parts = [
                status.upper(),
                self.tr("{0} °C → setpoint {1} °C").format(
                    fmt(v.get("temp"), 2), fmt(v.get("temp_ref"), 2)
                ),
                self.tr("output {0} %").format(fmt(v.get("temp_u"), 1)),
            ]
            if status == "work" and v.get("interval"):
                parts.append(
                    self.tr("interval {0}/{1} ({2})").format(
                        int(v["interval"]),
                        int(v.get("intervals_count", 0)),
                        _duration(v.get("t_interval")),
                    )
                )
                if v.get("cycle_total"):
                    parts.append(
                        self.tr("cycle {0}/{1}").format(
                            int(v.get("cycle_cur", 0)), int(v["cycle_total"])
                        )
                    )
                parts.append(self.tr("job {0}").format(_duration(v.get("t_total"))))
            if snap.alarm_code:
                labels = control_settings.alarm_labels(control_settings.load())
                active = [
                    labels.get(b, f"bit {b}") for b in range(32) if snap.alarm_code & (1 << b)
                ]
                parts.append(self.tr("ALARM: {0}").format(", ".join(active)))
            self.status_lbl.setText(" · ".join(parts))
        working = connected and status == "work"
        program_running = working and int(v.get("intervals_count", 0) or 0) > 1
        self.next_btn.setEnabled(program_running)
        self.stop_btn.setEnabled(working)
        self.finish_btn.setVisible(connected and status == "finish")
        self.alarm_btn.setVisible(connected and (status == "alarm" or bool(snap.alarm_code)))
        self.manual_btn.setEnabled(connected and status != "alarm")
        self.program_btn.setEnabled(
            connected and status != "alarm" and self.intervals_table.rowCount() > 0
        )

    # ── commands ─────────────────────────────────────────────────────────────

    def _send(self, action, describe):
        client = self._client()
        if client is None:
            return False
        try:
            action(client)
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self, self.tr("Chamber PLC"), str(exc))
            return False
        self.status_lbl.setText(describe)
        QTimer.singleShot(300, self.refresh)
        return True

    def _command(self, command):
        self._send(lambda c: c.send_text_cmd(command), self.tr("Sent: {0}").format(command))

    def _replaces_running_job(self):
        client = self._client()
        if client is None or client.snapshot().status != "work":
            return True
        answer = QMessageBox.question(
            self,
            self.tr("Chamber PLC"),
            self.tr("A job is running on {0}. Replace it?").format(self.chamber["name"]),
        )
        return answer == QMessageBox.Yes

    def _start_manual(self):
        if not self._replaces_running_job():
            return
        client = self._client()
        current = client.snapshot().values.get("temp") if client else None
        settings = control_settings.load()
        target = self.temp_spin.value()
        try:
            intervals = plc_gate.manual_setpoint_intervals(
                target,
                current,
                control_settings.enable_flags(settings),
                self.ramp_spin.value() if self.ramp_check.isChecked() else None,
                self.hold_spin.value() * 60.0 if self.hold_check.isChecked() else None,
            )
        except ValueError as exc:
            QMessageBox.warning(self, self.tr("Manual Setpoint"), str(exc))
            return
        params = control_settings.control_params(settings)
        self._send(
            lambda c: c.send_job(plc_gate.PROG_TYPE_SETPOINT, intervals, params),
            self.tr("Manual setpoint {0} °C sent").format(fmt(target, 1)),
        )

    def _start_program(self):
        try:
            intervals, cycle = self._program()
            low, high = control_settings.temperature_limits(control_settings.load())
            plc_programs.check_limits(intervals, low, high)
        except plc_programs.ProgramError as exc:
            QMessageBox.warning(self, self.tr("Program"), str(exc))
            return
        if not self._replaces_running_job():
            return
        params = control_settings.control_params(control_settings.load())
        profile = self.profile_combo.currentData()
        self._send(
            lambda c: c.send_job(plc_gate.PROG_TYPE_PROG, intervals, params, cycle),
            self.tr("Program '{0}' sent ({1} intervals)").format(profile["name"], len(intervals)),
        )

    def _next_interval(self):
        self._command("next")

    def _stop(self):
        answer = QMessageBox.question(
            self,
            self.tr("Stop"),
            self.tr("Stop the job running on {0}?").format(
                self.chamber["name"] if self.chamber else ""
            ),
        )
        if answer == QMessageBox.Yes:
            self._command("stop")
