"""Analysis tab of an opened run."""

import contextlib
import math
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import app.services.annotations_service as annotations_service
import app.services.data_service as data
import app.services.derived_variables_service as derived_variables_service
import app.services.settings_service as settings_service
from app import common
from app.chart_widget import ChartWidget
from app.common import COLORS, RULE_COLOR_OPTIONS
from app.services import run_export
from app.theme_colors import HMI

MAX_COMPARE_MENU_RUNS = 40

STAGE_COLORS = {"ramp": HMI["connecting"], "setpoint": HMI["cobalt"], "hold": HMI["live"]}
SEVERITY_COLORS = {"critical": HMI["alarm"], "warning": HMI["connecting"], "info": HMI["steel"]}


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _stat(value, precision=4):
    """Formats a statistic."""
    if not _finite(value):
        return "—"
    magnitude = abs(value)
    if magnitude != 0 and (magnitude < 1e-3 or magnitude >= 1e6):
        return f"{value:.2e}"
    digits = 1 if magnitude >= 100 else (3 if precision > 2 else precision)
    return f"{value:.{digits}f}"


def _duration(seconds):
    if not _finite(seconds):
        return "—"
    total = int(round(seconds))
    sign = "-" if total < 0 else ""
    hours, rest = divmod(abs(total), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{sign}{hours} h {minutes:02d} min"
    if minutes:
        return f"{sign}{minutes} min {secs:02d} s"
    return f"{sign}{secs} s"


def _percent(fraction):
    return f"{fraction * 100:.0f} %" if _finite(fraction) else "—"


def _pid_shares(pid):
    if not _finite(pid.share_p):
        return "—"
    return f"P {_percent(pid.share_p)} · I {_percent(pid.share_i)} · D {_percent(pid.share_d)}"


def _swatch_icon(color):
    pix = QPixmap(14, 14)
    pix.fill(QColor(color))
    return QIcon(pix)


def _make_table(headers, stretch_column):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeToContents)
    header.setSectionResizeMode(stretch_column, QHeaderView.Stretch)
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setHighlightSections(False)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setMinimumHeight(90)
    return table


def _fill_table(table, rows, right_from=1, colors=None):
    """Fills a table with rows and text colours."""
    table.setRowCount(len(rows))
    for r, cells in enumerate(rows):
        for c, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            item.setTextAlignment(
                (Qt.AlignRight if c >= right_from else Qt.AlignLeft) | Qt.AlignVCenter
            )
            if colors and (r, c) in colors:
                item.setForeground(QColor(colors[(r, c)]))
            table.setItem(r, c, item)


class FlowLayout(QLayout):
    """Lays its items out left to right, wrapping onto new rows like text."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect, test_only):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._gap
            if next_x - self._gap > area.right() and row_height > 0:
                x = area.x()
                y += row_height + self._gap
                next_x = x + hint.width() + self._gap
                row_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + m.bottom()


class _FlowBox(QWidget):
    """Holds a FlowLayout and keeps its minimum height equal to the wrapped rows."""

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_minimum()

    def event(self, event):
        handled = super().event(event)
        if event.type() == QEvent.LayoutRequest:
            self._update_minimum()
        return handled

    def _update_minimum(self):
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            needed = lay.heightForWidth(self.width())
            if needed != self.minimumHeight():
                self.setMinimumHeight(needed)


def _state_meta():
    """Returns the PLC states' labels and units."""
    try:
        from app.api import control_settings

        return control_settings.state_meta(control_settings.load()) or {}
    except Exception:
        return {}


