"""Analysis tab page (RunTabPage) shown for each opened run, plus SimWorker.

Its Export menu has DeepVac Control's export options (app/services/run_export.py,
shared with the analyst agent): chart image, data, comparison CSV, experiment
report and experiment bundle.
"""

import contextlib
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

import app.services.annotations_service as annotations_service
import app.services.data_service as data
import app.services.derived_variables_service as derived_variables_service
import app.services.settings_service as settings_service
from app import common
from app.chart_widget import ChartWidget
from app.common import COLORS, RULE_COLOR_OPTIONS, _svg_icon, fmt
from app.services import run_export


class SimWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def run(self):
        try:
            self.finished_ok.emit(data.simulate_gru_run(self.payload))
        except Exception as exc:
            self.failed.emit(str(exc))


class RunTabPage(QWidget):
    compare_changed = Signal(set)

    def __init__(self, run_key, all_runs, dark=True, current_user=None, parent=None):
        super().__init__(parent)
        self.run_key = run_key
        self.all_runs = all_runs
        self.active_run = run_key
        self.detail = None
        self.series = None
        self.selected_columns = set()  # populated in load() from saved settings or defaults
        self._channel_colors = {}
        self.compare_runs = {run_key}
        self.dark = dark
        self.current_user = current_user or {"id": None, "name": "Unknown", "email": ""}
        self._user_annotations = []  # [{id, x0, x1, label, color, user_name, ...}] — persisted
        self._ann_list_layout = None
        self._var_rules = []  # [{id, name, channel, lo, hi, color, user_name, ...}] — persisted
        self._rule_ch_combo = None
        self._rules_list_layout = None
        self._derived_variables = []  # every definition, regardless of availability for this run
        self._available_derived = []  # subset computable from this run's numeric_columns
        self._catalogue = None  # app.api.runs.RunCatalogue, for the exports
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("workspaceScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("workspaceBody")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(14, 14, 14, 14)
        self._body_layout.setSpacing(10)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Chart card
        chart_card = QFrame()
        chart_card.setObjectName("card")
        chart_card_lay = QVBoxLayout(chart_card)
        chart_card_lay.setContentsMargins(10, 10, 10, 10)
        chart_card_lay.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)

        self.title_label = QLabel(self.tr("Loading…"))
        self.title_label.setObjectName("title")
        toolbar.addWidget(self.title_label)

        self.run_metric_labels = {}
        for lbl, key in [(self.tr("Samples"), "samples"), (self.tr("Duration"), "duration")]:
            toolbar.addWidget(self._inline_stat(lbl, key))
        toolbar.addStretch(1)

        self._ch_btn = QPushButton(self.tr("Channels ▾"))
        self._ch_btn.setObjectName("secondaryButton")
        self._ch_btn.clicked.connect(self._open_channels_menu)

        self.chart_mode = QComboBox()
        self.chart_mode.addItems(["line", "scatter"])
        self.chart_mode.currentTextChanged.connect(self.refresh_chart)

        self.show_setpoint = QCheckBox(self.tr("Setpoint"))
        self.show_setpoint.setChecked(True)
        self.show_setpoint.stateChanged.connect(self.refresh_chart)

        self.setpoint_value = QLineEdit("0")
        self.setpoint_value.setFixedWidth(64)
        self.setpoint_value.textChanged.connect(self.refresh_chart)

        reset_btn = QPushButton(self.tr("Reset View"))
        reset_btn.setIcon(_svg_icon("arrow-counterclockwise", "#94a3b8", 14))
        reset_btn.setIconSize(QSize(14, 14))
        reset_btn.clicked.connect(self._reset_views)

        self._ann_btn = QPushButton(self.tr("Annotate"))
        self._ann_btn.setCheckable(True)
        self._ann_btn.setToolTip(self.tr("Drag on the chart to mark a time range"))
        self._ann_btn.toggled.connect(self._toggle_annotate_mode)

        download = QPushButton(self.tr("Export"))
        download.setIcon(_svg_icon("download", "#ffffff", 14))
        download.setIconSize(QSize(14, 14))
        download.setObjectName("primaryButton")
        download.setMenu(self._build_export_menu(download))

        for w in [
            self._ch_btn,
            self.show_setpoint,
            self.setpoint_value,
            reset_btn,
            self._ann_btn,
            self.chart_mode,
            download,
        ]:
            toolbar.addWidget(w)

        chart_card_lay.addLayout(toolbar)
        self.chart = ChartWidget()
        self.chart.annotation_committed.connect(self._on_annotation_committed)
        chart_card_lay.addWidget(self.chart)
        self._body_layout.addWidget(chart_card)

        # Controls card
        ctrl_card = QFrame()
        ctrl_card.setObjectName("card")
        ctrl_lay = QVBoxLayout(ctrl_card)
        ctrl_lay.setContentsMargins(10, 10, 10, 10)
        ctrl_lay.setSpacing(8)
        ctrl_lay.addWidget(self._sec_lbl(self.tr("PLOT CONTROLS")))

        time_row = QHBoxLayout()
        time_row.setSpacing(6)
        self.time_start = QDoubleSpinBox()
        self.time_start.setRange(0, 1_000_000)
        self.time_start.setDecimals(1)
        self.time_start.setSuffix(" s")
        self.time_end = QDoubleSpinBox()
        self.time_end.setRange(0, 1_000_000)
        self.time_end.setDecimals(1)
        self.time_end.setSuffix(" s")
        apply_time = QPushButton(self.tr("Apply Range"))
        apply_time.clicked.connect(self._apply_time_range)
        reset_time = QPushButton(self.tr("Full Range"))
        reset_time.clicked.connect(self._reset_time_range)
        self.smoothing = QSpinBox()
        self.smoothing.setRange(1, 501)
        self.smoothing.setSingleStep(2)
        self.smoothing.setValue(1)
        self.smoothing.setSuffix(" pt")
        self.smoothing.valueChanged.connect(self._set_smoothing)
        for w in [
            QLabel(self.tr("Start")),
            self.time_start,
            QLabel(self.tr("End")),
            self.time_end,
            apply_time,
            reset_time,
            QLabel(self.tr("Smooth")),
            self.smoothing,
        ]:
            time_row.addWidget(w)
        time_row.addStretch(1)
        ctrl_lay.addLayout(time_row)

        ov_row = QHBoxLayout()
        ov_row.setSpacing(12)
        ov_row.addWidget(QLabel(self.tr("Overlays")))
        self.overlay_min = QCheckBox(self.tr("Min"))
        self.overlay_max = QCheckBox(self.tr("Max"))
        self.overlay_avg = QCheckBox(self.tr("Average"))
        for cb in [self.overlay_min, self.overlay_max, self.overlay_avg]:
            cb.stateChanged.connect(self._update_overlays)
            ov_row.addWidget(cb)
        ov_row.addSpacing(20)
        ov_row.addWidget(QLabel(self.tr("Markers")))
        self.mk_events = QCheckBox(self.tr("Events"))
        self.mk_alarms = QCheckBox(self.tr("Alarms"))
        self.mk_ctrl = QCheckBox(self.tr("Controller"))
        self.mk_state = QCheckBox(self.tr("State"))
        for cb in [self.mk_events, self.mk_alarms, self.mk_ctrl, self.mk_state]:
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_markers)
            ov_row.addWidget(cb)
        ov_row.addStretch(1)
        ctrl_lay.addLayout(ov_row)
        self._body_layout.addWidget(ctrl_card)

        self._rules_card_widget = self._build_rules_card()
        self._body_layout.addWidget(self._rules_card_widget)

        self._ann_card_widget = self._build_annotations_card()
        self._body_layout.addWidget(self._ann_card_widget)

        self.chart.set_dark(self.dark)

    def _inline_stat(self, label, key):
        box = QFrame()
        box.setObjectName("inlineStat")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(0)
        cap = QLabel(label)
        cap.setObjectName("inlineStatLabel")
        val = QLabel("-")
        val.setObjectName("inlineStatValue")
        lay.addWidget(cap)
        lay.addWidget(val)
        self.run_metric_labels[key] = val
        return box

    def _sec_lbl(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        return lbl

    # ── Data loading ─────────────────────────────────────────────────────────

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
        self._user_annotations = annotations_service.list_annotations(self.run_key)
        self._var_rules = annotations_service.list_rules(self.run_key)
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
        # A derived variable selected in a previous run may not be
        # computable here -- drop it from the active selection rather than
        # letting _load_series() fail on a channel that quietly isn't real.
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
        self._load_series()
        self.compare_changed.emit(self.compare_runs)

    def update_theme(self, dark):
        self.dark = dark
        self.chart.set_dark(dark)

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
        summary = (self.detail or {}).get("summary", {})
        values = {
            "samples": run.get("samples"),
            "duration": f"{fmt(run.get('duration_s'), 1)} s"
            if run.get("duration_s") is not None
            else None,
            "tail_mae": summary.get("tail_mae"),
        }
        for key, lbl in self.run_metric_labels.items():
            lbl.setText(fmt(values.get(key)))

    def _render_channels(self):
        if not self.detail:
            return
        cols = self.detail.get("numeric_columns", [])
        for i, col in enumerate(cols):
            if col not in self._channel_colors:
                self._channel_colors[col] = COLORS[i % len(COLORS)]
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
        cols = (self.detail or {}).get("numeric_columns", [])
        total = len(cols) + len(self._available_derived)
        n = len(self.selected_columns)
        self._ch_btn.setText(
            self.tr("Channels ({0}/{1}) ▾").format(n, total) if total else self.tr("Channels ▾")
        )

    def _open_channels_menu(self):
        if not self.detail:
            return
        menu = QMenu(self)
        for col in self.detail.get("numeric_columns", []):
            self._add_channel_menu_row(menu, col)

        if self._available_derived:
            menu.addSeparator()
            header = menu.addAction(self.tr("DERIVED VARIABLES"))
            header.setEnabled(False)
            for d in self._available_derived:
                self._add_channel_menu_row(menu, d["name"], tooltip=self._derived_tooltip(d))

        menu.addSeparator()
        manage_action = menu.addAction(self.tr("Manage Derived Variables…"))
        manage_action.triggered.connect(self._open_derived_variables_dialog)

        menu.exec(self._ch_btn.mapToGlobal(self._ch_btn.rect().bottomLeft()))

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

    def _add_channel_menu_row(self, menu, col, tooltip=None):
        wa = QWidgetAction(menu)
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 4, 10, 4)
        rl.setSpacing(10)

        cb = QCheckBox(col)
        cb.setChecked(col in self.selected_columns)
        if tooltip:
            cb.setToolTip(tooltip)
        cb.stateChanged.connect(lambda state, c=col: self._set_channel_active(c, bool(state)))

        swatch = QPushButton()
        swatch.setFixedSize(16, 16)
        color = self._channel_colors.get(col, COLORS[0])
        swatch.setStyleSheet(
            f"background:{color};border:1px solid rgba(255,255,255,0.25);border-radius:3px;"
        )
        swatch.clicked.connect(lambda _, c=col, m=menu: self._pick_channel_color(c, m))

        rl.addWidget(cb, 1)
        rl.addWidget(swatch)
        wa.setDefaultWidget(row)
        menu.addAction(wa)

    def _set_channel_active(self, col, active):
        if active:
            self.selected_columns.add(col)
        else:
            self.selected_columns.discard(col)
        self._update_ch_btn_label()
        self._load_series()
        settings_service.save_channels(self.run_key, sorted(self.selected_columns))

    def _pick_channel_color(self, col, menu):
        menu.close()
        current = QColor(self._channel_colors.get(col, COLORS[0]))
        picked = QColorDialog.getColor(current, self, self.tr("Color — {0}").format(col))
        if picked.isValid():
            self._channel_colors[col] = picked.name()
            self.refresh_chart()

    def _load_series(self):
        cols = list(self.selected_columns)
        if not cols:
            self.series = {"columns": [], "points": []}
            self.refresh_chart()
            return
        try:
            if self._compare_mode():
                # Derived variables aren't supported in compare mode: each
                # compared run would need its own source-channel fetch and
                # its own availability check, and the primary use case
                # (comparing one real channel across runs) doesn't need them.
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
                continue  # already checked for availability; a runtime failure just omits it
            for point, value in zip(points, computed, strict=False):
                point["values"][definition["name"]] = (
                    None if value != value else float(value)
                )  # NaN -> None

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

    def _set_smoothing(self, value):
        if value > 1 and value % 2 == 0:
            value += 1
            self.smoothing.blockSignals(True)
            self.smoothing.setValue(value)
            self.smoothing.blockSignals(False)
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

    # ── Annotations ───────────────────────────────────────────────────────────

    def _toggle_annotate_mode(self, checked: bool):
        self.chart.set_annotate_mode(checked)
        self._ann_btn.setText(self.tr("Done") if checked else self.tr("Annotate"))

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
        self._ann_btn.setChecked(False)
        self._refresh_annotations_list()
        self.refresh_chart()

    def _delete_user_annotation(self, idx: int):
        if 0 <= idx < len(self._user_annotations):
            ann = self._user_annotations.pop(idx)
            annotations_service.delete_annotation(ann["id"])
        self._refresh_annotations_list()
        self.refresh_chart()

    def _refresh_annotations_list(self):
        if self._ann_list_layout is None:
            return
        while self._ann_list_layout.count():
            item = self._ann_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for idx, ann in enumerate(self._user_annotations):
            row = QFrame()
            row.setObjectName("ruleRow")
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(6, 3, 6, 3)
            rlay.setSpacing(10)
            swatch = QLabel("■")
            swatch.setStyleSheet(
                f"color: {ann['color']}; background: transparent; font-size: 14px;"
            )
            desc = QLabel(
                self.tr("<b>{0}</b>  ·  {1:.1f} – {2:.1f} s  ·  {3}").format(
                    ann["label"], ann["x0"], ann["x1"], ann.get("user_name", "Unknown")
                )
            )
            desc.setStyleSheet("background: transparent;")
            del_btn = QPushButton("✕")
            del_btn.setObjectName("tabClose")
            del_btn.setFixedSize(22, 22)
            del_btn.clicked.connect(lambda _=False, i=idx: self._delete_user_annotation(i))
            rlay.addWidget(swatch)
            rlay.addWidget(desc, 1)
            rlay.addWidget(del_btn)
            self._ann_list_layout.addWidget(row)

    def _draw_user_annotations(self):
        for ann in self._user_annotations:
            self.chart.add_x_annotation(ann["x0"], ann["x1"], ann["label"], ann["color"])

    def _build_annotations_card(self):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        hdr = QHBoxLayout()
        hdr.addWidget(self._sec_lbl(self.tr("ANNOTATIONS")))
        info = QLabel(
            self.tr("Click 'Annotate', then drag on the chart to mark a time range and label it.")
        )
        info.setObjectName("sectionLabel")
        info.setWordWrap(True)
        hdr.addWidget(info, 1)
        lay.addLayout(hdr)
        self._ann_list_widget = QWidget()
        self._ann_list_layout = QVBoxLayout(self._ann_list_widget)
        self._ann_list_layout.setContentsMargins(0, 2, 0, 0)
        self._ann_list_layout.setSpacing(3)
        lay.addWidget(self._ann_list_widget)
        return card

    # ── Variable rules ────────────────────────────────────────────────────────

    def _build_rules_card(self):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.addWidget(self._sec_lbl(self.tr("VARIABLE RULES")))
        info = QLabel(
            self.tr("Highlight acceptable ranges on the Y-axis when the channel is plotted.")
        )
        info.setObjectName("sectionLabel")
        info.setWordWrap(True)
        hdr.addWidget(info, 1)
        lay.addLayout(hdr)

        form = QHBoxLayout()
        form.setSpacing(6)
        self._rule_ch_combo = QComboBox()
        self._rule_ch_combo.setFixedWidth(110)
        self._rule_lo_ed = QLineEdit()
        self._rule_lo_ed.setPlaceholderText(self.tr("min"))
        self._rule_lo_ed.setFixedWidth(72)
        self._rule_hi_ed = QLineEdit()
        self._rule_hi_ed.setPlaceholderText(self.tr("max"))
        self._rule_hi_ed.setFixedWidth(72)
        self._rule_name_ed = QLineEdit()
        self._rule_name_ed.setPlaceholderText(self.tr("label"))
        self._rule_name_ed.setFixedWidth(100)
        self._rule_color_combo = QComboBox()
        for name, _ in RULE_COLOR_OPTIONS:
            self._rule_color_combo.addItem(self.tr(name))
        self._rule_color_combo.setFixedWidth(90)
        add_btn = QPushButton(self.tr("Add Rule"))
        add_btn.setObjectName("primaryButton")
        add_btn.clicked.connect(self._add_var_rule)

        for cap, w in [
            (self.tr("Channel"), self._rule_ch_combo),
            (self.tr("Min"), self._rule_lo_ed),
            (self.tr("Max"), self._rule_hi_ed),
            (self.tr("Label"), self._rule_name_ed),
            (self.tr("Color"), self._rule_color_combo),
            ("", add_btn),
        ]:
            if cap:
                lbl = QLabel(cap)
                lbl.setObjectName("sectionLabel")
                form.addWidget(lbl)
            form.addWidget(w)
        form.addStretch(1)
        lay.addLayout(form)

        self._rules_list_widget = QWidget()
        self._rules_list_layout = QVBoxLayout(self._rules_list_widget)
        self._rules_list_layout.setContentsMargins(0, 2, 0, 0)
        self._rules_list_layout.setSpacing(3)
        lay.addWidget(self._rules_list_widget)
        return card

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
        lo_text = self._rule_lo_ed.text().strip()
        hi_text = self._rule_hi_ed.text().strip()
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
        self._refresh_rules_list()
        self.refresh_chart()

    def _remove_var_rule(self, idx):
        if 0 <= idx < len(self._var_rules):
            rule = self._var_rules.pop(idx)
            annotations_service.delete_rule(rule["id"])
        self._refresh_rules_list()
        self.refresh_chart()

    def _refresh_rules_list(self):
        while self._rules_list_layout.count():
            item = self._rules_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for idx, rule in enumerate(self._var_rules):
            row = QFrame()
            row.setObjectName("ruleRow")
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(6, 3, 6, 3)
            rlay.setSpacing(10)
            swatch = QLabel("■")
            swatch.setStyleSheet(
                f"color: {rule['color']}; background: transparent; font-size: 14px;"
            )
            lo_str = f"{rule['lo']:g}" if rule["lo"] is not None else "−∞"
            hi_str = f"{rule['hi']:g}" if rule["hi"] is not None else "+∞"
            desc = QLabel(
                self.tr("<b>{0}</b>  ·  {1}  [{2} , {3}]  ·  {4}").format(
                    rule["name"], rule["channel"], lo_str, hi_str, rule.get("user_name", "Unknown")
                )
            )
            desc.setStyleSheet("background: transparent;")
            del_btn = QPushButton("✕")
            del_btn.setObjectName("tabClose")
            del_btn.setFixedSize(22, 22)
            del_btn.clicked.connect(lambda _=False, i=idx: self._remove_var_rule(i))
            rlay.addWidget(swatch)
            rlay.addWidget(desc, 1)
            rlay.addWidget(del_btn)
            self._rules_list_layout.addWidget(row)

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

    # ── Exports (DeepVac Control's Export menu) ─────────────────────────────

    def _build_export_menu(self, parent):
        menu = QMenu(parent)
        images = menu.addMenu(self.tr("Chart image"))
        for label, fmt_ in (
            (self.tr("PNG…"), "png"),
            (self.tr("BMP…"), "bmp"),
            (self.tr("JPEG…"), "jpg"),
        ):
            images.addAction(label, lambda *_, f=fmt_: self.export_chart_image(f))
        data_menu = menu.addMenu(self.tr("Data"))
        data_menu.addAction(
            self.tr("Whole experiment (CSV)…"), lambda *_: self.export_run_data(False, False)
        )
        data_menu.addAction(
            self.tr("Whole experiment (TXT)…"), lambda *_: self.export_run_data(False, True)
        )
        data_menu.addSeparator()
        data_menu.addAction(
            self.tr("Visible range (CSV)…"), lambda *_: self.export_run_data(True, False)
        )
        data_menu.addAction(
            self.tr("Visible range (TXT)…"), lambda *_: self.export_run_data(True, True)
        )
        menu.addAction(self.tr("Comparison CSV…"), lambda *_: self.export_comparison_csv())
        menu.addSeparator()
        menu.addAction(self.tr("Experiment report (HTML)…"), lambda *_: self.export_report("html"))
        menu.addAction(self.tr("Experiment report (PDF)…"), lambda *_: self.export_report("pdf"))
        menu.addSeparator()
        menu.addAction(self.tr("Experiment bundle (JSON)…"), lambda *_: self.export_bundle())
        return menu

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
        """File browser in the reports folder, named after the run (AnalysisRunPage::askExportPath)."""
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
        """Asks where, runs writer(path) with a wait cursor and says how it went."""
        if not self.detail:
            return None
        path = self._ask_export_path(extension, suffix)
        if path is None:
            return None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            writer(path)
            error = None
        except (
            Exception
        ) as exc:  # shown to the user: a run without the channels, a file in use, ...
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

    def export_report(self, fmt_="pdf"):
        def write(path):
            target = self._export_target(self.run_key)
            first, compare = [], []
            if self._compare_mode():
                compare = [self._export_target(k) for k in self._compared_keys()[1:]]
            else:
                shown = [c for c in self.selected_columns if c in target.channels]
                if shown:
                    lo, hi = self.chart.current_x_range()
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
