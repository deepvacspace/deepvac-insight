"""Live tab of the Analysis page."""

import contextlib
import math
import time
from datetime import date, datetime, timedelta
from datetime import time as day_time

import numpy as np
from PySide6.QtCore import QDate, QDateTime, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.chart_side_bar import GAP, ChartSideBar
from app.chart_widget import ChartWidget
from app.common import COLORS, fmt
from app.core.analysis import analyse_experiment
from app.core.analysis.anomalies import SEVERITY_RANK, WARNING
from app.core.analysis.experimentanalysis import ExperimentInput
from app.core.analysis.runsamples import columns_from_rows
from app.services import data_service, day_recorder, run_export, settings_service
from app.theme_colors import HMI

LIVE_KEY = "live"
IDLE_WINDOW_S = 30 * 60
MAX_CHART_POINTS = 3000
ANALYSIS_PERIOD_MS = 10_000
WINDOW_REFRESH_S = 5.0
WINDOWS = [
    ("job", "Experiment"),
    ("1h", "Last hour"),
    ("6h", "Last 6 hours"),
    ("today", "Today"),
    ("24h", "Last 24 hours"),
    ("day", "Day…"),
]
STAGE_COLORS = {
    "ramp": HMI["connecting"],
    "setpoint": HMI["cobalt"],
    "hold": HMI["live"],
    "unknown": HMI["haze"],
}
DEFAULT_COLORS = {"temp": HMI["temperature"], "pres": HMI["pressure"], "temp_ref": HMI["steel"]}
RAIL_ICON_LIGHT = HMI["cobalt"]
RAIL_ICON_DARK = HMI["sky"]
CHANNEL_LABELS = {"temp": "Temperature", "temp_ref": "Setpoint", "temp_u": "Output %",
                  "temp_raw": "Raw temp", "pres": "Pressure", "pres_ref": "Pressure setpoint"}  # fmt: skip
NOT_CHANNELS = {"timestamp", "plc_time", "status_condition"}


def _state_meta():
    """Returns the PLC states' labels and units."""
    try:
        from app.api import control_settings

        return control_settings.state_meta(control_settings.load()) or {}
    except Exception:
        return {}