class RunTabPage(QWidget):
    compare_changed = Signal(set)

    def __init__(self, run_key, all_runs, dark=True, current_user=None, parent=None):
        super().__init__(parent)
        self.run_key = run_key
        self.all_runs = all_runs
        self.active_run = run_key
        self.detail = None
        self.series = None
        self.selected_columns = set()
        self._channel_colors = {}
        self.compare_runs = {run_key}
        self.dark = dark
        self.current_user = current_user or {"id": None, "name": "Unknown", "email": ""}
        self._user_annotations = []
        self._var_rules = []
        self._rule_ch_combo = None
        self._derived_variables = []
        self._available_derived = []
        self._catalogue = None
        self._analysis = None
        self._build_ui()

    def _build_ui(self):
        """Builds the run's chart, rail and panels."""
        from app.analysis_panels import ChannelList, ChartTools
        from app.chart_side_bar import GAP, ChartSideBar

        self.setObjectName("analysisPage")
        self.setAttribute(Qt.WA_StyledBackground)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(GAP)

        self.title_label = QLabel(self.tr("Loading…"))
        self.title_label.setObjectName("analysisTitle")
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("analysisMeta")
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.meta_label)

        self.chart_mode = QComboBox(self)
        self.chart_mode.addItems(["line", "scatter"])
        self.chart_mode.setVisible(False)
        self.chart_mode.currentTextChanged.connect(self.refresh_chart)

        self.chart = ChartWidget()
        self.chart.plot.setMinimumHeight(160)
        self.chart.annotation_committed.connect(self._on_annotation_committed)
        chart_card = QFrame()
        chart_card.setObjectName("analysisChartCard")
        chart_lay = QVBoxLayout(chart_card)
        chart_lay.setContentsMargins(10, 8, 10, 8)
        chart_lay.setSpacing(6)
        chart_lay.addLayout(header)
        chart_lay.addWidget(self.chart, 1)

        self._annotations_dialog = self._make_dialog(
            self._build_annotations_panel(), self.tr("Annotations")
        )
        self._rules_dialog = self._make_dialog(self._build_rules_panel(), self.tr("Variable rules"))
        self._statistics_dialog = self._make_dialog(
            self._build_statistics_panel(), self.tr("Statistics")
        )

        self.side_bar = ChartSideBar(self)
        self.channel_list = ChannelList(manage=True)
        self.channel_list.toggled.connect(self._set_channel_active)
        self.channel_list.color_changed.connect(self._set_channel_color)
        self.channel_list.manage_requested.connect(self._open_derived_variables_dialog)
        self.side_bar.add_page("channels", self.tr("Channels"), "rail-channels", self.channel_list)
        self.side_bar.add_page("options", self.tr("Options"), "rail-options", self._options_page())
        stats = self.side_bar.add_action(self.tr("Statistics"), "rail-stats")
        stats.setToolTip(
            self.tr("Statistics of the visible range, run KPIs, stages, anomalies and comparison")
        )
        stats.clicked.connect(self._open_statistics)
        self.side_bar.add_page("info", self.tr("Info"), "rail-info", self._info_page())
        assistant = self.side_bar.add_action(self.tr("Assistant"), "rail-ai")
        assistant.setToolTip(self.tr("Chat with the Deepvac Agents"))
        assistant.clicked.connect(self._toggle_assistant)
        self.side_bar.add_page(
            "export", self.tr("Export"), "rail-export", self._export_page(), bottom=True
        )
        self.tools = ChartTools(self.side_bar, self.chart, annotate=True)
        self.side_bar.set_tools(
            self.tr("Chart tools"), self.tr("Tools"), "rail-pointer", self.tools
        )
        self.side_bar.opened.connect(self._on_panel_opened)

        root.addWidget(self.side_bar)
        root.addWidget(chart_card, 1)

        self._statistics_timer = QTimer(self)
        self._statistics_timer.setSingleShot(True)
        self._statistics_timer.setInterval(250)
        self._statistics_timer.timeout.connect(self._refresh_range_statistics)
        self.chart.plot.getViewBox().sigXRangeChanged.connect(
            lambda *_: self._statistics_timer.start()
        )

        self.chart.set_dark(self.dark)
        self._update_tool_icons()

    def _update_tool_icons(self):
        self.side_bar.set_icon_color(HMI["sky"] if self.dark else HMI["cobalt"])

    def _toggle_assistant(self):
        chat = getattr(self.window(), "agent_chat", None)
        if chat is not None:
            chat.toggle()

    def _on_panel_opened(self, key):
        if key == "export":
            self._prepare_export()
        elif key == "info":
            self._refresh_info()

    def _options_page(self):
        from app.analysis_panels import add_section, caption, link_button, panel_page, segments

        page, lay = panel_page()

        def time_spin():
            spin = QDoubleSpinBox()
            spin.setRange(0, 1_000_000)
            spin.setDecimals(1)
            spin.setSuffix(" s")
            return spin

        rng = add_section(lay, self.tr("Time range"))
        self.time_start = time_spin()
        self.time_end = time_spin()
        rng.addWidget(caption(self.tr("From")))
        rng.addWidget(self.time_start)
        rng.addWidget(caption(self.tr("To")))
        rng.addWidget(self.time_end)
        row = QHBoxLayout()
        apply_time = QPushButton(self.tr("Apply"))
        apply_time.setObjectName("primaryButton")
        apply_time.setToolTip(self.tr("Show only the selected time range"))
        apply_time.clicked.connect(self._apply_time_range)
        whole = link_button(self.tr("Reset to whole range"))
        whole.clicked.connect(self._reset_time_range)
        row.addWidget(apply_time)
        row.addWidget(whole)
        row.addStretch(1)
        rng.addLayout(row)

        display = add_section(lay, self.tr("Display"))
        display.addWidget(caption(self.tr("Smoothing")))
        self.smoothing = QComboBox()
        self.smoothing.addItem(self.tr("Off"), 1)
        for samples in (3, 5, 11, 21, 51, 101, 201):
            self.smoothing.addItem(self.tr("{0} samples").format(samples), samples)
        self.smoothing.setToolTip(self.tr("Moving average window"))
        self.smoothing.currentIndexChanged.connect(
            lambda *_: self._set_smoothing(self.smoothing.currentData())
        )
        display.addWidget(self.smoothing)
        display.addWidget(caption(self.tr("Draw as")))
        draw_row, (lines_btn, points_btn) = segments([self.tr("Lines"), self.tr("Points")])
        lines_btn.setChecked(True)
        lines_btn.clicked.connect(lambda: self.chart_mode.setCurrentText("line"))
        points_btn.clicked.connect(lambda: self.chart_mode.setCurrentText("scatter"))
        display.addLayout(draw_row)

        markers = add_section(lay, self.tr("Markers"))
        row1, (self.mk_events, self.mk_alarms, self.mk_ctrl) = segments(
            [self.tr("Events"), self.tr("Alarms"), self.tr("Controller")], exclusive=False
        )
        row2, (self.mk_state, self.mk_stages, self.mk_anomalies) = segments(
            [self.tr("State"), self.tr("Stages"), self.tr("Anomalies")], exclusive=False
        )
        for button in (self.mk_events, self.mk_alarms, self.mk_ctrl, self.mk_state):
            button.setChecked(True)
            button.toggled.connect(self._update_markers)
        self.mk_stages.setToolTip(self.tr("Show the automatic ramp / setpoint / hold stages"))
        self.mk_anomalies.setToolTip(self.tr("Mark the detected anomalies on the chart"))
        for button in (self.mk_stages, self.mk_anomalies):
            button.toggled.connect(self.refresh_chart)
        markers.addLayout(row1)
        markers.addLayout(row2)

        overlays = add_section(lay, self.tr("Reference lines"))
        self.overlay_combo = QComboBox()
        self.overlay_combo.addItem(self.tr("Every channel shown"), None)
        self.overlay_combo.currentIndexChanged.connect(
            lambda *_: self.chart.set_overlay_channel(self.overlay_combo.currentData())
        )
        overlays.addWidget(self.overlay_combo)
        overlays.addWidget(caption(self.tr("Lines at")))
        ref_row, (self.overlay_min, self.overlay_max, self.overlay_avg) = segments(
            [self.tr("Min"), self.tr("Max"), self.tr("Average")], exclusive=False
        )
        for button in (self.overlay_min, self.overlay_max, self.overlay_avg):
            button.toggled.connect(self._update_overlays)
        overlays.addLayout(ref_row)

        compare = add_section(lay, self.tr("Comparison"))
        self._compare_btn = QPushButton(self.tr("Compare"))
        self._compare_btn.setToolTip(self.tr("Overlay the first selected channel of other runs"))
        self._compare_btn.clicked.connect(self._open_compare_menu)
        compare.addWidget(self._compare_btn)
        self.show_setpoint = QCheckBox(self.tr("Setpoint line at"))
        self.show_setpoint.setChecked(True)
        self.show_setpoint.toggled.connect(self.refresh_chart)
        self.setpoint_value = QLineEdit("0")
        self.setpoint_value.textChanged.connect(self.refresh_chart)
        self._setpoint_group = QWidget()
        self._setpoint_group.setObjectName("sidePage")
        sp = QHBoxLayout(self._setpoint_group)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.addWidget(self.show_setpoint)
        sp.addWidget(self.setpoint_value, 1)
        self._setpoint_group.setVisible(False)
        compare.addWidget(self._setpoint_group)

        notes = add_section(lay, self.tr("Annotations and rules"))
        ann = link_button(self.tr("Annotations…"))
        ann.clicked.connect(lambda: self._show_dialog(self._annotations_dialog))
        rules = link_button(self.tr("Variable rules…"))
        rules.clicked.connect(lambda: self._show_dialog(self._rules_dialog))
        notes.addWidget(ann, 0, Qt.AlignLeft)
        notes.addWidget(rules, 0, Qt.AlignLeft)
        lay.addStretch(1)
        return page

    def _info_page(self):
        from app.analysis_panels import RunDetailsForm, panel_page

        page, lay = panel_page()
        self._info_title = QLabel()
        self._info_title.setObjectName("infoTitle")
        self._info_title.setWordWrap(True)
        lay.addWidget(self._info_title)
        self.details_form = RunDetailsForm()
        self.details_form.edited.connect(lambda: self._details_note.clear())
        lay.addWidget(self.details_form)
        row = QHBoxLayout()
        self._details_update = QPushButton(self.tr("Update"))
        self._details_update.setObjectName("primaryButton")
        self._details_update.clicked.connect(self._save_details)
        self._details_note = QLabel()
        self._details_note.setProperty("muted", True)
        self._details_note.setWordWrap(True)
        row.addWidget(self._details_update)
        row.addWidget(self._details_note, 1)
        lay.addLayout(row)
        line = QFrame()
        line.setObjectName("cardDivider")
        line.setFixedHeight(1)
        lay.addWidget(line)
        self._info_rows = QLabel()
        self._info_rows.setWordWrap(True)
        self._info_rows.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._info_rows)
        lay.addStretch(1)
        return page

    def _refresh_info(self):
        from app.analysis_panels import info_rows

        run = (self.detail or {}).get("run", {})
        self._info_title.setText(run.get("id", self.run_key))
        try:
            self.details_form.set_metadata(data.run_metadata(self.run_key))
            self._details_update.setEnabled(True)
        except Exception:
            self.details_form.set_metadata({})
            self._details_update.setEnabled(False)
        self._details_note.clear()
        start, end = self._run_span()

        def clock(epoch):
            from datetime import datetime

            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch else None

        self._info_rows.setText(
            info_rows(
                [
                    (self.tr("Run"), self.run_key),
                    (self.tr("Chamber"), run.get("chamber")),
                    (self.tr("Test profile"), run.get("test_profile")),
                    (self.tr("Start"), clock(start) or run.get("start_time")),
                    (self.tr("End"), clock(end) or run.get("end_time")),
                    (
                        self.tr("Duration"),
                        _duration(run["duration_s"]) if run.get("duration_s") else None,
                    ),
                    (self.tr("Samples"), run.get("samples")),
                    (self.tr("Channels"), len((self.detail or {}).get("numeric_columns", []))),
                ]
            )
        )

    def _save_details(self):
        try:
            data.update_run_metadata(self.run_key, self.details_form.metadata())
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Experiment details"), str(exc))
            return
        self._details_note.setText(self.tr("Saved. The report and the agents use these details."))

    def _export_page(self):
        from app.analysis_panels import panel_page
        from app.export_panel import ExportPanel

        page, lay = panel_page()
        self.export_panel = ExportPanel(can_report=True, can_bundle=True)
        self.export_panel.export_requested.connect(self._on_export_requested)
        self.export_panel.preview_requested.connect(self._on_export_preview)
        lay.addWidget(self.export_panel)
        lay.addStretch(1)
        return page

    def _make_dialog(self, panel, title):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setProperty("baseTitle", title)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.addWidget(panel)
        return dialog

    def _show_dialog(self, dialog):
        """Opens over the main window, most of its size."""
        if not dialog.isVisible():
            area = self.window().geometry()
            dialog.resize(int(area.width() * 0.85), int(area.height() * 0.8))
            dialog.move(area.center() - QPoint(dialog.width() // 2, dialog.height() // 2))
        run_id = (self.detail or {}).get("run", {}).get("id", self.run_key)
        dialog.setWindowTitle(f"{dialog.property('baseTitle')} · {run_id}")
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def load(self):
        try:
            self.detail = data.run_detail(self.run_key)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Load error"), str(exc))
            return
        if not any(c in self.detail["numeric_columns"] for c in self.selected_columns):
            numeric_cols = self.detail["numeric_columns"]
            saved = [c for c in settings_service.load_channels(self.run_key) if c in numeric_cols]
            if saved:
                self.selected_columns = set(saved)
            else:
                preferred = [c for c in ["temp", "temp_ref"] if c in numeric_cols]
                self.selected_columns = set(preferred or numeric_cols[:3])
        run = self.detail.get("run", {})
        self.title_label.setText(run.get("id", self.run_key))
        self.title_label.setToolTip(run.get("id", self.run_key))
        self._user_annotations = annotations_service.list_annotations(self.run_key)
        self._var_rules = annotations_service.list_rules(self.run_key)
        self._analysis = None
        self._load_derived_variables()
        self._render_summary()
        self._render_channels()
        self._refresh_annotations_list()
        self._refresh_rules_list()
        self._load_series()

    def _load_derived_variables(self):
        try:
            self._derived_variables = derived_variables_service.list_derived_variables()
        except Exception:
            self._derived_variables = []
        numeric_cols = set((self.detail or {}).get("numeric_columns", []))
        self._available_derived = [
            d
            for d in self._derived_variables
            if not derived_variables_service.missing_channels(d, numeric_cols)
        ]
        available_names = {d["name"] for d in self._available_derived}
        real_and_available = set(self.detail.get("numeric_columns", [])) | available_names
        self.selected_columns = {c for c in self.selected_columns if c in real_and_available}

    def set_compare_run(self, key, checked):
        if checked:
            self.compare_runs.add(key)
        else:
            self.compare_runs.discard(key)
        if not self.compare_runs:
            self.compare_runs.add(self.run_key)
        self._update_compare_ui()
        self._load_series()
        self.compare_changed.emit(self.compare_runs)
        if self._statistics_dialog.isVisible():
            self._refresh_statistics()

    def update_theme(self, dark):
        self.dark = dark
        self.chart.set_dark(dark)
        self._update_tool_icons()

    def primary_chart(self):
        return self.chart

    def _compare_mode(self):
        return len(self.compare_runs) > 1

    def _first_col(self):
        return next(iter(self.selected_columns), None)

    def _active_charts(self):
        return [self.chart]

    def _render_summary(self):
        run = (self.detail or {}).get("run", {})
        parts = []
        if run.get("samples") is not None:
            parts.append(self.tr("{0} samples").format(run["samples"]))
        if run.get("duration_s") is not None:
            parts.append(_duration(run["duration_s"]))
        self.meta_label.setText("  ·  ".join(parts))

    def _render_channels(self):
        if not self.detail:
            return
        cols = self.detail.get("numeric_columns", [])
        from app.live_tab import DEFAULT_COLORS

        for i, col in enumerate(cols):
            if col not in self._channel_colors:
                self._channel_colors[col] = DEFAULT_COLORS.get(col) or COLORS[i % len(COLORS)]
        for i, d in enumerate(self._available_derived):
            if d["name"] not in self._channel_colors:
                self._channel_colors[d["name"]] = d.get("color") or COLORS[i % len(COLORS)]
        available_names = set(cols) | {d["name"] for d in self._available_derived}
        self.selected_columns = {c for c in self.selected_columns if c in available_names}
        if not self.selected_columns:
            preferred = [c for c in ["temp", "temp_ref"] if c in cols]
            self.selected_columns = set(preferred or cols[:2])
        self._update_ch_btn_label()
        self._update_rule_channel_combo()

    def _update_ch_btn_label(self):
        """Updates the Channels panel."""
        cols = (self.detail or {}).get("numeric_columns", [])
        meta = _state_meta()
        self.channel_list.set_channels(
            [
                {
                    "key": col,
                    "label": (meta.get(col) or {}).get("label") or col,
                    "unit": (meta.get(col) or {}).get("unit") or "",
                    "color": self._channel_colors.get(col, COLORS[0]),
                    "checked": col in self.selected_columns,
                    "tooltip": col,
                }
                for col in cols
            ],
            [
                {
                    "key": d["name"],
                    "label": d["name"],
                    "color": self._channel_colors.get(d["name"], COLORS[0]),
                    "checked": d["name"] in self.selected_columns,
                    "tooltip": self._derived_tooltip(d),
                }
                for d in self._available_derived
            ],
        )
        self.side_bar.set_subtitle("channels", self.channel_list.shown_text())
        current = self.overlay_combo.currentData()
        self.overlay_combo.blockSignals(True)
        while self.overlay_combo.count() > 1:
            self.overlay_combo.removeItem(1)
        for col in cols:
            self.overlay_combo.addItem((meta.get(col) or {}).get("label") or col, col)
        self.overlay_combo.setCurrentIndex(max(0, self.overlay_combo.findData(current)))
        self.overlay_combo.blockSignals(False)

    def _update_compare_ui(self):
        others = len(self.compare_runs) - 1
        self._compare_btn.setText(
            self.tr("Compare ({0})").format(others) if others else self.tr("Compare")
        )
        self._setpoint_group.setVisible(self._compare_mode())

    def _open_compare_menu(self):
        menu = QMenu(self)
        others = [r for r in self.all_runs if r.get("key") != self.run_key]
        if not others:
            menu.addAction(self.tr("No other runs to compare")).setEnabled(False)
        for run in others[:MAX_COMPARE_MENU_RUNS]:
            act = menu.addAction(run.get("id", run["key"]))
            act.setCheckable(True)
            act.setChecked(run["key"] in self.compare_runs)
            act.toggled.connect(lambda checked, k=run["key"]: self.set_compare_run(k, checked))
        if len(others) > MAX_COMPARE_MENU_RUNS:
            more = menu.addAction(
                self.tr("{0} more: tick them in the runs list").format(
                    len(others) - MAX_COMPARE_MENU_RUNS
                )
            )
            more.setEnabled(False)
        if self._compare_mode():
            menu.addSeparator()
            menu.addAction(self.tr("Clear comparison"), self._clear_comparison)
        menu.exec(self._compare_btn.mapToGlobal(self._compare_btn.rect().bottomLeft()))

    def _clear_comparison(self):
        for key in [k for k in self.compare_runs if k != self.run_key]:
            self.compare_runs.discard(key)
        self.set_compare_run(self.run_key, True)

    def _open_derived_variables_dialog(self):
        from app.derived_variables_dialog import DerivedVariablesDialog

        numeric_cols = (self.detail or {}).get("numeric_columns", [])
        dlg = DerivedVariablesDialog(numeric_cols, current_user=self.current_user, parent=self)
        dlg.exec()
        if dlg.changed:
            self._load_derived_variables()
            self._render_channels()
            self._load_series()

    def _derived_tooltip(self, definition):
        kind = {
            derived_variables_service.TYPE_DIFFERENCE: self.tr("difference"),
            derived_variables_service.TYPE_RATE_OF_CHANGE: self.tr("rate of change"),
            derived_variables_service.TYPE_ROLLING_STD: self.tr("rolling std. dev."),
            derived_variables_service.TYPE_CUMULATIVE_INTEGRAL: self.tr("cumulative integral"),
            derived_variables_service.TYPE_CUSTOM: self.tr("custom expression"),
        }.get(definition["type"], definition["type"])
        return f"{kind}: {definition.get('expression') or definition.get('source_channel', '')}"

    def _set_channel_active(self, col, active):
        if active:
            self.selected_columns.add(col)
        else:
            self.selected_columns.discard(col)
        self.side_bar.set_subtitle("channels", self.channel_list.shown_text())
        self._load_series()
        settings_service.save_channels(self.run_key, sorted(self.selected_columns))
        if self._statistics_dialog.isVisible():
            self._refresh_range_statistics()

    def _set_channel_color(self, col, color):
        self._channel_colors[col] = color
        self._update_ch_btn_label()
        self.refresh_chart()

    def _load_series(self):
        cols = list(self.selected_columns)
        if not cols:
            self.series = {"columns": [], "points": []}
            self.refresh_chart()
            return
        try:
            if self._compare_mode():
                ch = self._first_col()
                series = []
                for key in self.compare_runs:
                    payload = data.run_series(key, [ch])
                    run = next((r for r in self.all_runs if r["key"] == key), None)
                    series.append({"label": run["id"] if run else key, "points": payload["points"]})
                self.series = {"series": series, "channel": ch}
            else:
                self.series = self._load_series_with_derived(cols)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Series error"), str(exc))
            return
        self.refresh_chart()

    def _load_series_with_derived(self, cols):
        real_available = set(self.detail.get("numeric_columns", []))
        derived_by_name = {d["name"]: d for d in self._available_derived}
        selected_real = [c for c in cols if c in real_available]
        selected_derived = [derived_by_name[c] for c in cols if c in derived_by_name]

        needed_real = set(selected_real)
        for d in selected_derived:
            needed_real |= derived_variables_service.required_channels(d) & real_available
        if not needed_real:
            return {"columns": [], "points": []}

        payload = data.run_series(self.run_key, sorted(needed_real))

        if selected_derived:
            self._inject_derived_series(payload, needed_real, selected_derived)

        displayed = {c for c in selected_real} | {d["name"] for d in selected_derived}
        payload["columns"] = [c for c in cols if c in displayed]
        return payload

    def _inject_derived_series(self, payload, needed_real, selected_derived):
        import numpy as np

        points = payload["points"]
        raw_t = [p.get("t") for p in points]
        first_t = next((t for t in raw_t if isinstance(t, (int, float))), None)
        elapsed = np.array(
            [
                (t - first_t) if isinstance(t, (int, float)) and first_t is not None else float(i)
                for i, t in enumerate(raw_t)
            ],
            dtype=float,
        )
        columns_data = {
            col: np.array(
                [
                    v if isinstance((v := p["values"].get(col)), (int, float)) else np.nan
                    for p in points
                ],
                dtype=float,
            )
            for col in needed_real
        }
        for definition in selected_derived:
            try:
                computed = derived_variables_service.compute_series(
                    definition, columns_data, elapsed
                )
            except derived_variables_service.DerivedVariableError:
                continue
            for point, value in zip(points, computed, strict=False):
                point["values"][definition["name"]] = None if value != value else float(value)

    def refresh_chart(self):
        setpoint = None
        if self._compare_mode() and self.show_setpoint.isChecked():
            with contextlib.suppress(ValueError):
                setpoint = float(self.setpoint_value.text())
        annotations = [] if self._compare_mode() else (self.detail or {}).get("annotations", [])
        for ch in self._active_charts():
            ch.draw(
                self.series,
                self.chart_mode.currentText(),
                annotations,
                setpoint,
                colors=self._channel_colors,
            )
        self._draw_rules_on_chart()
        self._draw_user_annotations()
        self._draw_analysis_markers()
        self._update_time_controls()

    def _update_time_controls(self):
        start, end = self.chart.data_x_range()
        if end <= start:
            return
        for sb in [self.time_start, self.time_end]:
            sb.blockSignals(True)
            sb.setRange(start, end)
        self.time_start.setValue(start)
        self.time_end.setValue(end)
        for sb in [self.time_start, self.time_end]:
            sb.blockSignals(False)

    def _apply_time_range(self):
        for ch in self._active_charts():
            ch.set_time_range(self.time_start.value(), self.time_end.value())

    def _reset_time_range(self):
        for ch in self._active_charts():
            ch.set_time_range(None, None)
        self._update_time_controls()

    def _zoom_to(self, start, end):
        """Zooms to a time range."""
        self.chart.region.setVisible(False)
        self.chart.auto_range_on_draw = False
        self.chart.plot.setXRange(float(start), float(end), padding=0)

    def _set_smoothing(self, value):
        value = int(value or 1)
        for ch in self._active_charts():
            ch.set_smoothing_window(value)

    def _update_overlays(self):
        for ch in self._active_charts():
            ch.set_overlay_flags(
                min=self.overlay_min.isChecked(),
                max=self.overlay_max.isChecked(),
                avg=self.overlay_avg.isChecked(),
            )

    def _update_markers(self):
        for ch in self._active_charts():
            ch.set_marker_flags(
                events=self.mk_events.isChecked(),
                alarms=self.mk_alarms.isChecked(),
                controller=self.mk_ctrl.isChecked(),
                state=self.mk_state.isChecked(),
            )

    def _reset_views(self):
        for ch in self._active_charts():
            ch.reset_view()
        self._update_time_controls()

    def _on_annotation_committed(self, ann: dict):
        saved = annotations_service.add_annotation(
            run_key=self.run_key,
            user_id=self.current_user.get("id"),
            user_name=self.current_user.get("name", "Unknown"),
            x0=ann["x0"],
            x1=ann["x1"],
            label=ann["label"],
            color=ann["color"],
        )
        self._user_annotations.append(saved)
        self.tools.set_tool("inspect")
        self._refresh_annotations_list()
        self.refresh_chart()

    def _delete_user_annotation(self, idx: int):
        if 0 <= idx < len(self._user_annotations):
            ann = self._user_annotations.pop(idx)
            annotations_service.delete_annotation(ann["id"])
        self._refresh_annotations_list()
        self.refresh_chart()

    def _build_annotations_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        hint = QLabel(
            self.tr(
                "Annotations of this run. To add one, press Annotate and drag across the chart."
            )
        )
        hint.setObjectName("analysisMeta")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._ann_table = _make_table(
            [
                "",
                self.tr("LABEL"),
                self.tr("FROM"),
                self.tr("TO"),
                self.tr("DURATION"),
                self.tr("BY"),
            ],
            1,
        )
        self._ann_table.verticalHeader().setDefaultSectionSize(30)
        lay.addWidget(self._ann_table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        show_btn = QPushButton(self.tr("Show on chart"))
        remove_btn = QPushButton(self.tr("Remove"))
        close_btn = QPushButton(self.tr("Close"))
        close_btn.setObjectName("primaryButton")
        for b in (show_btn, remove_btn, close_btn):
            b.setMinimumWidth(110)
        buttons.addWidget(show_btn)
        buttons.addWidget(remove_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        lay.addLayout(buttons)

        def selected_row():
            rows = self._ann_table.selectionModel().selectedRows()
            return rows[0].row() if rows else -1

        def update_buttons():
            any_selected = selected_row() >= 0
            show_btn.setEnabled(any_selected)
            remove_btn.setEnabled(any_selected)

        def show_on_chart(row=None):
            row = selected_row() if row is None else row
            if not 0 <= row < len(self._user_annotations):
                return
            ann = self._user_annotations[row]
            margin = max(30.0, 0.15 * (ann["x1"] - ann["x0"]))
            self._zoom_to(max(0.0, ann["x0"] - margin), ann["x1"] + margin)

        def remove():
            row = selected_row()
            if not 0 <= row < len(self._user_annotations):
                return
            label = self._user_annotations[row].get("label", "")
            answer = QMessageBox.question(
                self,
                self.tr("Remove annotation"),
                self.tr('Remove the annotation "{0}"?').format(label),
            )
            if answer == QMessageBox.Yes:
                self._delete_user_annotation(row)

        self._ann_table.itemSelectionChanged.connect(update_buttons)
        self._ann_table.cellDoubleClicked.connect(lambda row, _col: show_on_chart(row))
        show_btn.clicked.connect(lambda: show_on_chart())
        remove_btn.clicked.connect(remove)
        close_btn.clicked.connect(lambda: panel.window().close())
        update_buttons()
        return panel

    def _refresh_annotations_list(self):
        table = self._ann_table
        table.setRowCount(len(self._user_annotations))
        for row, ann in enumerate(self._user_annotations):
            cells = [
                QTableWidgetItem(_swatch_icon(ann.get("color", "#60a5fa")), ""),
                QTableWidgetItem(ann.get("label", "")),
                QTableWidgetItem(_duration(ann["x0"])),
                QTableWidgetItem(_duration(ann["x1"])),
                QTableWidgetItem(_duration(ann["x1"] - ann["x0"])),
                QTableWidgetItem(ann.get("user_name", "Unknown")),
            ]
            cells[2].setToolTip(f"{ann['x0']:.1f} s")
            cells[3].setToolTip(f"{ann['x1']:.1f} s")
            for col, item in enumerate(cells):
                table.setItem(row, col, item)

    def _draw_user_annotations(self):
        for ann in self._user_annotations:
            self.chart.add_x_annotation(ann["x0"], ann["x1"], ann["label"], ann["color"])

    def _build_rules_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        hint = QLabel(
            self.tr(
                "Highlight acceptable ranges of a channel on the chart. Values outside them "
                "are reported as anomalies."
            )
        )
        hint.setObjectName("analysisMeta")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._rule_ch_combo = QComboBox()
        self._rule_ch_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._rule_ch_combo.setMinimumContentsLength(16)
        self._rule_lo_ed = QLineEdit()
        self._rule_lo_ed.setPlaceholderText(self.tr("min"))
        self._rule_hi_ed = QLineEdit()
        self._rule_hi_ed.setPlaceholderText(self.tr("max"))
        self._rule_name_ed = QLineEdit()
        self._rule_name_ed.setPlaceholderText(self.tr("label"))
        for ed in (self._rule_lo_ed, self._rule_hi_ed, self._rule_name_ed):
            ed.setFixedWidth(100)
        self._rule_color_combo = QComboBox()
        for name, color in RULE_COLOR_OPTIONS:
            self._rule_color_combo.addItem(_swatch_icon(color), self.tr(name))
        add_btn = QPushButton(self.tr("Add rule"))
        add_btn.setObjectName("primaryButton")
        add_btn.clicked.connect(self._add_var_rule)

        form = FlowLayout(spacing=6)
        for w in (
            self._rule_ch_combo,
            self._rule_lo_ed,
            self._rule_hi_ed,
            self._rule_name_ed,
            self._rule_color_combo,
            add_btn,
        ):
            form.addWidget(w)
        form_box = _FlowBox()
        form_box.setLayout(form)
        lay.addWidget(form_box)

        list_row = QHBoxLayout()
        self._rules_list = QListWidget()
        self._rules_list.setMinimumHeight(100)
        remove_btn = QPushButton(self.tr("Remove"))
        remove_btn.clicked.connect(self._remove_selected_rule)
        list_row.addWidget(self._rules_list, 1)
        list_row.addWidget(remove_btn, 0, Qt.AlignTop)
        lay.addLayout(list_row, 1)
        return panel

    def _update_rule_channel_combo(self):
        if not self._rule_ch_combo:
            return
        current = self._rule_ch_combo.currentText()
        self._rule_ch_combo.blockSignals(True)
        self._rule_ch_combo.clear()
        for col in (self.detail or {}).get("numeric_columns", []):
            self._rule_ch_combo.addItem(col)
        idx = self._rule_ch_combo.findText(current)
        if idx >= 0:
            self._rule_ch_combo.setCurrentIndex(idx)
        self._rule_ch_combo.blockSignals(False)

    def _add_var_rule(self):
        ch = self._rule_ch_combo.currentText() if self._rule_ch_combo else ""
        if not ch:
            return
        lo_text = self._rule_lo_ed.text().strip().replace(",", ".")
        hi_text = self._rule_hi_ed.text().strip().replace(",", ".")
        try:
            lo = float(lo_text) if lo_text else None
            hi = float(hi_text) if hi_text else None
        except ValueError:
            return
        if lo is None and hi is None:
            return
        _, color = RULE_COLOR_OPTIONS[self._rule_color_combo.currentIndex()]
        saved = annotations_service.add_rule(
            run_key=self.run_key,
            user_id=self.current_user.get("id"),
            user_name=self.current_user.get("name", "Unknown"),
            name=self._rule_name_ed.text().strip() or ch,
            channel=ch,
            lo=lo,
            hi=hi,
            color=color,
        )
        self._var_rules.append(saved)
        self._rule_lo_ed.clear()
        self._rule_hi_ed.clear()
        self._rule_name_ed.clear()
        self._rules_changed()

    def _remove_selected_rule(self):
        item = self._rules_list.currentItem()
        if item is not None:
            self._remove_var_rule(item.data(Qt.UserRole))

    def _remove_var_rule(self, idx):
        if 0 <= idx < len(self._var_rules):
            rule = self._var_rules.pop(idx)
            annotations_service.delete_rule(rule["id"])
        self._rules_changed()

    def _rules_changed(self):
        self._analysis = None
        self._refresh_rules_list()
        self.refresh_chart()
        if self._statistics_dialog.isVisible():
            self._refresh_statistics()

    def _refresh_rules_list(self):
        self._rules_list.clear()
        for idx, rule in enumerate(self._var_rules):
            lo_str = f"{rule['lo']:g}" if rule["lo"] is not None else "−∞"
            hi_str = f"{rule['hi']:g}" if rule["hi"] is not None else "+∞"
            item = QListWidgetItem(
                _swatch_icon(rule.get("color", "#60a5fa")),
                f"{rule['name']}  ·  {rule['channel']}  [{lo_str} … {hi_str}]  ·  "
                f"{rule.get('user_name', 'Unknown')}",
            )
            item.setData(Qt.UserRole, idx)
            self._rules_list.addItem(item)

    def _draw_rules_on_chart(self):
        for rule in self._var_rules:
            if rule["channel"] not in self.selected_columns:
                continue
            lo = rule.get("lo")
            hi = rule.get("hi")
            color = rule.get("color", "#60a5fa")
            name = rule.get("name", "")
            if lo is not None and hi is not None:
                self.chart.add_range_band(lo, hi, color, name)
            elif lo is not None:
                self.chart.add_horizontal_marker(
                    lo, self.tr("{0} min").format(name), color, overlay=True
                )
            elif hi is not None:
                self.chart.add_horizontal_marker(
                    hi, self.tr("{0} max").format(name), color, overlay=True
                )

    def _run_data(self, key):
        """Returns the run's input and analysis."""
        from app.api.runs import RunCatalogue

        if self._catalogue is None:
            self._catalogue = RunCatalogue()
        return self._catalogue.load(self._catalogue.entry_for_key(key))

    def _own_analysis(self):
        """Returns the run's analysis, or None."""
        if self._analysis is None:
            from app.core.analysis import ExperimentInput, analyse_experiment
            from app.core.analysis.anomalies import LimitRule
            from app.core.analysis.common import NaN

            try:
                stored, _ = self._run_data(self.run_key)
            except Exception:
                return None
            rules = [
                LimitRule(
                    r["channel"],
                    NaN if r["lo"] is None else r["lo"],
                    NaN if r["hi"] is None else r["hi"],
                    r["name"],
                )
                for r in self._var_rules
            ]
            self._analysis = analyse_experiment(
                ExperimentInput(stored.times, stored.channels, rules)
            )
        return self._analysis

    def _draw_analysis_markers(self):
        if self._compare_mode():
            return
        if not (self.mk_stages.isChecked() or self.mk_anomalies.isChecked()):
            return
        analysis = self._own_analysis()
        if analysis is None:
            return
        if self.mk_stages.isChecked():
            for stage in analysis.run.stages:
                s = stage.segment
                self.chart.add_x_annotation(s.start, s.end, "", STAGE_COLORS.get(s.kind, "#64748b"))
        if self.mk_anomalies.isChecked():
            for a in analysis.anomalies:
                end = a.end if a.end > a.start else a.start + 1.0
                self.chart.add_x_annotation(
                    a.start,
                    end,
                    a.kind.replace("_", " "),
                    SEVERITY_COLORS.get(a.severity, "#64748b"),
                )

    def _build_statistics_panel(self):
        tabs = QTabWidget()
        tabs.setObjectName("flatTabs")
        tabs.setDocumentMode(True)
        tabs.tabBar().setExpanding(False)

        range_page = QWidget()
        range_lay = QVBoxLayout(range_page)
        range_lay.setContentsMargins(0, 6, 0, 0)
        self._range_label = QLabel()
        self._range_label.setObjectName("analysisMeta")
        self._range_table = _make_table(
            [
                self.tr("Channel"),
                self.tr("n"),
                self.tr("Missing"),
                self.tr("Min"),
                self.tr("Max"),
                self.tr("Mean"),
                self.tr("Std"),
                self.tr("RMS"),
                self.tr("Slope /min"),
                self.tr("P95"),
            ],
            0,
        )
        self._range_kpi_label = QLabel()
        self._range_kpi_label.setWordWrap(True)
        self._range_kpi_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        range_lay.addWidget(self._range_label)
        range_lay.addWidget(self._range_table, 1)
        range_lay.addWidget(self._range_kpi_label)

        kpi_page = QWidget()
        kpi_lay = QVBoxLayout(kpi_page)
        kpi_lay.setContentsMargins(0, 6, 0, 0)
        tiles = QHBoxLayout()
        tiles.setSpacing(16)
        self._kpi_tiles = []
        for title in (
            self.tr("MAE"),
            self.tr("RMSE"),
            self.tr("Max error"),
            self.tr("Overshoot"),
            self.tr("Undershoot"),
            self.tr("Settling"),
            self.tr("Tail MAE"),
        ):
            tile = QWidget()
            tile_lay = QVBoxLayout(tile)
            tile_lay.setContentsMargins(0, 0, 0, 0)
            tile_lay.setSpacing(1)
            name = QLabel(title)
            name.setObjectName("kpiName")
            value = QLabel("—")
            value.setObjectName("kpiValue")
            tile_lay.addWidget(name)
            tile_lay.addWidget(value)
            tiles.addWidget(tile, 1)
            self._kpi_tiles.append(value)
        self._kpi_label = QLabel()
        self._kpi_label.setObjectName("analysisMeta")
        self._kpi_label.setWordWrap(True)
        self._kpi_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._stages_table = _make_table(
            [
                "#",
                self.tr("Stage"),
                self.tr("Interval"),
                self.tr("Cycle"),
                self.tr("Start"),
                self.tr("Duration"),
                self.tr("Setpoint"),
                self.tr("MAE"),
                self.tr("Overshoot"),
                self.tr("Settling"),
                self.tr("Rise / fall"),
                self.tr("Steady-state error"),
                self.tr("Steady after"),
                self.tr("Steady"),
                self.tr("Saturated"),
                self.tr("P / I / D"),
                self.tr("Kp / Ki / Kd"),
            ],
            16,
        )
        self._stages_table.setToolTip(self.tr("Click a stage to zoom to it"))
        self._stages_table.cellClicked.connect(self._zoom_to_stage)
        kpi_lay.addLayout(tiles)
        kpi_lay.addWidget(self._kpi_label)
        kpi_lay.addWidget(self._stages_table, 1)

        anomaly_page = QWidget()
        anomaly_lay = QVBoxLayout(anomaly_page)
        anomaly_lay.setContentsMargins(0, 6, 0, 0)
        self._anomaly_label = QLabel()
        self._anomaly_label.setObjectName("analysisMeta")
        self._anomaly_label.setWordWrap(True)
        self._anomaly_table = _make_table(
            [
                self.tr("Severity"),
                self.tr("Type"),
                self.tr("Channel"),
                self.tr("Start"),
                self.tr("Duration"),
                self.tr("Detail"),
            ],
            5,
        )
        self._anomaly_table.setToolTip(self.tr("Click an anomaly to zoom to it"))
        self._anomaly_table.cellClicked.connect(self._zoom_to_anomaly)
        anomaly_lay.addWidget(self._anomaly_label)
        anomaly_lay.addWidget(self._anomaly_table, 1)

        comparison_page = QWidget()
        comparison_lay = QVBoxLayout(comparison_page)
        comparison_lay.setContentsMargins(0, 6, 0, 0)
        self._comparison_label = QLabel()
        self._comparison_label.setObjectName("analysisMeta")
        self._comparison_label.setWordWrap(True)
        self._comparison_table = _make_table([self.tr("KPI")], 0)
        comparison_lay.addWidget(self._comparison_label)
        comparison_lay.addWidget(self._comparison_table, 1)

        tabs.addTab(range_page, self.tr("Selected range"))
        tabs.addTab(kpi_page, self.tr("KPIs && stages"))
        tabs.addTab(anomaly_page, self.tr("Anomalies"))
        tabs.addTab(comparison_page, self.tr("Comparison"))
        return tabs

    def _open_statistics(self):
        self._show_dialog(self._statistics_dialog)
        self._refresh_statistics()

    def _refresh_statistics(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._refresh_run_kpis()
            self._refresh_anomalies()
            self._refresh_comparison()
            self._refresh_range_statistics()
        finally:
            QApplication.restoreOverrideCursor()

    def _compared_data(self):
        """Returns this run and the compared runs."""
        rows = []
        for key in self._compared_keys() if self._compare_mode() else [self.run_key]:
            try:
                stored, analysis = self._run_data(key)
            except Exception:
                continue
            if key == self.run_key:
                analysis = self._own_analysis() or analysis
            run = next((r for r in self.all_runs if r.get("key") == key), None)
            rows.append((run["id"] if run else key, stored, analysis))
        return rows

    def _refresh_range_statistics(self):
        if not self.detail or not self._statistics_dialog.isVisible():
            return
        start, end = self.chart.current_x_range()
        self._range_label.setText(
            self.tr("Visible range {0} – {1} (raw samples, before smoothing)").format(
                _duration(max(0.0, start)), _duration(max(0.0, end))
            )
        )
        from app.core.analysis.seriesstats import compute_stats

        rows = []
        if self._compare_mode():
            channel = self._compared_channel()
            for title, stored, _ in self._compared_data():
                if channel in stored.channels:
                    rows.append((title, stored.times, stored.channels[channel]))
        else:
            try:
                stored, _ = self._run_data(self.run_key)
            except Exception:
                stored = None
            points = (self.series or {}).get("points", [])
            first_t = next((p.get("t") for p in points if _finite(p.get("t"))), None)
            for col in sorted(self.selected_columns):
                if stored is not None and col in stored.channels:
                    rows.append((col, stored.times, stored.channels[col]))
                elif points and first_t is not None:
                    times = [(p.get("t") or first_t) - first_t for p in points]
                    values = [p["values"].get(col) for p in points]
                    values = [v if _finite(v) else math.nan for v in values]
                    rows.append((col, times, values))

        cells = []
        for label, times, values in rows:
            s = compute_stats(times, values, start, end)
            slope = s.slope_per_s * 60.0 if _finite(s.slope_per_s) else math.nan
            cells.append(
                [
                    label,
                    s.n,
                    s.missing,
                    _stat(s.min),
                    _stat(s.max),
                    _stat(s.mean),
                    _stat(s.std),
                    _stat(s.rms),
                    _stat(slope),
                    _stat(s.p95),
                ]
            )
        _fill_table(self._range_table, cells)
        self._refresh_range_kpis(start, end)

    def _refresh_range_kpis(self, start, end):
        from app.core.analysis.runanalysis import analyse_run

        lines = []
        runs = self._compared_data()
        for title, stored, _ in runs:
            ch = stored.channels
            if "temp" not in ch or "temp_ref" not in ch:
                lines.append(
                    self.tr("<b>{0}</b>: control KPIs need the channels temp and temp_ref.").format(
                        title
                    )
                )
                continue
            keep = [i for i, t in enumerate(stored.times) if start <= t <= end]

            def sliced(name, keep=keep, ch=ch):
                return [ch[name][i] for i in keep] if name in ch else []

            a = analyse_run(
                [stored.times[i] for i in keep],
                sliced("temp"),
                sliced("temp_ref"),
                sliced("interval"),
                sliced("cycle_cur"),
            )
            name = f"<b>{title}</b>: " if len(runs) > 1 else self.tr("<b>Control in range</b> ")
            if a.overall.samples == 0:
                lines.append(name + self.tr("no setpoint (temp_ref) in this range."))
                continue
            counts = {}
            for stage in a.stages:
                counts[stage.segment.kind] = counts.get(stage.segment.kind, 0) + 1
            stages = ", ".join(
                f'<span style="color:{STAGE_COLORS.get(kind, "#64748b")}">{kind}</span> ×{n}'
                for kind, n in counts.items()
            )
            k = a.overall
            lines.append(
                name
                + self.tr(
                    "(temp vs temp_ref, {0} samples): MAE {1} · RMSE {2} · max |error| {3} · "
                    "overshoot {4} · undershoot {5} · settling {6} · steady-state error {7} · "
                    "tail MAE {8} · tail std {9} · stages: {10}"
                ).format(
                    k.samples,
                    _stat(k.mae),
                    _stat(k.rmse),
                    _stat(k.max_abs_error),
                    _stat(a.overshoot),
                    _stat(a.undershoot),
                    _duration(a.settling_time_s),
                    _stat(k.steady_state_error),
                    _stat(k.tail_mae),
                    _stat(k.tail_std),
                    stages or "—",
                )
            )
        self._range_kpi_label.setText("<br>".join(lines))

    def _refresh_run_kpis(self):
        from app.core.analysis.steadystate import steady_fraction

        a = self._own_analysis()
        if a is None or not a.has_control:
            for tile in self._kpi_tiles:
                tile.setText("—")
            cols = set((self.detail or {}).get("numeric_columns", []))
            if a is None:
                text = self.tr("The run's samples could not be read.")
            elif not {"temp", "temp_ref"} <= cols:
                text = self.tr("KPIs need the channels temp and temp_ref.")
            else:
                text = self.tr(
                    "No setpoint (temp_ref) was recorded in this run, so there are no KPIs or stages."
                )
            self._kpi_label.setText(text)
            self._stages_table.setRowCount(0)
            return

        k = a.run.overall
        c = a.config

        def degrees(v):
            return f"{_stat(v)} °C" if _finite(v) else "—"

        values = [
            degrees(k.mae),
            degrees(k.rmse),
            degrees(k.max_abs_error),
            degrees(a.run.overshoot),
            degrees(a.run.undershoot),
            _duration(a.run.settling_time_s),
            degrees(k.tail_mae),
        ]
        for tile, value in zip(self._kpi_tiles, values, strict=True):
            tile.setText(value)

        steady_share = math.nan
        if a.run.stages:
            steady_share = steady_fraction(
                a.steady, a.run.stages[0].segment.start, a.run.stages[-1].segment.end
            )
        setpoints = " → ".join(_stat(s, 2) for s in c.setpoints) or "—"
        gain_sets = self.tr(" ({0} gain sets)").format(c.gain_sets) if c.gain_sets > 1 else ""
        warnings = a.anomaly_count("warning")
        self._kpi_label.setText(
            self.tr("<b>Run</b>: {0} samples · tail std {1}.").format(k.samples, _stat(k.tail_std))
            + "<br>"
            + self.tr(
                "<b>Control</b>: steady {0} of the time · output saturated {1} (windup {2}) · "
                "P / I / D share {3} · <b>configuration</b>: Kp {4}, Ki {5}, Kd {6}{7} · "
                "setpoints {8} · {9} anomalies ({10} warning or worse)."
            ).format(
                _percent(steady_share),
                _percent(a.saturation.fraction),
                _duration(a.saturation.windup_s),
                _pid_shares(a.pid),
                _stat(c.kp),
                _stat(c.ki),
                _stat(c.kd),
                gain_sets,
                setpoints,
                len(a.anomalies),
                warnings,
            )
        )

        rows, colors = [], {}
        for r, stage in enumerate(a.run.stages):
            s = stage.segment
            d = a.stage_details[r] if r < len(a.stage_details) else None
            step = s.kind == "setpoint"
            if (
                _finite(s.reference_start)
                and _finite(s.reference_end)
                and (abs(s.reference_end - s.reference_start) >= 0.05)
            ):
                setpoint = f"{_stat(s.reference_start, 2)} → {_stat(s.reference_end, 2)}"
            else:
                setpoint = _stat(s.reference_end, 2)
            kp = stage.kpis
            transition = kp.rise_time_s if _finite(kp.rise_time_s) else kp.fall_time_s
            rows.append(
                [
                    r + 1,
                    s.kind.capitalize(),
                    s.interval if s.interval >= 0 else "—",
                    s.cycle if s.cycle >= 0 else "—",
                    _duration(s.start),
                    _duration(s.duration),
                    setpoint,
                    _stat(kp.mae),
                    _stat(kp.overshoot) if step else "—",
                    _duration(kp.settling_time_s) if step else "—",
                    _duration(transition) if step else "—",
                    _stat(kp.steady_state_error) if step else "—",
                    _duration(d.time_to_steady_s) if d else "—",
                    _percent(d.steady_fraction) if d else "—",
                    _percent(d.saturation.fraction) if d else "—",
                    _pid_shares(d.pid) if d else "—",
                    f"{_stat(d.kp)} / {_stat(d.ki)} / {_stat(d.kd)}" if d else "—",
                ]
            )
            colors[(r, 1)] = STAGE_COLORS.get(s.kind, "#64748b")
        _fill_table(self._stages_table, rows, right_from=4, colors=colors)

    def _refresh_anomalies(self):
        a = self._own_analysis()
        anomalies = a.anomalies if a is not None else []
        if not anomalies:
            self._anomaly_label.setText(
                self.tr(
                    "No anomalies detected (variable rules, spikes, flat signals, missing data, "
                    "oscillation, setpoints not reached, long output saturation)."
                )
            )
        else:
            self._anomaly_label.setText(
                self.tr("{0} anomalies, {1} warning or worse. Click one to zoom to it.").format(
                    len(anomalies), a.anomaly_count("warning")
                )
            )
        rows, colors = [], {}
        for r, an in enumerate(anomalies):
            rows.append(
                [
                    an.severity.capitalize(),
                    an.kind.replace("_", " "),
                    an.channel or "—",
                    _duration(an.start),
                    _duration(an.end - an.start),
                    an.detail,
                ]
            )
            colors[(r, 0)] = SEVERITY_COLORS.get(an.severity, "#64748b")
        _fill_table(self._anomaly_table, rows, right_from=99, colors=colors)

    def _refresh_comparison(self):
        from app.core.analysis.comparison import compare_kpis

        if not self._compare_mode():
            self._comparison_label.setText(
                self.tr("Press Compare (or tick runs in the runs list) to compare runs here.")
            )
            self._comparison_table.setColumnCount(1)
            self._comparison_table.setHorizontalHeaderLabels([self.tr("KPI")])
            self._comparison_table.setRowCount(0)
            return
        runs = self._compared_data()
        self._comparison_label.setText(
            self.tr("Headline KPIs side by side; the first run is the reference.")
        )
        table = self._comparison_table
        table.setColumnCount(1 + len(runs))
        table.setHorizontalHeaderLabels([self.tr("KPI")] + [title for title, _, _ in runs])
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        cells = []
        for row in compare_kpis([a for _, _, a in runs]):
            label = f"{row.label} ({row.unit})" if row.unit else row.label
            texts = []
            for v in row.values:
                if row.unit == "s":
                    texts.append(_duration(v))
                elif row.unit == "%":
                    texts.append(f"{_stat(v, 1)} %" if _finite(v) else "—")
                else:
                    texts.append(_stat(v))
            cells.append([label, *texts])
        _fill_table(table, cells)

    def _zoom_to_stage(self, row, _column=0):
        a = self._own_analysis()
        if a is not None and 0 <= row < len(a.run.stages):
            s = a.run.stages[row].segment
            self._zoom_to(s.start, max(s.end, s.start + 1.0))

    def _zoom_to_anomaly(self, row, _column=0):
        a = self._own_analysis()
        if a is not None and 0 <= row < len(a.anomalies):
            an = a.anomalies[row]
            margin = max(60.0, 0.5 * (an.end - an.start))
            self._zoom_to(max(0.0, an.start - margin), an.end + margin)

    def _open_export(self):
        """Opens the Export panel."""
        if self.side_bar.current() == "export":
            self._prepare_export()
        else:
            self.side_bar.open_panel("export")

    def _prepare_export(self):
        """Sets up the Export panel for this run."""
        from datetime import date

        from app.services import day_recorder

        panel = self.export_panel
        panel.set_comparison_available(self._compare_mode())
        start, end = self._run_span()
        if start is not None:
            panel.set_day(date.fromtimestamp(start), start, end)
        else:
            panel.set_day(date.today())
        chambers = sorted({d["chamber"] for d in day_recorder.list_days()})
        own = (self.detail or {}).get("run", {}).get("chamber")
        panel.set_chambers(chambers, day_recorder.safe_name(own) if own else None)

    def _run_span(self):
        """Returns the run's time span."""
        try:
            stored, _ = self._run_data(self.run_key)
            start = self._catalogue.entry_for_key(self.run_key).start_epoch
        except Exception:
            return None, None
        if not _finite(start) or not stored.times:
            return None, None
        return start, start + stored.times[-1]

    def _recorded_target(self):
        """The panel's day / custom range of the chosen chamber's recordings, as a run to export."""
        panel = self.export_panel
        return run_export.recorded_export(panel.chamber(), panel.from_epoch(), panel.to_epoch())

    def _on_export_preview(self, range_):
        from app.export_panel import show_data_preview

        try:
            target = self._recorded_target()
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Preview"), str(exc))
            return
        self._preview_dialog = show_data_preview(self, target)

    def _on_export_requested(self, fmt_, range_):
        """Exports what the Export panel asks for."""
        from app.export_panel import CUSTOM, DAY, EXPERIMENT, extension, is_data, is_image

        if fmt_ == "comparison-csv":
            done = self.export_comparison_csv()
        elif fmt_ == "json":
            done = self.export_bundle()
        elif range_ in (DAY, CUSTOM):
            done = self._export_recorded(fmt_)
        elif is_image(fmt_):
            done = (
                self._export_whole_chart(extension(fmt_))
                if range_ == EXPERIMENT
                else self.export_chart_image(extension(fmt_))
            )
        elif is_data(fmt_):
            done = self.export_run_data(range_ != EXPERIMENT, fmt_ == "txt")
        else:
            done = self.export_report(fmt_, visible_only=range_ != EXPERIMENT)
        if done is not None:
            self.side_bar.close_panel()

    def _shown_channels(self, target):
        return sorted(c for c in self.selected_columns if c in target.channels)

    def _export_whole_chart(self, ext):
        """Exports the chart of the whole run."""

        def write(path):
            target = self._export_target(self.run_key)
            image, _, _ = run_export.run_chart(
                target, channels=self._shown_channels(target) or None
            )
            run_export.save_image(image, path, ext)

        return self._export(ext, write, "-run")

    def _export_recorded(self, fmt_):
        """An image, the data or a report of a day / custom range of the chamber's recordings."""
        from app.export_panel import extension, is_data, is_image

        panel = self.export_panel
        ext = extension(fmt_)
        suffix = f"-{panel.chamber()}-{int(panel.from_epoch())}"

        def write(path):
            target = self._recorded_target()
            if is_image(fmt_):
                image, _, _ = run_export.run_chart(
                    target, channels=self._shown_channels(target) or None
                )
                run_export.save_image(image, path, ext)
            elif is_data(fmt_):
                run_export.write_data(target, path, None, None, None, fmt_ == "txt")
            else:
                run_export.write_report(
                    target, path, fmt_, author=self.current_user.get("name") or ""
                )

        return self._export(ext, write, suffix)

    def _export_target(self, key, with_samples=False):
        from app.api.runs import RunCatalogue

        if self._catalogue is None:
            self._catalogue = RunCatalogue()
        entry = self._catalogue.entry_for_key(key)
        return self._catalogue.export_run(entry, with_samples, title=entry.name)

    def _compared_keys(self):
        """This run first, then the runs compared with it."""
        return [self.run_key, *sorted(k for k in self.compare_runs if k != self.run_key)]

    def _compared_channel(self):
        if self._compare_mode() and isinstance(self.series, dict) and self.series.get("channel"):
            return self.series["channel"]
        return self._first_col() or "temp"

    def _ask_export_path(self, extension, suffix=""):
        """Asks where to save an export."""
        name = run_export.safe_name((self.detail or {}).get("run", {}).get("id", self.run_key))
        common.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        suggested = str(common.REPORTS_DIR / f"{name}{suffix}.{extension}")
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export"),
            suggested,
            self.tr("{0} files (*.{1})").format(extension.upper(), extension),
        )
        if not path:
            return None
        if not path.lower().endswith("." + extension):
            path += "." + extension
        return Path(path)

    def _export(self, extension, writer, suffix=""):
        """Runs an export and reports the result."""
        if not self.detail:
            return None
        path = self._ask_export_path(extension, suffix)
        if path is None:
            return None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            writer(path)
            error = None
        except Exception as exc:
            error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if error:
            QMessageBox.critical(
                self, self.tr("Export"), self.tr("Failed to write {0}: {1}").format(path, error)
            )
            return None
        QMessageBox.information(self, self.tr("Export"), self.tr("Saved to {0}").format(path))
        return path

    def export_chart_image(self, fmt_="png"):
        """The chart as shown."""
        return self._export(fmt_, lambda path: self.chart.export_view(str(path)))

    def export_run_data(self, visible_only=False, tab_separated=False):
        def write(path):
            lo, hi = self.chart.current_x_range() if visible_only else (None, None)
            run_export.write_data(
                self._export_target(self.run_key), path, None, lo, hi, tab_separated
            )

        return self._export(
            "txt" if tab_separated else "csv", write, "-range" if visible_only else ""
        )

    def export_comparison_csv(self):
        def write(path):
            targets = [self._export_target(k) for k in self._compared_keys()]
            run_export.write_comparison_csv(targets, path, self._compared_channel())

        return self._export("csv", write, "-comparison")

    def export_report(self, fmt_="pdf", visible_only=True):
        """Exports the run's report."""

        def write(path):
            target = self._export_target(self.run_key)
            first, compare = [], []
            if self._compare_mode():
                compare = [self._export_target(k) for k in self._compared_keys()[1:]]
            else:
                shown = [c for c in self.selected_columns if c in target.channels]
                if shown:
                    lo, hi = self.chart.current_x_range() if visible_only else (None, None)
                    image, _, _ = run_export.run_chart(
                        target, channels=sorted(shown), from_s=lo, to_s=hi
                    )
                    first.append((self.tr("Chart as shown"), image))
            run_export.write_report(
                target,
                path,
                fmt_,
                compare,
                self.current_user.get("name") or "",
                first,
                self._compared_channel(),
            )

        return self._export(fmt_, write)

    def export_bundle(self):
        return self._export(
            "json",
            lambda path: run_export.write_bundle(self._export_target(self.run_key, True), path),
        )
