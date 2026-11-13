"""The Controller page."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.api import control_settings
from app.program_editor import (
    CycleWidget,
    DelayCountdownDialog,
    DelaySelectDialog,
    ElidedLabel,
    IntervalCardList,
    ProgramStepper,
    RunDetailsPanel,
    bold,
    cycle_text,
    duration_text,
    muted,
)
from app.program_graph import ProgramGraph
from app.program_library import ProgramLibraryDialog, ProgramSaveDialog, load_into_model
from app.program_model import (
    HOLD_UNTIL_STOPPED,
    INFINITE_CYCLES,
    PROG_PROGRAM,
    PROG_SETPOINT,
    ProgramModel,
)
from app.services import chambers_service, experiment_recorder, plc_gate

log = logging.getLogger(__name__)

STEP_DETAILS, STEP_INTERVALS, STEP_START = 0, 1, 2
FRESH_S = 15.0


def default_program(params: list[dict], settings: dict) -> dict:
    """Returns the chamber's default program."""
    default = dict(settings.get("default_program") or {})
    interval = {p["name"]: default.get(p["name"], p.get("value", 0)) for p in params}
    if "flags" not in default:
        temp = next((i for i, p in enumerate(params) if p.get("name") == "temp"), -1)
        flags = control_settings.enable_flags(settings) | (1 << plc_gate.RAMP_BIT)
        if temp >= 0:
            flags |= 1 << temp
        interval["flags"] = flags
    if "duration" not in default:
        interval["duration"] = 3600
    return {"intervals": [interval], "cycle": {"start": 0, "end": 0, "count": 0}}


