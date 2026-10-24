"""Live tab of the Analysis page (LiveTabPage): follows the experiment running
on a PLC chamber now, like control2-client's live chart tab.

Shows the job since it started (or the last minutes when the chamber is
idle), its stages shaded on the chart, the chamber's state, and KPIs and
anomalies recomputed every few seconds with the Analysis Core. When a job
ends it can be saved as a run, so experiments started from anywhere --
including by the agents -- land in the run database.

Data comes from the PLC gate (protocol "plc" chambers) through the app's
shared ChamberLinks; chambers with protocol "json" are followed in Live
Monitoring instead.
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

    def __init__(self, links, dark=True, parent=None):
        super().__init__(parent)
        self.run_key = LIVE_KEY  # the tab system and UI-state persistence key pages by this
        self.compare_runs = set()  # Analysis' run list asks every page for its comparison
        self.links = links
        self.dark = dark
        self._chamber = None
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

    def reload_chambers(self):
        current = self._chamber["id"] if self._chamber else None
        plcs = [c for c in self.links.chambers() if c["protocol"] == "plc"]
        self.chamber_combo.blockSignals(True)
        self.chamber_combo.clear()
        for chamber in plcs:
            self.chamber_combo.addItem(
                f"{chamber['name']} ({chamber['host']}:{chamber['port']})", chamber
            )
        index = next((i for i, c in enumerate(plcs) if c["id"] == current), 0)
        if plcs:
            self.chamber_combo.setCurrentIndex(index)
        self.chamber_combo.blockSignals(False)
        self._on_chamber_changed()

    def _on_chamber_changed(self, *_):
        self._chamber = self.chamber_combo.currentData()
        self._analysis = None
        if self._chamber:
            self.links.client(self._chamber, wait=False)  # starts connecting in the background
        self._refresh()

    def _client(self):
        return self.links.client(self._chamber, wait=False) if self._chamber else None

    # ── live data ────────────────────────────────────────────────────────────

    def _job_samples(self, client, snap):
        """The current or last job's samples; the last minutes when no job was seen."""
        if snap.job_started_at:
            samples = client.history(since=snap.job_started_at - 0.001)
            if snap.job_ended_at:
                samples = [s for s in samples if s["timestamp"] <= snap.job_ended_at]
            if samples:
                return samples, True
        return client.history(since=time.time() - IDLE_WINDOW_S), False

    def _refresh(self, *_):
        client = self._client()
        if client is None:
            self._show_idle(
                self.tr('No PLC chamber. Add one with protocol "plc" (see the README, Agent API).')
            )
            return
        snap = client.snapshot()
        if not snap.connected or not snap.values:
            reason = f": {snap.last_error}" if snap.last_error else "…"
            self._show_idle(self.tr("Connecting to {0}{1}").format(snap.address, reason))
            return
        samples, is_job = self._job_samples(client, snap)
        self._update_now(snap)
        self._maybe_autosave(client, snap)
        working = snap.status == "work"
        if is_job:
            label = self.tr("Job running") if working else self.tr("Last job")
        else:
            label = self.tr("No job running: last {0} min").format(IDLE_WINDOW_S // 60)
        self.status_label.setText(f"{snap.status.upper()} · {label}")
        self.idle_label.setText("")
        self.save_btn.setEnabled(is_job and len(samples) > 2)
        self._draw(samples)

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

    def _update_now(self, snap):
        v = snap.values
        output = v.get("temp_u")
        mode = ""
        if output is not None:
            mode = (
                self.tr(" heating") if output > 1 else (self.tr(" cooling") if output < -1 else "")
            )
        program = "—"
        if v.get("interval"):
            program = self.tr("interval {0}/{1}").format(
                int(v["interval"]), int(v.get("intervals_count", 0))
            )
            if v.get("cycle_total"):
                program += self.tr(", cycle {0}/{1}").format(
                    int(v.get("cycle_cur", 0)), int(v["cycle_total"])
                )
        elapsed = "—"
        if snap.status == "work" and snap.job_started_at:
            elapsed = _duration(time.time() - snap.job_started_at)
        values = {
            "status": snap.status,
            "temp": f"{fmt(v.get('temp'), 2)} °C",
            "setpoint": f"{fmt(v.get('temp_ref'), 2)} °C",
            "output": f"{fmt(output, 1)} %{mode}",
            "program": program,
            "elapsed": elapsed,
        }
        for key, text in values.items():
            self.now_values[key].setText(text)
        if snap.alarm_code:
            self.now_values["status"].setText(f"{snap.status} · alarm {snap.alarm_code}")
        error = (
            None
            if v.get("temp") is None or v.get("temp_ref") is None
            else v["temp_ref"] - v["temp"]
        )
        self.kpi_values["error"].setText(f"{fmt(error, 2)} °C" if error is not None else "—")

    # ── analysis (background) ────────────────────────────────────────────────

    def _start_analysis(self):
        if self._worker is not None and self._worker.isRunning():
            return
        client = self._client()
        if client is None:
            return
        snap = client.snapshot()
        if not snap.connected:
            return
        samples, is_job = self._job_samples(client, snap)
        if not is_job or len(samples) < 3:
            self._analysis = None
            for key in ("stage", "stage_time", "mae", "overshoot", "settling"):
                self.kpi_values[key].setText("—")
            self.anomaly_label.setText(self.tr("Shown while a job runs."))
            return
        self._worker = _AnalysisWorker(samples)
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

    def _maybe_autosave(self, client, snap):
        if not (self.autosave.isChecked() and snap.job_started_at and snap.job_ended_at):
            return
        if snap.job_started_at in self._saved_jobs:
            return
        self._saved_jobs.add(snap.job_started_at)
        samples, _ = self._job_samples(client, snap)
        self._save(samples, quiet=True)

    def _save_current(self):
        client = self._client()
        if client is None:
            return
        samples, is_job = self._job_samples(client, client.snapshot())
        if is_job:
            self._save(samples, quiet=False)

    def _save(self, samples, quiet):
        if len(samples) < 3:
            return
        started = datetime.fromtimestamp(samples[0]["timestamp"]).strftime("%Y-%m-%d %H:%M")
        name = f"{started} Live job"
        try:
            result = data_service.save_monitoring_session(
                name, samples, chamber=self._chamber["name"]
            )
        except Exception as exc:
            if not quiet:
                QMessageBox.critical(self, self.tr("Save failed"), str(exc))
            return
        self.run_saved.emit(result["key"])
        if not quiet:
            QMessageBox.information(
                self, self.tr("Saved"), self.tr('Saved as run "{0}".').format(name)
            )


def _finite(value):
    return value is not None and math.isfinite(value)


def _duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
