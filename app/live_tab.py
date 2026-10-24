"""Live tab of the Analysis page (LiveTabPage): follows the experiment running
on a chamber now, like control2-client's live chart tab.

Shows the job since it started (or the last minutes when the chamber is
idle), its stages shaded on the chart, the chamber's state, and KPIs and
anomalies recomputed every few seconds with the Analysis Core, and saves it
as a run so experiments started from anywhere -- including by the agents --
land in the run database.

Two kinds of chamber:
- protocol "plc": the PLC itself over the PLC gate, through the app's shared
  ChamberLinks; jobs are seen starting and ending, and a finished job is
  saved by itself;
- protocol "json": sample feeds connected from the Controller page
  (ChamberSession); the whole recording since connecting (or the last save)
  is shown, and Save as Run saves it and starts a new recording.
"""

import math
import time
from datetime import datetime

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.chart_widget import ChartWidget
from app.common import fmt
from app.core.analysis import analyse_experiment
from app.core.analysis.anomalies import SEVERITY_RANK, WARNING
from app.core.analysis.experimentanalysis import ExperimentInput
from app.core.analysis.runsamples import columns_from_rows
from app.services import data_service, settings_service

LIVE_KEY = "live"
IDLE_WINDOW_S = 30 * 60  # shown while no job runs
MAX_CHART_POINTS = 3000
ANALYSIS_PERIOD_MS = 10_000
STAGE_COLORS = {"ramp": "#d97706", "setpoint": "#2563eb", "hold": "#16a34a", "unknown": "#64748b"}
CHANNELS = [
    ("temp", "Temperature"),
    ("temp_ref", "Setpoint"),
    ("temp_u", "Output %"),
    ("temp_raw", "Raw temp"),
]


class _AnalysisWorker(QThread):
    finished_ok = Signal(object, float)  # ExperimentAnalysis, epoch of the first sample

    def __init__(self, samples):
        super().__init__()
        self.samples = samples

    def run(self):
        times, channels = columns_from_rows(self.samples)
        if len(times) < 3:
            return
        start = times[0]
        analysis = analyse_experiment(ExperimentInput([t - start for t in times], channels))
        self.finished_ok.emit(analysis, start)