class ControllerPage(QWidget):
    job_sent = Signal(str)

    def __init__(self, links, current_user=None, recorder=None, parent=None):
        super().__init__(parent)
        self.setObjectName("controllerPage")
        self.links = links
        self.current_user = current_user or {"name": "Unknown"}
        self.recorder = recorder
        self.chamber = None
        self._snap = None
        self._settings = control_settings.load()
        self._params = control_settings.control_params(self._settings)
        self.model = ProgramModel(self._params, self)
        self.model.set_program(default_program(self._params, self._settings))

        self._library_id = None
        self._library_name = ""
        self._labels: list[str] = []
        self._library_modified = False
        self._loading_library = False
        self._running_name = ""
        self._running_labels: list[str] = []
        self._working = False
        self._delay_dialog = None

        self._build_ui()
        self._set_model(self.model)
        self.reload_chambers()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.start(1000)
        self._summary_timer = QTimer(self)
        self._summary_timer.timeout.connect(self._update_sequence_info)
        self._summary_timer.start(30_000)
        self._update_program_label()
        self._update_sequence_info()
        self.poll()

    def _card(self, lay):
        card = QFrame(self)
        card.setObjectName("pageCard")
        card.setLayout(lay)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)
        return card

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        self.stepper = ProgramStepper(self)
        root.addWidget(self.stepper)
        column = QVBoxLayout()
        column.setSpacing(10)
        root.addLayout(column, 1)

        graph_lay = QVBoxLayout()
        graph_card = self._card(graph_lay)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.program_label = bold(ElidedLabel(self))
        self.program_label.setObjectName("programName")
        self.library_badge = QLabel(self)
        self.library_badge.setObjectName("libraryBadge")
        self.summary_label = muted("", self)
        title_row.addWidget(self.program_label)
        title_row.addWidget(self.library_badge)
        title_row.addSpacing(6)
        title_row.addWidget(self.summary_label)
        title_row.addStretch()
        title_row.addWidget(muted(self.tr("Chamber"), self))
        self.chamber_combo = QComboBox(self)
        self.chamber_combo.setMinimumWidth(200)
        self.chamber_combo.setToolTip(
            self.tr("The chamber PLC this page controls (Settings → Chamber Connections)")
        )
        self.chamber_combo.currentIndexChanged.connect(self._chamber_chosen)
        title_row.addWidget(self.chamber_combo)
        self.chamber_state = QLabel("●", self)
        self.chamber_state.setObjectName("chamberIconOff")
        title_row.addWidget(self.chamber_state)
        graph_lay.addLayout(title_row)
        self.graph = ProgramGraph(graph_card)
        graph_lay.addWidget(self.graph, 1)
        self.run_bar = muted("", self)
        self.run_bar.setObjectName("runBar")
        self.run_bar.setWordWrap(True)
        graph_lay.addWidget(self.run_bar)
        column.addWidget(graph_card, 1)

        details_page = QWidget(self)
        details_lay = QVBoxLayout(details_page)
        details_lay.setContentsMargins(0, 0, 0, 0)
        self.details = RunDetailsPanel(self.current_user.get("name") or "", details_page)
        details_lay.addWidget(self.details)
        self.stepper.add_step(self.tr("Experiment details"), details_page)

        intervals_page = QWidget(self)
        intervals_lay = QVBoxLayout(intervals_page)
        intervals_lay.setContentsMargins(0, 0, 0, 0)
        intervals_lay.setSpacing(8)
        library_row = QHBoxLayout()
        library_row.setSpacing(8)
        self.load_btn = QPushButton(self.tr("Load from library"), intervals_page)
        self.load_btn.setToolTip(
            self.tr(
                "Load a program from the program library (also those shared by paired computers)"
            )
        )
        self.save_btn = QPushButton(self.tr("Save to library"), intervals_page)
        self.save_btn.setToolTip(self.tr("Save this program in the program library, by name"))
        for button in (self.load_btn, self.save_btn):
            library_row.addWidget(button, 1)
        intervals_lay.addLayout(library_row)
        self.interval_list = IntervalCardList(intervals_page)
        intervals_lay.addWidget(self.interval_list)
        cycle_box = QFrame(intervals_page)
        cycle_box.setObjectName("pageCard")
        cycle_lay = QVBoxLayout(cycle_box)
        cycle_lay.setContentsMargins(12, 8, 12, 10)
        cycle_lay.setSpacing(4)
        cycle_lay.addWidget(bold(QLabel(self.tr("Cycle"), cycle_box)))
        note = muted(self.tr("Repeat a range of intervals a number of times."), cycle_box)
        note.setWordWrap(True)
        cycle_lay.addWidget(note)
        self.cycle_widget = CycleWidget(cycle_box)
        cycle_lay.addWidget(self.cycle_widget)
        intervals_lay.addWidget(cycle_box)
        self.stepper.add_step(self.tr("Intervals"), intervals_page)
        self.add_btn = QPushButton(self.tr("Add"), self)
        self.add_btn.setObjectName("primaryButton")
        self.add_btn.setToolTip(self.tr("A new interval after the selected one"))
        self.stepper.set_header_widget(STEP_INTERVALS, self.add_btn)

        start_page = QWidget(self)
        start_lay = QVBoxLayout(start_page)
        start_lay.setContentsMargins(0, 0, 0, 0)
        start_lay.setSpacing(10)
        facts = QGridLayout()
        facts.setHorizontalSpacing(12)
        facts.setVerticalSpacing(6)
        self.facts = {}
        for row, (key, title) in enumerate(
            (
                ("purpose", self.tr("Name or purpose")),
                ("dut", self.tr("Device under test")),
                ("operator", self.tr("Operator")),
                ("program", self.tr("Program")),
                ("cycle", self.tr("Cycle")),
                ("duration", self.tr("Duration")),
            )
        ):
            value = bold(QLabel(start_page))
            value.setWordWrap(True)
            facts.addWidget(muted(title, start_page), row, 0)
            facts.addWidget(value, row, 1)
            self.facts[key] = value
        facts.setColumnStretch(1, 1)
        start_lay.addLayout(facts)
        self.run_status = QLabel(start_page)
        self.run_status.setObjectName("runStatus")
        self.run_status.setWordWrap(True)
        start_lay.addWidget(self.run_status)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_btn = QPushButton(self.tr("Start"), start_page)
        self.start_btn.setObjectName("startButton")
        self.delay_btn = QPushButton(self.tr("Delay start"), start_page)
        self.stop_btn = QPushButton(self.tr("Stop"), start_page)
        self.stop_btn.setObjectName("stopButton")
        for button in (self.start_btn, self.delay_btn, self.stop_btn):
            button.setMinimumHeight(36)
            font = button.font()
            font.setBold(True)
            button.setFont(font)
            buttons.addWidget(button, 1)
        start_lay.addLayout(buttons)
        extra = QHBoxLayout()
        extra.setSpacing(8)
        self.next_btn = QPushButton(self.tr("Next interval"), start_page)
        self.next_btn.setToolTip(self.tr("End the current program interval and go to the next one"))
        self.finish_btn = QPushButton(self.tr("Confirm finish"), start_page)
        self.finish_btn.setObjectName("primaryButton")
        self.finish_btn.setToolTip(
            self.tr("The job finished: acknowledge it so the chamber returns to idle")
        )
        self.alarm_btn = QPushButton(self.tr("Confirm alarm"), start_page)
        for button in (self.next_btn, self.finish_btn, self.alarm_btn):
            extra.addWidget(button, 1)
        start_lay.addLayout(extra)
        self.stepper.add_step(self.tr("Confirm and start"), start_page)

        self.details.edited.connect(self._details_edited)
        self.stepper.next_requested.connect(self._next_requested)
        self.add_btn.clicked.connect(self._add_interval)
        self.interval_list.duplicate_requested.connect(self._duplicate_interval)
        self.interval_list.delete_requested.connect(self._delete_interval)
        self.interval_list.hold_requested.connect(self._hold_interval)
        self.interval_list.label_edited.connect(self._label_edited)
        self.graph.interval_double_clicked.connect(self._open_interval)
        self.load_btn.clicked.connect(self.open_library)
        self.save_btn.clicked.connect(self.save_to_library)
        self.start_btn.clicked.connect(self.start_now)
        self.delay_btn.clicked.connect(self.delay_start)
        self.stop_btn.clicked.connect(self.stop_job)
        self.next_btn.clicked.connect(lambda: self._command("next"))
        self.finish_btn.clicked.connect(lambda: self._command("confirm_finish"))
        self.alarm_btn.clicked.connect(lambda: self._command("confirm_alarm"))
        self._details_edited()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        spacing = self.layout().spacing() if self.layout() else 0
        margins = self.layout().contentsMargins()
        width = event.size().width() - margins.left() - margins.right() - spacing
        self.stepper.setFixedWidth(max(320, width // 3))

    def showEvent(self, event):
        super().showEvent(event)
        self.details.fill_default_name()
        self.poll()

    def update_theme(self, dark):
        self.graph.update_theme(dark)
        self._update_program_label()

    def _set_model(self, model):
        self.interval_list.set_model(model)
        pressure = control_settings.state_meta(self._settings).get("pres")
        if pressure:
            label = pressure["label"] + (f", {pressure['unit']}" if pressure["unit"] else "")
            self.graph.set_pressure_label(label)
            self.interval_list.set_pressure_label(label)
        self.graph.set_model(model)
        self.cycle_widget.set_params(model.cycle())
        self.cycle_widget.set_intervals_count(model.count())
        model.number_of_intervals_changed.connect(self._intervals_changed)
        model.cycle_params_updated.connect(self.cycle_widget.set_params)
        model.cycle_params_updated.connect(self._mark_modified)
        model.model_changed.connect(self._mark_modified)
        model.model_changed.connect(self._update_sequence_info)
        self.cycle_widget.params_changed.connect(self._cycle_edited)

    def reload_chambers(self):
        """Reloads the chamber PLCs."""
        previous = self.chamber["id"] if self.chamber else None
        try:
            plcs = [c for c in chambers_service.list_chambers() if c.get("protocol") == "plc"]
        except Exception:
            plcs = []
        self.chamber_combo.blockSignals(True)
        self.chamber_combo.clear()
        for chamber in plcs:
            self.chamber_combo.addItem(chamber["name"], chamber)
        if not plcs:
            self.chamber_combo.addItem(self.tr("No chamber PLC"), None)
        index = next((i for i, c in enumerate(plcs) if c["id"] == previous), 0)
        self.chamber_combo.setCurrentIndex(index)
        self.chamber_combo.setEnabled(bool(plcs))
        self.chamber_combo.blockSignals(False)
        self._chamber_chosen()

    def _chamber_chosen(self, *_):
        self.chamber = self.chamber_combo.currentData()
        if self.chamber is not None:
            try:
                self.links.client(self.chamber, wait=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("chamber %s: %s", self.chamber.get("name"), exc)
        self._snap = None
        self.poll()

    def client(self):
        if self.chamber is None:
            return None
        try:
            return self.links.client(self.chamber, wait=False)
        except Exception:  # noqa: BLE001
            return None

    def online(self) -> bool:
        s = self._snap
        return bool(
            s is not None
            and s.connected
            and s.values
            and s.values_at is not None
            and time.time() - s.values_at < FRESH_S
        )

    def status(self) -> str:
        return self._snap.status if self.online() else "none"

    def _value(self, name):
        if not self.online():
            return None
        value = self._snap.values.get(name)
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None

    def chamber_temperature(self):
        return self._value("temp")

    def poll(self):
        client = self.client()
        self._snap = client.snapshot() if client is not None else None
        online = self.online()
        self.chamber_state.setObjectName("chamberIconOn" if online else "chamberIconOff")
        self.chamber_state.style().unpolish(self.chamber_state)
        self.chamber_state.style().polish(self.chamber_state)
        if self.chamber is None:
            tip = self.tr("No chamber PLC: add one in Settings → Chamber Connections.")
        elif online:
            tip = self.tr("{0} answers ({1})").format(self.chamber["name"], self._snap.address)
        else:
            reason = self._snap.last_error if self._snap is not None else ""
            tip = self.tr("{0} is not answering{1}").format(
                self.chamber["name"], f": {reason}" if reason else ""
            )
        self.chamber_state.setToolTip(tip)
        temperature = self.chamber_temperature()
        self.interval_list.set_start_temperature(temperature)
        self.graph.set_start_temperature(temperature)
        self.update_gui()
        self._update_run_status()

    def setpoint_running(self) -> bool:
        """Sets whether a setpoint job runs."""
        work_type = self._value("work_type")
        return self.status() == "work" and work_type is not None and int(work_type) == PROG_SETPOINT

    def update_gui(self):
        count = self.model.count()
        status = self.status()
        working = status == "work"
        online = self.online()
        enable_start = online and status in ("idle", "finish", "work") and count > 0
        self.start_btn.setEnabled(enable_start)
        self.delay_btn.setEnabled(enable_start and not working)
        self.stop_btn.setEnabled(working)
        program_running = working and int(self._value("intervals_count") or 0) > 1
        self.next_btn.setVisible(program_running)
        self.finish_btn.setVisible(online and status == "finish")
        alarm = online and (status == "alarm" or bool(self._snap.alarm_code))
        self.alarm_btn.setVisible(alarm)
        self.cycle_widget.set_intervals_count(count)
        self.save_btn.setEnabled(count > 0)
        self.cycle_widget.setEnabled(count > 0)

    def details_complete(self) -> bool:
        return self.details.complete()

    def _details_edited(self):
        self.stepper.set_next_enabled(
            STEP_DETAILS,
            self.details_complete(),
            self.tr("Name or purpose and operator are required"),
        )
        self._update_confirmation()

    def _next_requested(self, step):
        if step == STEP_DETAILS and self.details_complete():
            self.stepper.set_current(STEP_INTERVALS)
        elif step == STEP_INTERVALS and self.intervals_ready():
            self.stepper.set_current(STEP_START)

    def intervals_ready(self) -> bool:
        interval = self.model.interval_without_setpoint()
        if interval < 0:
            return True
        self.stepper.set_current(STEP_INTERVALS)
        self.interval_list.show_interval(interval)
        QMessageBox.information(
            self,
            self.tr("Program control"),
            self.tr(
                "Interval {0} has no setpoint. Choose its variable and give it a setpoint."
            ).format(interval + 1),
        )
        return False

    def ready_to_start(self) -> bool:
        if not self.online():
            QMessageBox.information(
                self, self.tr("Program control"), self.tr("The chamber is not answering.")
            )
            return False
        if self.status() == "work" and not self.setpoint_running():
            QMessageBox.information(
                self,
                self.tr("Program control"),
                self.tr("A program is running. Stop it before starting another."),
            )
            return False
        if not self.details_complete():
            self.stepper.set_current(STEP_DETAILS)
            QMessageBox.information(
                self,
                self.tr("Program control"),
                self.tr("Give the experiment a name or purpose and an operator."),
            )
            return False
        return self.intervals_ready()

    def start_now(self):
        if not self.ready_to_start():
            return
        if not self.setpoint_running():
            self.send_program()
            return
        answer = QMessageBox.question(
            self,
            self.tr("Replace the setpoint"),
            self.tr(
                "A setpoint is running. Replace it with this experiment?\n\nThe chamber goes on to "
                "the new setpoint without stopping; the running one is saved as stopped."
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.send_program(replace=True)

    def delay_start(self):
        if not self.ready_to_start():
            return
        select = DelaySelectDialog(self)
        if select.exec() != QDialog.DialogCode.Accepted:
            return
        countdown = DelayCountdownDialog(self.tr("Program operation"), select.start_time(), self)
        countdown.accepted.connect(self.send_program)
        countdown.finished.connect(lambda _r: setattr(self, "_delay_dialog", None))
        self._delay_dialog = countdown
        countdown.show()

    def send_program(self, replace=False) -> bool:
        self.poll()
        if not self.ready_to_start():
            return False
        replacing = self.setpoint_running()
        if replacing and not replace:
            QMessageBox.information(
                self,
                self.tr("Program control"),
                self.tr("A setpoint is running. The delayed start was not done."),
            )
            return False
        try:
            low, high = control_settings.temperature_limits(self._settings)
        except Exception:  # noqa: BLE001
            low, high = -1e9, 1e9
        temp_row = self.model.row_of("temp")
        for i in range(self.model.count()):
            if temp_row >= 0 and self.model.flags(i) & (1 << temp_row):
                temp = float(self.model.value(i, "temp"))
                if not low <= temp <= high:
                    QMessageBox.warning(
                        self,
                        self.tr("Program control"),
                        self.tr(
                            "Interval {0} ({1:g} °C) is outside the chamber's range {2:g} … {3:g} °C."
                        ).format(i + 1, temp, low, high),
                    )
                    return False
        self.interval_list.set_start_temperature(self.chamber_temperature())
        self.interval_list.prepare_for_start()

        name = self._library_name
        self._running_name = (
            ""
            if not name
            else (self.tr("{0} (modified)").format(name) if self._library_modified else name)
        )
        self._running_labels = list(self._labels)
        count = self.model.count()
        program = self.model.program()
        prog_type = PROG_SETPOINT if count == 1 else PROG_PROGRAM
        self.model.prog_type = prog_type
        chamber = self.chamber["name"]
        details = {
            **self.details.metadata(),
            "program": program,
            "labels": self._running_labels,
            "user": self.current_user.get("name"),
        }
        if replacing and self.recorder is not None:
            self.recorder.set_working(chamber, False)
        label = (
            self.tr("Program {0}").format(self._running_name)
            if self._running_name
            else self.tr("Program")
        )
        experiment_recorder.announce_job(
            chamber, label, "program", test_profile=self._running_name or None, details=details
        )
        client = self.client()
        try:
            client.send_job(prog_type, program["intervals"], self._params, program["cycle"])
        except (RuntimeError, OSError, AttributeError) as exc:
            QMessageBox.warning(self, self.tr("Program control"), str(exc))
            return False
        log.info(
            "%s: program sent, %d interval(s)%s",
            chamber,
            count,
            f" ({self._running_name})" if self._running_name else "",
        )
        self.run_status.setText(
            self.tr("Starting program operation. {0} interval(s).").format(count)
        )
        self.job_sent.emit(chamber)
        QTimer.singleShot(400, self.poll)
        return True

    def stop_job(self):
        answer = QMessageBox.question(
            self,
            self.tr("Stop"),
            self.tr("Stop the job running on {0}?").format(
                self.chamber["name"] if self.chamber else ""
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._command("stop")

    def _command(self, command):
        client = self.client()
        if client is None:
            return
        try:
            client.send_text_cmd(command)
        except (RuntimeError, OSError) as exc:
            QMessageBox.warning(self, self.tr("Program control"), str(exc))
            return
        log.info("%s: %s sent", self.chamber["name"], command)
        QTimer.singleShot(400, self.poll)

    def _add_interval(self):
        index = self.model.selected() + 1
        if index <= len(self._labels):
            self._labels.insert(index, "")
            self._labels_edited()
        self.model.new_item(index)
        self.model.select(index)

    def _duplicate_interval(self, interval):
        if interval < len(self._labels):
            self._labels.insert(interval + 1, self._labels[interval])
            self._labels_edited()
        self.model.new_item(interval + 1)
        self.model.select(interval + 1)

    def _delete_interval(self, interval):
        if self.model.count() <= 1:
            return
        if interval < len(self._labels):
            del self._labels[interval]
            self._labels_edited()
        self.model.del_item(interval)

    def _hold_interval(self, interval):
        if interval < len(self._labels):
            self._labels.insert(interval + 1, "")
            self._labels_edited()
        self.model.new_item(interval + 1)
        self.interval_list.make_hold(interval + 1, 3600)
        self.model.select(interval + 1)

    def _label_edited(self, interval, label):
        while len(self._labels) <= interval:
            self._labels.append("")
        self._labels[interval] = label
        self._labels_edited()

    def _labels_edited(self):
        if not self._loading_library and self._library_name:
            self._library_modified = True
        self._update_program_label()

    def _open_interval(self, interval):
        if self.stepper.locked():
            return
        self.stepper.set_current(STEP_INTERVALS)
        self.interval_list.show_interval(interval)

    def _intervals_changed(self, *_):
        self._mark_modified()
        self.update_gui()
        self._update_sequence_info()

    def _cycle_edited(self, params):
        self.model.set_cycle(params)
        self._mark_modified()
        self._update_sequence_info()

    def _mark_modified(self, *_):
        if not self._loading_library and self._library_name and not self._library_modified:
            self._library_modified = True
            self._update_program_label()

    def open_library(self):
        dialog = ProgramLibraryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.selected is None:
            return
        self.load_profile(dialog.selected)

    def load_profile(self, profile) -> bool:
        self._loading_library = True
        error = load_into_model(profile, self.model)
        self._loading_library = False
        if error:
            QMessageBox.warning(self, self.tr("Program library"), error)
            return False
        self._set_library_program(
            profile["id"], profile["name"], [s.get("label", "") for s in profile["steps"]]
        )
        self.model.select(0)
        return True

    def save_to_library(self):
        if self.model.count() < 1:
            return
        dialog = ProgramSaveDialog(
            self.model, self._library_name, self._labels, self.current_user.get("name"), self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved:
            profile = dialog.saved
            self._set_library_program(
                profile["id"], profile["name"], [s.get("label", "") for s in profile["steps"]]
            )

    def _set_library_program(self, profile_id, name, labels):
        self._library_id = profile_id
        self._library_name = name
        self._labels = list(labels)
        self._library_modified = False
        self._update_program_label()

    def program_name(self):
        if not self._library_name:
            return self.tr("Untitled program")
        if self._library_modified:
            return self.tr("{0} (modified)").format(self._library_name)
        return self._library_name

    def _update_program_label(self):
        self.program_label.set_full_text(self._library_name or self.tr("Untitled program"))
        color = "#f59e0b" if self._library_modified else "#22c55e"
        self.library_badge.setText(
            self.tr("LIBRARY · MODIFIED") if self._library_modified else self.tr("LIBRARY")
        )
        self.library_badge.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 3px; padding: 1px 6px;"
            "font-weight: bold; font-size: 8pt; background: transparent;"
        )
        self.library_badge.setVisible(bool(self._library_name))
        self.interval_list.set_labels(self._labels)
        self._update_confirmation()

    def _update_sequence_info(self, *_):
        m = self.model
        if m.row_of("duration") < 0:
            self.summary_label.clear()
            return
        durations = m.durations()
        total = 0.0
        for d in durations:
            if d >= HOLD_UNTIL_STOPPED:
                self.summary_label.setText(
                    self.tr("{0}, then held until stopped").format(duration_text(total))
                    if total > 0
                    else self.tr("Held until stopped")
                )
                self.summary_label.setToolTip(self.tr("The programmed durations"))
                self._update_confirmation()
                return
            total += d
        cycle = m.cycle()
        repeats = int(cycle.get("count", 0))
        endless = repeats == INFINITE_CYCLES
        if repeats > 1 and not endless:
            start, end = int(cycle.get("start", 0)), int(cycle.get("end", 0))
            total += (repeats - 1) * sum(durations[start : end + 1])
        length = duration_text(total)
        if endless:
            text = self.tr("{0} per pass · repeats until stopped").format(length)
        else:
            ends = (datetime.now() + timedelta(seconds=total)).strftime("%H:%M")
            text = self.tr("{0} · ends {1} if started now").format(length, ends)
        self.summary_label.setText(text)
        self.summary_label.setToolTip(self.tr("The programmed durations"))
        self._update_confirmation()

    def _update_confirmation(self):
        if not hasattr(self, "facts"):
            return
        cycle = cycle_text(self.model.cycle())
        meta = self.details.metadata()
        dash = lambda t: t or "—"  # noqa: E731
        self.facts["purpose"].setText(dash(meta["purpose"]))
        dut = [meta["dut"]] if meta["dut"] else []
        if meta["dut"] and meta["dut_mass_kg"] is not None:
            dut.append(self.tr("{0:g} kg").format(meta["dut_mass_kg"]))
        if meta["dut"] and meta["dut_density_kg_m3"] is not None:
            dut.append(self.tr("{0:g} kg/m³").format(meta["dut_density_kg_m3"]))
        self.facts["dut"].setText(" · ".join(dut) if dut else self.tr("Not set"))
        self.facts["dut"].setStyleSheet("" if dut else "color: #f59e0b;")
        self.facts["operator"].setText(dash(meta["operator"]))
        count = self.model.count()
        self.facts["program"].setText(
            self.tr("{0} · {1} interval(s)").format(self.program_name(), count)
        )
        self.facts["cycle"].setText(cycle)
        self.facts["duration"].setText(dash(self.summary_label.text()))
        if not meta["purpose"]:
            summary = self.tr("Name or purpose not given")
        elif meta["dut"]:
            summary = f"{meta['purpose']} · {meta['dut']}"
        else:
            summary = meta["purpose"]
        self.stepper.set_summary(STEP_DETAILS, summary)
        self.stepper.set_summary(
            STEP_INTERVALS, self.tr("{0} interval(s) · cycle: {1}").format(count, cycle)
        )

    def _update_run_status(self):
        working = self.status() == "work"
        replaceable = self.setpoint_running()
        self.stepper.set_locked(working and not replaceable)
        if working and (not replaceable or not self._working):
            self.stepper.set_current(STEP_START)
        status = self.status()
        if status in ("finish", "alarm") and status != getattr(self, "_last_status", None):
            self.stepper.set_current(STEP_START)
        self._last_status = status
        self._working = working
        if not self.online():
            text = (
                self.tr("No chamber PLC: add one in Settings → Chamber Connections.")
                if self.chamber is None
                else self.tr("{0} is not answering.").format(self.chamber["name"])
            )
            self.run_status.setText(text)
            self.run_bar.setText(text)
            return
        status = self.status()
        if status == "finish":
            text = self.tr("The job finished: confirm it to return the chamber to idle.")
        elif status == "alarm" or self._snap.alarm_code:
            labels = control_settings.alarm_labels(self._settings)
            code = self._snap.alarm_code
            active = [labels.get(b, f"bit {b}") for b in range(32) if code & (1 << b)]
            text = self.tr("ALARM: {0}").format(", ".join(active) if active else status)
        elif not working:
            text = self.tr("{0} is idle.").format(self.chamber["name"])
        else:
            current = self._value("interval")
            count = self._value("intervals_count")
            running = self.recorder.running(self.chamber["name"]) if self.recorder else None
            if replaceable and (running is None or running.get("kind") != "program"):
                text = self.tr("A setpoint is running.")
            elif current is None or count is None:
                text = self.tr("A job is running.")
            else:
                parts = []
                if self._running_name:
                    parts.append(f"<b>{self._running_name}</b>")
                parts.append(self.tr("interval {0} of {1}").format(int(current), int(count)))
                index = int(current) - 1
                if 0 <= index < len(self._running_labels) and self._running_labels[index]:
                    parts.append(f"<i>{self._running_labels[index]}</i>")
                total = self._value("cycle_total")
                if total and total > 1:
                    parts.append(
                        self.tr("cycle {0} of {1}").format(
                            int(self._value("cycle_cur") or 0), int(total)
                        )
                    )
                text = " · ".join(parts)
        self.run_status.setText(text)
        self.run_bar.setText(text)