class _AnalysisWorker(QThread):
    finished_ok = Signal(object, float)

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
    run_saved = Signal(str)

    def __init__(self, links, dark=True, sessions=None, recorder=None, parent=None):
        """Live tab; sessions returns the connected sample-feed chambers."""
        super().__init__(parent)
        self._recorder = recorder or (lambda: None)
        self._window_drawn = None
        self._window_drawn_at = 0.0
        self._past_days = {}
        self.run_key = LIVE_KEY
        self.compare_runs = set()
        self.links = links
        self._sessions = sessions or dict
        self.dark = dark
        self._source = None
        self._analysis = None
        self._analysis_start = None
        self._worker = None
        self.colors = {}
        self.selected = set(settings_service.load_channels(LIVE_KEY) or ("temp", "temp_ref"))
        self._available = []
        self._calculated = []
        self._x_origin = None
        self._mode = "line"
        self._wall_clock = True
        self._load_calculated()
        self._build_ui()
        self.update_theme(dark)
        self.reload_chambers()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._analysis_timer = QTimer(self)
        self._analysis_timer.timeout.connect(self._start_analysis)
        self._analysis_timer.start(ANALYSIS_PERIOD_MS)

    def set_compare_run(self, key, checked):
        pass

    def update_theme(self, dark):
        self.dark = dark
        self.chart.set_dark(dark)
        self.side_bar.set_icon_color(RAIL_ICON_DARK if dark else RAIL_ICON_LIGHT)

    def showEvent(self, event):
        super().showEvent(event)
        self.reload_chambers()

    def _build_ui(self):
        """Builds the chart, its rail and panels."""
        from app.analysis_panels import ChannelList, ChartTools

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(GAP)

        chart_card = QFrame()
        chart_card.setObjectName("card")
        chart_lay = QVBoxLayout(chart_card)
        chart_lay.setContentsMargins(10, 8, 10, 8)
        chart_lay.setSpacing(6)
        self.status_label = QLabel()
        self.status_label.setObjectName("inlineStatLabel")
        chart_lay.addWidget(self.status_label)
        self.chart = ChartWidget()
        self.chart.plot.setMinimumHeight(200)
        self.chart.set_dark(self.dark)
        self.chart.annotation_committed.connect(self._on_annotation_committed)
        chart_lay.addWidget(self.chart, 1)
        self.idle_label = QLabel()
        self.idle_label.setObjectName("sectionLabel")
        self.idle_label.setWordWrap(True)
        chart_lay.addWidget(self.idle_label)

        self.side_bar = ChartSideBar(self)
        self.channel_list = ChannelList(manage=True)
        self.channel_list.toggled.connect(self._set_channel_shown)
        self.channel_list.color_changed.connect(self._set_channel_color)
        self.channel_list.manage_requested.connect(self._manage_calculated)
        self.side_bar.add_page("channels", self.tr("Channels"), "rail-channels", self.channel_list)
        self.side_bar.add_page("options", self.tr("Options"), "rail-options", self._options_page())
        self.side_bar.add_page(
            "statistics", self.tr("Statistics"), "rail-stats", self._statistics_page()
        )
        self.side_bar.add_page("info", self.tr("Info"), "rail-info", self._info_page())
        assistant = self.side_bar.add_action(self.tr("Assistant"), "rail-ai")
        assistant.setToolTip(self.tr("Chat with the Deepvac Agents"))
        assistant.clicked.connect(self._toggle_assistant)
        self.side_bar.add_page(
            "export", self.tr("Export"), "rail-export", self._export_page(), bottom=True
        )
        self.side_bar.add_page(
            "calendar", self.tr("Calendar"), "rail-calendar", self._calendar_page(), bottom=True
        )
        self.tools = ChartTools(self.side_bar, self.chart, annotate=True)
        self.side_bar.set_tools(
            self.tr("Chart tools"), self.tr("Tools"), "rail-pointer", self.tools
        )
        self.side_bar.opened.connect(self._on_panel_opened)
        self._refresh_channel_list([])

        root.addWidget(self.side_bar)
        root.addWidget(chart_card, 1)

    @staticmethod
    def _heading(text):
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _value_grid(self, lay, rows):
        """Builds name and value rows and returns the value labels."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        values = {}
        for i, (key, label) in enumerate(rows):
            cap = QLabel(label)
            cap.setObjectName("inlineStatLabel")
            val = QLabel("—")
            val.setObjectName("inlineStatValue")
            val.setWordWrap(True)
            grid.addWidget(cap, i, 0)
            grid.addWidget(val, i, 1)
            values[key] = val
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        return values

    def _refresh_channel_list(self, available):
        """Rebuilds the channel list."""
        meta = _state_meta()
        calculated = self._calculated_available(available)
        key = (
            tuple(available),
            tuple(d["name"] for d in calculated),
            tuple(sorted(self.colors.items())),
        )
        if key == getattr(self, "_channel_key", None):
            return
        self._channel_key = key
        for i, name in enumerate(available):
            self.colors.setdefault(name, DEFAULT_COLORS.get(name) or COLORS[i % len(COLORS)])
        for i, d in enumerate(calculated):
            self.colors.setdefault(d["name"], d.get("color") or COLORS[(i + 3) % len(COLORS)])
        self.channel_list.set_channels(
            [
                {
                    "key": name,
                    "label": (meta.get(name) or {}).get("label")
                    or self.tr(CHANNEL_LABELS.get(name, name)),
                    "unit": (meta.get(name) or {}).get("unit") or "",
                    "color": self.colors[name],
                    "checked": name in self.selected,
                    "tooltip": name,
                }
                for name in available
            ],
            [
                {
                    "key": d["name"],
                    "label": d["name"],
                    "color": self.colors[d["name"]],
                    "checked": d["name"] in self.selected,
                    "tooltip": d.get("expression") or d.get("source_channel", ""),
                }
                for d in calculated
            ],
        )
        self.side_bar.set_subtitle("channels", self.channel_list.shown_text())
        self._refresh_overlay_channels(available)

    def _set_channel_shown(self, name, shown):
        (self.selected.add if shown else self.selected.discard)(name)
        settings_service.save_channels(LIVE_KEY, sorted(self.selected))
        self.side_bar.set_subtitle("channels", self.channel_list.shown_text())
        self._redraw_now()

    def _set_channel_color(self, name, color):
        self.colors[name] = color
        self._channel_key = None
        self._refresh_channel_list(self._available)
        self._redraw_now()

    def _calculated_available(self, available):
        from app.services import derived_variables_service as dv

        have = set(available)
        return [d for d in self._calculated if not dv.missing_channels(d, have)]

    def _manage_calculated(self):
        from app.derived_variables_dialog import DerivedVariablesDialog

        dialog = DerivedVariablesDialog(list(self._available), parent=self)
        dialog.exec()
        if dialog.changed:
            self._load_calculated()
            self._channel_key = None
            self._refresh_channel_list(self._available)
            self._redraw_now()

    def _load_calculated(self):
        from app.services import derived_variables_service as dv

        try:
            self._calculated = dv.list_derived_variables()
        except Exception:
            self._calculated = []

    def _options_page(self):
        from app.analysis_panels import add_section, caption, link_button, panel_page, segments

        page, lay = panel_page()
        source = add_section(lay, self.tr("Chamber"))
        self.chamber_combo = QComboBox()
        self.chamber_combo.currentIndexChanged.connect(self._on_chamber_changed)
        source.addWidget(self.chamber_combo)
        source.addWidget(caption(self.tr("Show")))
        self.window_combo = QComboBox()
        for key, label in WINDOWS:
            self.window_combo.addItem(self.tr(label), key)
        self.window_combo.setToolTip(
            self.tr("The running experiment, or a window of the chamber's all-day recording")
        )
        self.window_combo.currentIndexChanged.connect(self._on_window_changed)
        source.addWidget(self.window_combo)
        self.save_btn = QPushButton(self.tr("Save as Run"))
        self.save_btn.setObjectName("primaryButton")
        self.save_btn.clicked.connect(self._save_current)
        self.save_btn.setToolTip(
            self.tr("Jobs are saved by themselves when they end; this saves the current one now")
        )
        source.addSpacing(4)
        source.addWidget(self.save_btn)
        autosave = QLabel(self.tr("Every experiment is saved as a run when it ends."))
        autosave.setProperty("muted", True)
        autosave.setWordWrap(True)
        source.addWidget(autosave)

        rng = add_section(lay, self.tr("Time range"))
        self.from_edit = QDateTimeEdit()
        self.to_edit = QDateTimeEdit()
        for edit in (self.from_edit, self.to_edit):
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setCalendarPopup(True)
            edit.setDateTime(QDateTime.currentDateTime())
        rng.addWidget(caption(self.tr("From")))
        rng.addWidget(self.from_edit)
        rng.addWidget(caption(self.tr("To")))
        rng.addWidget(self.to_edit)
        row = QHBoxLayout()
        apply_btn = QPushButton(self.tr("Apply"))
        apply_btn.setObjectName("primaryButton")
        apply_btn.clicked.connect(self._apply_time_range)
        whole = link_button(self.tr("Reset to whole range"))
        whole.clicked.connect(self._reset_time_range)
        row.addWidget(apply_btn)
        row.addWidget(whole)
        row.addStretch(1)
        rng.addLayout(row)

        display = add_section(lay, self.tr("Display"))
        display.addWidget(caption(self.tr("Smoothing")))
        self.smoothing = QComboBox()
        self.smoothing.addItem(self.tr("Off"), 1)
        for samples in (3, 5, 11, 21, 51, 101, 201):
            self.smoothing.addItem(self.tr("{0} samples").format(samples), samples)
        self.smoothing.currentIndexChanged.connect(
            lambda *_: self.chart.set_smoothing_window(self.smoothing.currentData())
        )
        display.addWidget(self.smoothing)
        display.addWidget(caption(self.tr("Draw as")))
        draw_row, (lines_btn, points_btn) = segments([self.tr("Lines"), self.tr("Points")])
        lines_btn.setChecked(True)
        lines_btn.clicked.connect(lambda: self._set_mode("line"))
        points_btn.clicked.connect(lambda: self._set_mode("scatter"))
        display.addLayout(draw_row)
        display.addWidget(caption(self.tr("Time axis")))
        axis_row, (wall_btn, start_btn) = segments([self.tr("Wall clock"), self.tr("From start")])
        wall_btn.setChecked(True)
        wall_btn.clicked.connect(lambda: self._set_wall_clock(True))
        start_btn.clicked.connect(lambda: self._set_wall_clock(False))
        display.addLayout(axis_row)
        self.follow = QCheckBox(self.tr("Follow new values"))
        self.follow.setChecked(True)
        self.follow.setToolTip(self.tr("Keep the whole experiment in view as data arrives"))
        display.addWidget(self.follow)

        markers = add_section(lay, self.tr("Markers"))
        mk_row, (self.mk_stages, self.mk_anomalies, self.mk_annotations) = segments(
            [self.tr("Stages"), self.tr("Anomalies"), self.tr("Annotations")], exclusive=False
        )
        self.mk_stages.setToolTip(self.tr("Ramp / setpoint / hold stages, shaded"))
        self.mk_anomalies.setToolTip(self.tr("The detected anomalies as lines"))
        self.mk_annotations.setToolTip(self.tr("The time ranges marked with Annotate"))
        for button in (self.mk_stages, self.mk_annotations):
            button.setChecked(True)
        for button in (self.mk_stages, self.mk_anomalies, self.mk_annotations):
            button.toggled.connect(self._redraw_now)
        markers.addLayout(mk_row)

        overlays = add_section(lay, self.tr("Reference lines"))
        self.overlay_combo = QComboBox()
        self.overlay_combo.currentIndexChanged.connect(
            lambda *_: self.chart.set_overlay_channel(self.overlay_combo.currentData())
        )
        overlays.addWidget(self.overlay_combo)
        overlays.addWidget(caption(self.tr("Lines at")))
        ref_row, (self.overlay_min, self.overlay_max, self.overlay_avg) = segments(
            [self.tr("Min"), self.tr("Max"), self.tr("Average")], exclusive=False
        )
        for button in (self.overlay_min, self.overlay_max, self.overlay_avg):
            button.toggled.connect(
                lambda *_: self.chart.set_overlay_flags(
                    min=self.overlay_min.isChecked(),
                    max=self.overlay_max.isChecked(),
                    avg=self.overlay_avg.isChecked(),
                )
            )
        overlays.addLayout(ref_row)

        calc = add_section(lay, self.tr("Calculated channels"), open_=False)
        manage = link_button(self.tr("+ New calculated channel"))
        manage.clicked.connect(self._manage_calculated)
        calc.addWidget(manage, 0, Qt.AlignLeft)
        lay.addStretch(1)
        return page

    def _refresh_overlay_channels(self, available):
        current = self.overlay_combo.currentData() or "temp"
        self.overlay_combo.blockSignals(True)
        self.overlay_combo.clear()
        self.overlay_combo.addItem(self.tr("Every channel shown"), None)
        meta = _state_meta()
        for name in available:
            self.overlay_combo.addItem((meta.get(name) or {}).get("label") or name, name)
        index = self.overlay_combo.findData(current)
        self.overlay_combo.setCurrentIndex(max(0, index))
        self.overlay_combo.blockSignals(False)
        self.chart.overlay_channel = self.overlay_combo.currentData()

    def _set_mode(self, mode):
        self._mode = mode
        self._redraw_now()

    def _set_wall_clock(self, on):
        self._wall_clock = on
        self._redraw_now()

    def _apply_time_range(self):
        if self._x_origin is None:
            return
        start = self.from_edit.dateTime().toSecsSinceEpoch() - self._x_origin
        end = self.to_edit.dateTime().toSecsSinceEpoch() - self._x_origin
        if end > start:
            self.follow.setChecked(False)
            self.chart.auto_range_on_draw = False
            self.chart.plot.setXRange(float(start), float(end), padding=0)

    def _reset_time_range(self):
        self.follow.setChecked(True)
        self.chart.reset_view()
        self._update_range_fields()

    def _update_range_fields(self):
        if self._x_origin is None:
            return
        lo, hi = self.chart.current_x_range()
        for edit, x in ((self.from_edit, lo), (self.to_edit, hi)):
            edit.blockSignals(True)
            edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(self._x_origin + x)))
            edit.blockSignals(False)

    def _statistics_page(self):
        from app.analysis_panels import panel_page

        page, lay = panel_page()
        lay.addWidget(self._heading(self.tr("EXPERIMENT SO FAR")))
        self.kpi_values = self._value_grid(
            lay,
            [
                ("stage", self.tr("Stage")),
                ("stage_time", self.tr("In stage")),
                ("error", self.tr("Error now")),
                ("mae", self.tr("MAE")),
                ("overshoot", self.tr("Overshoot")),
                ("settling", self.tr("Settling")),
            ],
        )
        lay.addSpacing(10)
        lay.addWidget(self._heading(self.tr("ANOMALIES")))
        self.anomaly_label = QLabel("—")
        self.anomaly_label.setWordWrap(True)
        lay.addWidget(self.anomaly_label)
        lay.addStretch(1)
        return page

    def _info_page(self):
        """Builds the Info panel."""
        from app.analysis_panels import RunDetailsForm, panel_page

        page, lay = panel_page()
        lay.addWidget(self._heading(self.tr("CHAMBER NOW")))
        self.now_values = self._value_grid(
            lay,
            [
                ("chamber", self.tr("Chamber")),
                ("status", self.tr("Status")),
                ("temp", self.tr("Temperature")),
                ("setpoint", self.tr("Setpoint")),
                ("output", self.tr("Output")),
                ("pres", self.tr("Pressure")),
                ("pres_ref", self.tr("Pressure setpoint")),
                ("program", self.tr("Program")),
                ("elapsed", self.tr("Job time")),
                ("alarm", self.tr("Alarm")),
                ("samples", self.tr("Samples")),
            ],
        )
        lay.addSpacing(10)
        lay.addWidget(self._heading(self.tr("EXPERIMENT DETAILS")))
        hint = QLabel(self.tr("Saved with the experiment running now and the next ones."))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.details_form = RunDetailsForm()
        self.details_form.set_metadata(settings_service.load_run_metadata())
        self.details_form.edited.connect(lambda: self._details_note.clear())
        lay.addWidget(self.details_form)
        row = QHBoxLayout()
        update = QPushButton(self.tr("Update"))
        update.setObjectName("primaryButton")
        update.clicked.connect(self._save_details)
        self._details_note = QLabel()
        self._details_note.setProperty("muted", True)
        self._details_note.setWordWrap(True)
        row.addWidget(update)
        row.addWidget(self._details_note, 1)
        lay.addLayout(row)
        lay.addSpacing(6)
        self.notice_label = QLabel()
        self.notice_label.setObjectName("liveNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        lay.addWidget(self.notice_label)
        lay.addStretch(1)
        return page

    def _save_details(self):
        settings_service.save_run_metadata(self.details_form.metadata())
        self._details_note.setText(self.tr("Saved: recorded with the experiments from now on."))

    def _calendar_page(self):
        from app.analysis_panels import panel_page

        page, lay = panel_page()
        self.calendar = QCalendarWidget()
        self.calendar.setMaximumDate(QDate.currentDate())
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.calendar.setGridVisible(False)
        self.calendar.clicked.connect(self._on_day_chosen)
        lay.addWidget(self.calendar)
        hint = QLabel(self.tr("Pick a day to see the chamber's recording of it."))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        back = QPushButton(self.tr("Back to the experiment"))
        back.clicked.connect(lambda: self._show_window("job"))
        lay.addWidget(back)
        lay.addStretch(1)
        return page

    def _on_day_chosen(self, _date):
        self._show_window("day")
        self._on_window_changed()

    def _show_window(self, mode):
        self.window_combo.setCurrentIndex(self.window_combo.findData(mode))

    def _export_page(self):
        from app.analysis_panels import panel_page
        from app.export_panel import ExportPanel

        page, lay = panel_page()
        self.export_panel = ExportPanel(
            can_report=True, can_bundle=False, experiment_label=self.tr("What is shown")
        )
        self.export_panel.export_requested.connect(self._on_export_requested)
        self.export_panel.preview_requested.connect(self._on_export_preview)
        lay.addWidget(self.export_panel)
        lay.addStretch(1)
        return page

    def _on_panel_opened(self, key):
        if key == "export":
            self._prepare_export()
        elif key == "options":
            self._update_range_fields()

    def _toggle_assistant(self):
        chat = getattr(self.window(), "agent_chat", None)
        if chat is not None:
            chat.toggle()

    def _annotation_key(self):
        return f"live:{self._source.chamber['name']}" if self._source is not None else None

    def _on_annotation_committed(self, ann):
        from app.services import annotations_service

        key = self._annotation_key()
        self.tools.set_tool("inspect")
        if key is None or self._x_origin is None:
            return
        annotations_service.add_annotation(
            run_key=key,
            user_id=None,
            user_name="",
            x0=ann["x0"] + self._x_origin,
            x1=ann["x1"] + self._x_origin,
            label=ann["label"],
            color=ann["color"],
        )
        self._redraw_now()

    def _draw_markers(self):
        """Draws annotations, stages and anomalies."""
        origin = self._x_origin
        if origin is None:
            return
        if self.mk_annotations.isChecked() and self._annotation_key():
            from app.services import annotations_service

            with contextlib.suppress(Exception):
                for a in annotations_service.list_annotations(self._annotation_key()):
                    self.chart.add_x_annotation(
                        a["x0"] - origin, a["x1"] - origin, a["label"], a["color"]
                    )
        if self._analysis is None or self._analysis_start is None:
            return
        offset = self._analysis_start - origin
        if self.mk_stages.isChecked():
            for stage in self._analysis.run.stages:
                s = stage.segment
                self.chart.add_x_annotation(
                    s.start + offset,
                    max(s.end, s.start + 1.0) + offset,
                    s.kind,
                    STAGE_COLORS.get(s.kind, HMI["haze"]),
                )
        if self.mk_anomalies.isChecked():
            for a in self._analysis.anomalies:
                color = {"critical": HMI["alarm"], "warning": HMI["connecting"]}.get(
                    a.severity, HMI["steel"]
                )
                self.chart.add_vertical_marker(a.start + offset, f"⚠ {a.detail}", color)

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

    def _redraw_now(self, *_):
        self._window_drawn = None
        self._refresh()

    def _on_window_changed(self, *_):
        self._window_drawn = None
        self.chart.auto_range_on_draw = True
        self._refresh()

    def _tick(self):
        """Refreshes the tab while it is visible."""
        if self.isVisible():
            self._refresh()

    def _refresh(self, *_):
        source = self._source
        mode = self.window_combo.currentData()
        if source is not None and mode != "job":
            self._refresh_window(source, mode)
            return
        self.chart.set_clock_origin(None)
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
        self._update_now(state, len(samples))
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

    def _window_span(self, mode):
        now = time.time()
        if mode == "1h":
            return now - 3600, now, None
        if mode == "6h":
            return now - 6 * 3600, now, None
        if mode == "24h":
            return now - 24 * 3600, now, None
        if mode == "today":
            start = datetime.combine(date.today(), day_time()).timestamp()
            return start, now, None
        day = self.calendar.selectedDate().toPython()
        start = datetime.combine(day, day_time()).timestamp()
        return start, (datetime.combine(day, day_time()) + timedelta(days=1)).timestamp() - 1, day

    def window_data(self, chamber, start, end, day=None):
        """Returns a chamber's recording between two times."""
        if day is not None and day < date.today():
            key = (chamber, day)
            if key not in self._past_days:
                self._past_days = {key: day_recorder.read_range(chamber, start, end)}
            return self._past_days[key]
        recorder = self._recorder()
        if recorder is not None:
            return recorder.window(chamber, start, end)
        return day_recorder.read_range(chamber, start, end)

    def _refresh_window(self, source, mode):
        state = source.state()
        if state["connected"]:
            self._update_now(state)
        self.save_btn.setEnabled(False)
        name = source.chamber["name"]
        start, end, day = self._window_span(mode)
        drawn = (name, mode, day)
        if drawn == self._window_drawn and time.time() - self._window_drawn_at < WINDOW_REFRESH_S:
            return
        self._window_drawn, self._window_drawn_at = drawn, time.time()
        try:
            times, channels = self.window_data(name, start, end, day)
        except Exception as exc:
            self._show_idle(
                self.tr("The recording of {0} could not be read: {1}").format(name, exc)
            )
            return
        label = self.window_combo.currentText() if mode != "day" else f"{day:%Y-%m-%d}"
        status = state["status"].upper() if state["connected"] else self.tr("OFFLINE")
        self.status_label.setText(
            self.tr("{0} · {1}: {2} sample(s)").format(status, label, len(times))
        )
        if len(times) == 0:
            self.idle_label.setText(
                self.tr("Nothing was recorded from {0} in this window.").format(name)
            )
            self.chart.set_clock_origin(None)
            self.chart.draw({"columns": [], "points": []})
            return
        self.idle_label.setText("")
        self._draw_arrays(times, channels)

    def _draw_arrays(self, times, channels):
        """Returns the window of the day recording to draw."""
        step = max(1, math.ceil(len(times) / MAX_CHART_POINTS))
        available = [k for k in channels if k not in NOT_CHANNELS]
        needed = self._needed_columns(available)
        data = {c: np.asarray(channels[c][::step], dtype=float) for c in needed}
        self._plot(np.asarray(times[::step], dtype=float), data, available)

    def _draw(self, samples):
        """Returns the samples to draw."""
        step = max(1, math.ceil(len(samples) / MAX_CHART_POINTS))
        shown = samples[::step]
        available = [
            k for k, v in samples[-1].items() if k not in NOT_CHANNELS and _number(v) is not None
        ]
        needed = self._needed_columns(available)
        times = np.array([s["timestamp"] for s in shown], dtype=float)
        data = {
            c: np.array([np.nan if _number(s.get(c)) is None else s[c] for s in shown], dtype=float)
            for c in needed
        }
        self._plot(times, data, available)

    def _needed_columns(self, available):
        """Returns the channels to read."""
        from app.services import derived_variables_service as dv

        have = set(available)
        needed = {c for c in self.selected if c in have}
        for d in self._calculated_available(available):
            if d["name"] in self.selected:
                needed |= dv.required_channels(d) & have
        return sorted(needed)

    def _plot(self, times, data, available):
        """Draws the visible channels and markers."""
        from app.services import derived_variables_service as dv

        self._available = available
        self._refresh_channel_list(available)
        if not len(times):
            self.chart.draw({"columns": [], "points": []})
            return
        origin = float(times[0])
        series = dict(data)
        calculated = [
            d for d in self._calculated_available(available) if d["name"] in self.selected
        ]
        for d in calculated:
            with contextlib.suppress(Exception):
                series[d["name"]] = np.asarray(
                    dv.compute_series(d, data, times - origin), dtype=float
                )
        columns = [c for c in available if c in self.selected and c in series]
        columns += [d["name"] for d in calculated if d["name"] in series]
        points = []
        for i, t in enumerate(times):
            values = {}
            for c in columns:
                v = series[c][i]
                values[c] = float(v) if np.isfinite(v) else None
            points.append({"i": i, "t": float(t), "values": values})
        if self.follow.isChecked():
            self.chart.auto_range_on_draw = True
        self.chart.draw({"columns": columns, "points": points}, self._mode, colors=self.colors)
        self._x_origin = origin
        self.chart.set_clock_origin(origin if self._wall_clock else None)
        self._draw_markers()

    def show_notice(self, text, error=False):
        """Shows a message of the experiment recorder."""
        stamp = datetime.now().strftime("%H:%M")
        self.notice_label.setText(f"{stamp} {text}")
        self.notice_label.setProperty("error", bool(error))
        self.notice_label.style().unpolish(self.notice_label)
        self.notice_label.style().polish(self.notice_label)
        self.notice_label.setVisible(True)

    def _prepare_export(self):
        """Sets the Export panel to what is shown."""
        panel = self.export_panel
        chambers = sorted({d["chamber"] for d in day_recorder.list_days()})
        current = day_recorder.safe_name(self._source.chamber["name"]) if self._source else None
        if current and current not in chambers:
            chambers.append(current)
        start, end = self._shown_span()
        day = date.fromtimestamp(start) if start else date.today()
        panel.set_day(day, start, end)
        panel.set_chambers(sorted(chambers), current)

    def _shown_span(self):
        """Returns the time span the chart shows."""
        source = self._source
        if source is None:
            return None, None
        mode = self.window_combo.currentData()
        if mode != "job":
            start, end, _ = self._window_span(mode)
            return start, end
        samples, _ = source.samples()
        if not samples:
            return None, None
        return samples[0]["timestamp"], samples[-1]["timestamp"]

    def _export_target(self, range_):
        from app.export_panel import DAY, EXPERIMENT

        panel = self.export_panel
        if range_ == EXPERIMENT:
            start, end = self._shown_span()
            if start is None:
                raise run_export.ExportError(self.tr("Nothing is shown."))
            chamber = self._source.chamber["name"]
        else:
            start, end, chamber = panel.from_epoch(), panel.to_epoch(), panel.chamber()
        own = self._source is not None and day_recorder.safe_name(
            self._source.chamber["name"]
        ) == day_recorder.safe_name(chamber)
        window = None
        if own:
            chamber = self._source.chamber["name"]
            day = date.fromtimestamp(start) if range_ == DAY else None
            window = self.window_data(chamber, start, end, day)
        return run_export.recorded_export(chamber, start, end, window=window)

    def _on_export_preview(self, range_):
        from app.export_panel import show_data_preview

        try:
            target = self._export_target(range_)
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Preview"), str(exc))
            return
        self._preview_dialog = show_data_preview(self, target)

    def _on_export_requested(self, fmt_, range_):
        from pathlib import Path

        from PySide6.QtWidgets import QApplication, QFileDialog

        from app import common
        from app.export_panel import VIEW, extension, is_data, is_image

        ext = extension(fmt_)
        common.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        suggested = common.REPORTS_DIR / f"live_{datetime.now():%Y%m%d_%H%M%S}.{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Export"), str(suggested), f"{ext.upper()} (*.{ext})"
        )
        if not path:
            return
        if not path.lower().endswith("." + ext):
            path += "." + ext
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if is_image(fmt_) and range_ == VIEW:
                self.chart.export_view(path)
            else:
                target = self._export_target(range_ if range_ != VIEW else "experiment")
                if is_image(fmt_):
                    image, _, _ = run_export.run_chart(target, kind="temperature")
                    run_export.save_image(image, Path(path), ext)
                elif is_data(fmt_):
                    lo, hi = (None, None)
                    if range_ == VIEW:
                        lo, hi = self.chart.current_x_range()
                    run_export.write_data(target, Path(path), None, lo, hi, fmt_ == "txt")
                else:
                    run_export.write_report(target, Path(path), fmt_)
            error = None
        except Exception as exc:
            error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if error:
            QMessageBox.critical(self, self.tr("Export"), error)
            return
        self.side_bar.close_panel()
        QMessageBox.information(self, self.tr("Export"), self.tr("Saved to {0}").format(path))

    def _show_idle(self, text):
        self.status_label.setText("")
        self.idle_label.setText(text)
        self.save_btn.setEnabled(False)
        for widget in list(self.now_values.values()) + list(self.kpi_values.values()):
            widget.setText("—")
        self.chart.draw({"columns": [], "points": []})

    def _update_now(self, state, samples=None):
        """Updates the current values."""
        from app.services.pressure import chamber_unit, to_torr

        v = state["values"]
        output = _number(v.get("temp_u"))
        mode = ""
        if output is not None:
            mode = (
                self.tr(" heating") if output > 1 else (self.tr(" cooling") if output < -1 else "")
            )
        elapsed = _duration(time.time() - state["since"]) if state["since"] else "—"
        unit = chamber_unit()
        pres = to_torr(_number(v.get("pres")), unit)
        pres_ref = to_torr(_number(v.get("pres_ref")), unit)
        source = self._source
        values = {
            "chamber": f"{source.chamber['name']} ({source.address})" if source else "—",
            "status": state["status"],
            "temp": f"{fmt(_number(v.get('temp')), 2)} °C",
            "setpoint": f"{fmt(_number(v.get('temp_ref')), 2)} °C",
            "output": f"{fmt(output, 1)} %{mode}",
            "pres": f"{_sci(pres)} Torr" if pres else "—",
            "pres_ref": f"{_sci(pres_ref)} Torr" if pres_ref else "—",
            "program": state["program"] or "—",
            "elapsed": elapsed,
            "alarm": state["alarm"] or self.tr("none"),
            "samples": "—" if samples is None else str(samples),
        }
        for key, text in values.items():
            self.now_values[key].setText(text)
        temp, ref = _number(v.get("temp")), _number(v.get("temp_ref"))
        error = None if temp is None or ref is None else ref - temp
        self.kpi_values["error"].setText(f"{fmt(error, 2)} °C" if error is not None else "—")

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
    """A chamber PLC read over the PLC gate."""

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
        self._client()

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
        """Returns the current or last job's samples."""
        client = self._client()
        snap = client.snapshot()
        if snap.job_started_at:
            samples = client.history(since=snap.job_started_at - 0.001)
            if snap.job_ended_at:
                samples = [s for s in samples if s["timestamp"] <= snap.job_ended_at]
            if samples:
                return samples, True
        return client.history(since=time.time() - IDLE_WINDOW_S), False

    def test_profile(self):
        return None

    def after_save(self):
        pass


class _JsonSource:
    """A sample-feed chamber."""

    kind = "json"

    def __init__(self, session):
        self.session = session
        self.chamber = session.chamber
        self.key = ("json", self.chamber["id"])
        self.address = f"{self.chamber['host']}:{self.chamber['port']}"
        self.label = f"{self.chamber['name']} ({self.address})"

    def start(self):
        pass

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

    def test_profile(self):
        return self.session.session_test_profile_name

    def after_save(self):
        self.session.clear_buffer()


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _finite(value):
    return value is not None and math.isfinite(value)


def _sci(value):
    """Formats a pressure."""
    if value is None or not math.isfinite(value):
        return "—"
    if value and abs(value) < 0.01:
        mantissa, exponent = f"{value:.1E}".split("E")
        return f"{mantissa}·E{exponent}"
    return f"{value:.3g}"


def _duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