class LiveTabPage(QWidget):
    run_saved = Signal(str)  # key of a run saved from the live data

    def __init__(self, links, dark=True, sessions=None, parent=None):
        """sessions: callable returning the connected sample-feed chambers
        ({chamber id: ChamberSession}); none when omitted."""
        super().__init__(parent)
        self.run_key = LIVE_KEY  # the tab system and UI-state persistence key pages by this
        self.compare_runs = set()  # Analysis' run list asks every page for its comparison
        self.links = links
        self._sessions = sessions or dict
        self.dark = dark
        self._source = None
        self._analysis = None
        self._analysis_start = None
        self._worker = None
        self._saved_jobs = set()  # job start epochs already saved
        self._build_ui()
        self.reload_chambers()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        self._analysis_timer = QTimer(self)
        self._analysis_timer.timeout.connect(self._start_analysis)
        self._analysis_timer.start(ANALYSIS_PERIOD_MS)

    # ── interface the Analysis page expects from a tab ───────────────────────

    def set_compare_run(self, key, checked):
        pass  # the live data is not compared with recorded runs here

    def update_theme(self, dark):
        self.dark = dark
        self.chart.set_dark(dark)

    def showEvent(self, event):
        super().showEvent(event)
        self.reload_chambers()  # chambers may have been added or edited meanwhile

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        chart_card = QFrame()
        chart_card.setObjectName("card")
        chart_lay = QVBoxLayout(chart_card)
        chart_lay.setContentsMargins(10, 10, 10, 10)
        chart_lay.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        title = QLabel(self.tr("Live"))
        title.setObjectName("title")
        toolbar.addWidget(title)
        self.status_label = QLabel()
        self.status_label.setObjectName("inlineStatLabel")
        toolbar.addWidget(self.status_label)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel(self.tr("Chamber")))
        self.chamber_combo = QComboBox()
        self.chamber_combo.setMinimumWidth(180)
        self.chamber_combo.currentIndexChanged.connect(self._on_chamber_changed)
        toolbar.addWidget(self.chamber_combo)
        self.channel_checks = {}
        for name, label in CHANNELS:
            check = QCheckBox(self.tr(label))
            check.setChecked(name in ("temp", "temp_ref"))
            check.stateChanged.connect(self._refresh)
            self.channel_checks[name] = check
            toolbar.addWidget(check)
        self.follow = QCheckBox(self.tr("Follow"))
        self.follow.setChecked(True)
        self.follow.setToolTip(self.tr("Keep the whole experiment in view as data arrives"))
        toolbar.addWidget(self.follow)
        self.save_btn = QPushButton(self.tr("Save as Run"))
        self.save_btn.setObjectName("primaryButton")
        self.save_btn.clicked.connect(self._save_current)
        toolbar.addWidget(self.save_btn)
        chart_lay.addLayout(toolbar)

        self.chart = ChartWidget()
        self.chart.set_dark(self.dark)
        chart_lay.addWidget(self.chart, 1)
        self.idle_label = QLabel()
        self.idle_label.setObjectName("sectionLabel")
        chart_lay.addWidget(self.idle_label)
        root.addWidget(chart_card, 3)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.now_values = self._value_card(
            cards,
            self.tr("CHAMBER NOW"),
            [
                ("status", self.tr("Status")),
                ("temp", self.tr("Temperature")),
                ("setpoint", self.tr("Setpoint")),
                ("output", self.tr("Output")),
                ("program", self.tr("Program")),
                ("elapsed", self.tr("Job time")),
            ],
        )
        self.kpi_values = self._value_card(
            cards,
            self.tr("EXPERIMENT SO FAR"),
            [
                ("stage", self.tr("Stage")),
                ("stage_time", self.tr("In stage")),
                ("error", self.tr("Error now")),
                ("mae", self.tr("MAE")),
                ("overshoot", self.tr("Overshoot")),
                ("settling", self.tr("Settling")),
            ],
        )
        anomaly_card = QFrame()
        anomaly_card.setObjectName("card")
        anomaly_lay = QVBoxLayout(anomaly_card)
        anomaly_lay.setContentsMargins(10, 10, 10, 10)
        heading = QLabel(self.tr("ANOMALIES"))
        heading.setObjectName("sectionLabel")
        anomaly_lay.addWidget(heading)
        self.anomaly_label = QLabel("—")
        self.anomaly_label.setWordWrap(True)
        anomaly_lay.addWidget(self.anomaly_label, 1)
        self.autosave = QCheckBox(self.tr("Save finished jobs as runs"))
        self.autosave.setChecked(settings_service.load_live_autosave())
        self.autosave.toggled.connect(settings_service.save_live_autosave)
        anomaly_lay.addWidget(self.autosave)
        cards.addWidget(anomaly_card, 1)
        root.addLayout(cards, 1)

    def _value_card(self, parent_layout, heading, rows):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        title = QLabel(heading)
        title.setObjectName("sectionLabel")
        lay.addWidget(title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        values = {}
        for i, (key, label) in enumerate(rows):
            cap = QLabel(label)
            cap.setObjectName("inlineStatLabel")
            val = QLabel("—")
            val.setObjectName("inlineStatValue")
            grid.addWidget(cap, i // 2, (i % 2) * 2)
            grid.addWidget(val, i // 2, (i % 2) * 2 + 1)
            values[key] = val
        lay.addLayout(grid)
        lay.addStretch(1)
        parent_layout.addWidget(card, 1)
        return values

    # ── chambers ─────────────────────────────────────────────────────────────

    def _sources(self):
        sources = [
            _PlcSource(self.links, c) for c in self.links.chambers() if c["protocol"] == "plc"
        ]
        sources += [_JsonSource(session) for session in self._sessions().values()]
        return sources

    def reload_chambers(self):
        current = self._source.key if self._source else None
        sources = self._sources()
        self.chamber_combo.blockSignals(True)
        self.chamber_combo.clear()
        for source in sources:
            self.chamber_combo.addItem(source.label, source)
        index = next((i for i, src in enumerate(sources) if src.key == current), 0)
        if sources:
            self.chamber_combo.setCurrentIndex(index)
        self.chamber_combo.blockSignals(False)
        self._on_chamber_changed()

    def _on_chamber_changed(self, *_):
        source = self.chamber_combo.currentData()
        if source is None or self._source is None or source.key != self._source.key:
            self._analysis = None
        self._source = source
        if source is not None:
            source.start()
        self._refresh()

    # ── live data ────────────────────────────────────────────────────────────

    def _refresh(self, *_):
        source = self._source
        if source is None:
            self._show_idle(
                self.tr(
                    "No chamber to follow. Connect one on the Controller page, or add a chamber "
                    'PLC (Controller → Chamber Settings…, connection "Chamber PLC").'
                )
            )
            return
        state = source.state()
        if not state["connected"]:
            self._show_idle(self.tr("Connecting to {0}{1}").format(source.address, state["note"]))
            return
        samples, is_job = source.samples()
        self._update_now(state)
        self._maybe_autosave(source)
        if source.kind == "json":
            label = self.tr("Recording {0} sample(s)").format(len(samples))
        elif is_job:
            label = self.tr("Job running") if state["working"] else self.tr("Last job")
        else:
            label = self.tr("No job running: last {0} min").format(IDLE_WINDOW_S // 60)
        self.status_label.setText(f"{state['status'].upper()} · {label}")
        self.idle_label.setText("")
        self.save_btn.setEnabled(is_job and len(samples) > 2)
        if samples:
            self._draw(samples)
        else:
            self.chart.draw({"columns": [], "points": []})

    def _show_idle(self, text):
        self.status_label.setText("")
        self.idle_label.setText(text)
        self.save_btn.setEnabled(False)
        for widget in list(self.now_values.values()) + list(self.kpi_values.values()):
            widget.setText("—")
        self.chart.draw({"columns": [], "points": []})

    def _draw(self, samples):
        step = max(1, math.ceil(len(samples) / MAX_CHART_POINTS))
        shown = samples[::step]
        columns = [name for name, check in self.channel_checks.items() if check.isChecked()]
        points = [
            {"i": i, "t": s["timestamp"], "values": {c: s.get(c) for c in columns}}
            for i, s in enumerate(shown)
        ]
        if self.follow.isChecked():
            self.chart.auto_range_on_draw = True
        self.chart.draw({"columns": columns, "points": points})
        self._draw_stages(shown[0]["timestamp"] if shown else None)

    def _draw_stages(self, first_t):
        if self._analysis is None or first_t is None or self._analysis_start is None:
            return
        offset = self._analysis_start - first_t  # analysis times are from its own first sample
        for stage in self._analysis.run.stages:
            s = stage.segment
            self.chart.add_x_annotation(
                s.start + offset,
                max(s.end, s.start + 1.0) + offset,
                s.kind,
                STAGE_COLORS.get(s.kind, "#64748b"),
            )

    def _update_now(self, state):
        v = state["values"]
        output = _number(v.get("temp_u"))
        mode = ""
        if output is not None:
            mode = (
                self.tr(" heating") if output > 1 else (self.tr(" cooling") if output < -1 else "")
            )
        elapsed = _duration(time.time() - state["since"]) if state["since"] else "—"
        values = {
            "status": state["status"] + (f" · {state['alarm']}" if state["alarm"] else ""),
            "temp": f"{fmt(_number(v.get('temp')), 2)} °C",
            "setpoint": f"{fmt(_number(v.get('temp_ref')), 2)} °C",
            "output": f"{fmt(output, 1)} %{mode}",
            "program": state["program"] or "—",
            "elapsed": elapsed,
        }
        for key, text in values.items():
            self.now_values[key].setText(text)
        temp, ref = _number(v.get("temp")), _number(v.get("temp_ref"))
        error = None if temp is None or ref is None else ref - temp
        self.kpi_values["error"].setText(f"{fmt(error, 2)} °C" if error is not None else "—")

    # ── analysis (background) ────────────────────────────────────────────────

    def _start_analysis(self):
        if self._worker is not None and self._worker.isRunning():
            return
        source = self._source
        if source is None or not source.state()["connected"]:
            return
        samples, is_job = source.samples()
        if not is_job or len(samples) < 3:
            self._analysis = None
            for key in ("stage", "stage_time", "mae", "overshoot", "settling"):
                self.kpi_values[key].setText("—")
            self.anomaly_label.setText(self.tr("Shown while a job runs."))
            return
        self._worker = _AnalysisWorker(list(samples))
        self._worker.finished_ok.connect(self._on_analysis)
        self._worker.start()

    def _on_analysis(self, analysis, start):
        self._analysis = analysis
        self._analysis_start = start
        stages = analysis.run.stages
        if stages:
            last = stages[-1]
            self.kpi_values["stage"].setText(f"{len(stages)} · {last.segment.kind}")
            self.kpi_values["stage_time"].setText(_duration(last.segment.end - last.segment.start))
        k = analysis.run.overall
        headline = k.mae if analysis.run.has_ramp() else k.tail_mae
        self.kpi_values["mae"].setText(f"{fmt(headline, 2)} °C" if _finite(headline) else "—")
        self.kpi_values["overshoot"].setText(
            f"{fmt(analysis.run.overshoot, 2)} °C" if _finite(analysis.run.overshoot) else "—"
        )
        settling = analysis.run.settling_time_s
        self.kpi_values["settling"].setText(
            _duration(settling) if _finite(settling) else self.tr("not yet")
        )
        rank = SEVERITY_RANK[WARNING]
        found = [a for a in analysis.anomalies if SEVERITY_RANK[a.severity] >= rank]
        self.anomaly_label.setText(
            "\n".join(f"⚠ {a.detail}" for a in found[-5:]) or self.tr("None so far.")
        )

    # ── saving ───────────────────────────────────────────────────────────────

    def _maybe_autosave(self, source):
        if not self.autosave.isChecked():
            return
        ended = source.ended_job()
        if ended is None or ended[0] in self._saved_jobs:
            return
        self._saved_jobs.add(ended[0])
        self._save(source, ended[1], quiet=True)

    def _save_current(self):
        if self._source is None:
            return
        samples, is_job = self._source.samples()
        if is_job:
            self._save(self._source, list(samples), quiet=False)

    def _save(self, source, samples, quiet):
        if len(samples) < 3:
            return
        started = datetime.fromtimestamp(samples[0]["timestamp"]).strftime("%Y-%m-%d %H:%M")
        if source.kind == "json":
            name = f"{started} {source.chamber['name']}"
        else:
            name = f"{started} Live job"
        try:
            result = data_service.save_monitoring_session(
                name, samples, chamber=source.chamber["name"], test_profile=source.test_profile()
            )
        except Exception as exc:
            if not quiet:
                QMessageBox.critical(self, self.tr("Save failed"), str(exc))
            return
        source.after_save()
        self._analysis = None
        self.run_saved.emit(result["key"])
        if not quiet:
            QMessageBox.information(
                self, self.tr("Saved"), self.tr('Saved as run "{0}".').format(name)
            )


class _PlcSource:
    """A chamber PLC (protocol "plc"), read over the PLC gate."""

    kind = "plc"

    def __init__(self, links, chamber):
        self.links = links
        self.chamber = chamber
        self.key = ("plc", chamber["id"])
        self.address = f"{chamber['host']}:{chamber['port']}"
        self.label = f"{chamber['name']} ({self.address}, PLC)"

    def _client(self):
        return self.links.client(self.chamber, wait=False)

    def start(self):
        self._client()  # starts connecting in the background

    def state(self):
        snap = self._client().snapshot()
        v = snap.values
        program = ""
        if v.get("interval"):
            program = f"interval {int(v['interval'])}/{int(v.get('intervals_count', 0))}"
            if v.get("cycle_total"):
                program += f", cycle {int(v.get('cycle_cur', 0))}/{int(v['cycle_total'])}"
        working = snap.status == "work"
        return {
            "connected": snap.connected and bool(v),
            "note": f": {snap.last_error}" if snap.last_error else "…",
            "status": snap.status,
            "working": working,
            "values": v,
            "program": program,
            "since": snap.job_started_at if working else None,
            "alarm": f"alarm {snap.alarm_code}" if snap.alarm_code else "",
        }

    def samples(self):
        """The current or last job's samples; the last minutes when no job was seen."""
        client = self._client()
        snap = client.snapshot()
        if snap.job_started_at:
            samples = client.history(since=snap.job_started_at - 0.001)
            if snap.job_ended_at:
                samples = [s for s in samples if s["timestamp"] <= snap.job_ended_at]
            if samples:
                return samples, True
        return client.history(since=time.time() - IDLE_WINDOW_S), False

    def ended_job(self):
        snap = self._client().snapshot()
        if snap.job_started_at and snap.job_ended_at:
            samples, _ = self.samples()
            return snap.job_started_at, samples
        return None

    def test_profile(self):
        return None

    def after_save(self):
        pass


class _JsonSource:
    """A sample-feed chamber (protocol "json") connected from the Controller page."""

    kind = "json"

    def __init__(self, session):
        self.session = session
        self.chamber = session.chamber
        self.key = ("json", self.chamber["id"])
        self.address = f"{self.chamber['host']}:{self.chamber['port']}"
        self.label = f"{self.chamber['name']} ({self.address})"

    def start(self):
        pass  # connected and disconnected on the Controller page

    def state(self):
        s = self.session
        buffer = s.mon_buffer
        program = ""
        if s.test_running_profile is not None:
            steps = len(s.test_running_profile["steps"])
            program = f"{s.test_running_profile['name']}, step {s.test_step_index + 1}/{steps}"
        elif s.manual_running:
            program = "manual setpoint"
        working = s.manual_running or s.test_running_profile is not None
        connected = s.is_connected()
        return {
            "connected": connected or bool(buffer),
            "note": f": {s.connection_note}" if s.connection_note else "…",
            "status": ("work" if working else "online") if connected else "offline",
            "working": working,
            "values": buffer[-1] if buffer else {},
            "program": program,
            "since": buffer[0]["timestamp"] if buffer else None,
            "alarm": "",
        }

    def samples(self):
        buffer = self.session.mon_buffer
        return buffer, bool(buffer)

    def ended_job(self):
        return None  # a feed has no job boundaries: saved from Save as Run or on disconnect

    def test_profile(self):
        return self.session.session_test_profile_name

    def after_save(self):
        self.session.clear_buffer()


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _finite(value):
    return value is not None and math.isfinite(value)


def _duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
