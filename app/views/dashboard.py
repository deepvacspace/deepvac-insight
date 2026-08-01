"""DashboardMixin — builds and refreshes the Dashboard page.

Two filtering tiers: the Date Range control affects everything on the
page (stat tiles, charts, insights, best-runs, the table); Chamber and
Test Profile only narrow the Recent Runs table itself, so they never
blank out the page's summary numbers. Both are per-run tags set only on
runs saved via "Save Session as Run" in Live Monitoring (see
data_service.save_monitoring_session()'s chamber/test_profile parameters)
-- folder/upload-sourced runs simply have neither tag, which is correct
(they didn't come from a chamber connection or a test profile run).
"""

from datetime import datetime, timedelta, timezone

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import app.services.alarms_service as alarms_service
from app.common import REPORTS_DIR, _svg_icon, fmt
from app.services.chamber_session import MAX_CONNECTED_CHAMBERS

_DASH_PAGE_SIZE = 5
_DASH_RANGES = [
    ("7D", 7),
    ("30D", 30),
    ("90D", 90),
    ("1Y", 365),
    ("All", None),
]


def _parse_start_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _time_ago(dt):
    if dt is None:
        return None
    seconds = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _has_report(run):
    return (REPORTS_DIR / f"{_safe_report_filename(run['id'])}.xlsx").exists()


def _safe_report_filename(run_id):
    safe = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in str(run_id)).strip()
    return safe or "run"


class DashboardMixin:
    # ── layout builders ──────────────────────────────────────────────────────

    def _dash_card(self, title, header_extra=None):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        hdr = QHBoxLayout()
        lbl = QLabel(title)
        lbl.setObjectName("sectionLabel")
        hdr.addWidget(lbl, 0, Qt.AlignVCenter)
        hdr.addStretch(1)
        if header_extra is not None:
            hdr.addWidget(header_extra, 0, Qt.AlignVCenter)
        # header row must never stretch to fill leftover card height (that
        # extra space belongs to the card's main content below) -- without
        # this, when the card frame gets stretched taller than its natural
        # size (e.g. to match a taller sibling column), the header row (the
        # only non-fixed-height item competing for it) absorbs that space
        # instead, leaving the title/buttons floating in a tall empty strip
        # above the actual content.
        lay.addLayout(hdr, 0)
        return card, lay

    def _dashboard_view(self):
        scroll = QScrollArea()
        scroll.setObjectName("workspaceScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("workspaceBody")
        root = QVBoxLayout(body)
        root.setContentsMargins(24, 16, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(body)

        # -- top row: stat cards (left column) + filters/refresh/export
        # (right column), single row -- both "columns" share this one row
        # instead of stacking, to save vertical space.
        top_row = QHBoxLayout()
        top_row.setSpacing(16)

        self._dash_stats_row = QHBoxLayout()
        self._dash_stats_row.setSpacing(12)
        top_row.addLayout(self._dash_stats_row)

        top_row.addStretch(1)

        filt_row = QHBoxLayout()
        filt_row.setSpacing(8)

        self._dash_range_combo = QComboBox()
        for label, days in _DASH_RANGES:
            self._dash_range_combo.addItem(self.tr(label), days)
        self._dash_range_combo.setCurrentIndex(1)  # 30D default
        self._dash_range_combo.setToolTip(self.tr("Date Range"))
        self._dash_range_combo.currentIndexChanged.connect(self._dash_on_range_combo_changed)
        filt_row.addWidget(self._dash_range_combo)

        self._dash_chamber_combo = QComboBox()
        self._dash_chamber_combo.addItem(self.tr("All Chambers"), None)
        self._dash_chamber_combo.setToolTip(self.tr("Chamber"))
        self._dash_chamber_combo.currentIndexChanged.connect(self._dash_refresh_table)
        filt_row.addWidget(self._dash_chamber_combo)

        self._dash_profile_combo = QComboBox()
        self._dash_profile_combo.addItem(self.tr("All Test Profiles"), None)
        self._dash_profile_combo.setToolTip(self.tr("Test Profile"))
        self._dash_profile_combo.currentIndexChanged.connect(self._dash_refresh_table)
        filt_row.addWidget(self._dash_profile_combo)

        refresh_btn = QPushButton()
        refresh_btn.setIcon(_svg_icon("arrow-counterclockwise", "#94a3b8", 15))
        refresh_btn.setToolTip(self.tr("Reload runs"))
        refresh_btn.setFixedSize(34, 34)
        refresh_btn.clicked.connect(self.load_runs)
        filt_row.addWidget(refresh_btn)

        export_btn = QPushButton()
        export_btn.setObjectName("primaryButton")
        export_btn.setIcon(_svg_icon("download", "#ffffff", 14))
        export_btn.setToolTip(self.tr("Export"))
        export_btn.setFixedSize(34, 34)
        export_btn.clicked.connect(self._dash_export_csv)
        filt_row.addWidget(export_btn)

        top_row.addLayout(filt_row)
        root.addLayout(top_row)

        # -- main body: charts+table (left) / insights (right) --------------
        main_row = QHBoxLayout()
        main_row.setSpacing(12)

        left_col = QVBoxLayout()
        left_col.setSpacing(12)

        range_row = QHBoxLayout()
        range_row.setSpacing(2)
        self._dash_range_btns = {}
        for label, days in _DASH_RANGES:
            btn = QPushButton(self.tr(label))
            btn.setObjectName("dashRangeBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda _checked, d=days: self._dash_set_range(d))
            self._dash_range_btns[days] = btn
            range_row.addWidget(btn)
        range_row_widget = QWidget()
        range_row_widget.setLayout(range_row)
        trend_card, trend_lay = self._dash_card(
            self.tr("PERFORMANCE TREND — TAIL MAE OVER RUNS"), header_extra=range_row_widget
        )
        self._dash_mae_plot = pg.PlotWidget()
        self._dash_mae_plot.setBackground("#111827" if self.dark else "#f8fafc")
        self._dash_mae_plot.showGrid(x=True, y=True, alpha=0.22)
        self._dash_mae_plot.setMinimumHeight(190)
        self._dash_mae_plot.setMaximumHeight(260)
        self._dash_mae_plot.setMenuEnabled(False)
        trend_lay.addWidget(self._dash_mae_plot, 1)
        left_col.addWidget(trend_card)

        lower_row = QHBoxLayout()
        lower_row.setSpacing(12)

        ovr_card, ovr_lay = self._dash_card(self.tr("OVERSHOOT vs TAIL MAE"))
        self._dash_ovr_plot = pg.PlotWidget()
        self._dash_ovr_plot.setBackground("#111827" if self.dark else "#f8fafc")
        self._dash_ovr_plot.showGrid(x=True, y=True, alpha=0.22)
        self._dash_ovr_plot.setMinimumHeight(190)
        self._dash_ovr_plot.setMaximumHeight(260)
        self._dash_ovr_plot.setMenuEnabled(False)
        ovr_lay.addWidget(self._dash_ovr_plot, 1)
        lower_row.addWidget(ovr_card, 2)

        table_view_all = QPushButton(self.tr("View all"))
        table_view_all.setObjectName("secondaryButton")
        table_view_all.setFlat(True)
        table_view_all.clicked.connect(lambda: self._nav_to(1))
        table_card, table_lay = self._dash_card(self.tr("Recent Runs"), header_extra=table_view_all)
        self._dash_table = QTableWidget()
        self._dash_table.setAlternatingRowColors(True)
        self._dash_table.setShowGrid(False)
        self._dash_table.setWordWrap(False)
        self._dash_table.verticalHeader().setVisible(False)
        self._dash_table.horizontalHeader().setStretchLastSection(True)
        self._dash_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._dash_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._dash_table.setMinimumHeight(150)
        self._dash_table.setMaximumHeight(230)
        self._dash_table.itemDoubleClicked.connect(self._dash_open_row)
        table_lay.addWidget(self._dash_table, 1)
        pager_row = QHBoxLayout()
        self._dash_pager_label = QLabel("")
        self._dash_pager_label.setStyleSheet(
            "color: #94a3b8; font-size: 9.5pt; background: transparent;"
        )
        pager_row.addWidget(self._dash_pager_label)
        pager_row.addStretch(1)
        self._dash_prev_btn = QPushButton("‹")
        self._dash_prev_btn.setFixedSize(28, 26)
        self._dash_prev_btn.clicked.connect(lambda: self._dash_change_page(-1))
        self._dash_page_label = QLabel("")
        self._dash_page_label.setStyleSheet("background: transparent;")
        self._dash_next_btn = QPushButton("›")
        self._dash_next_btn.setFixedSize(28, 26)
        self._dash_next_btn.clicked.connect(lambda: self._dash_change_page(1))
        pager_row.addWidget(self._dash_prev_btn)
        pager_row.addWidget(self._dash_page_label)
        pager_row.addWidget(self._dash_next_btn)
        table_lay.addLayout(pager_row)
        lower_row.addWidget(table_card, 3)

        left_col.addLayout(lower_row)
        main_row.addLayout(left_col, 3)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.setContentsMargins(0, 0, 0, 0)

        insights_card, self._dash_insights_lay = self._dash_card(self.tr("Insights"))
        right_col.addWidget(insights_card)

        best_view_all = QPushButton(self.tr("View all"))
        best_view_all.setObjectName("secondaryButton")
        best_view_all.setFlat(True)
        best_view_all.clicked.connect(lambda: self._nav_to(1))
        best_card, best_lay = self._dash_card(
            self.tr("Best Runs (By Tail MAE)"), header_extra=best_view_all
        )
        self._dash_best_list = QListWidget()
        self._dash_best_list.setMaximumHeight(160)
        self._dash_best_list.itemDoubleClicked.connect(
            lambda item: self._dash_open_key(item.data(Qt.UserRole))
        )
        best_lay.addWidget(self._dash_best_list)
        right_col.addWidget(best_card)

        actions_card, actions_lay = self._dash_card(self.tr("Quick Actions"))
        for label, icon_name, slot in [
            (self.tr("Compare Runs"), "layout-split", lambda: self._nav_to(1)),
            (self.tr("Export Data"), "download", self._dash_export_csv),
            (self.tr("View Reports"), "file-earmark", lambda: self._nav_to(4)),
        ]:
            btn = QPushButton(f"  {label}")
            btn.setIcon(_svg_icon(icon_name, "#94a3b8", 15))
            btn.clicked.connect(slot)
            actions_lay.addWidget(btn)
        right_col.addWidget(actions_card)

        missing_card, missing_lay = self._dash_card(self.tr("Missing Reports"))
        missing_row = QHBoxLayout()
        self._dash_missing_count_lbl = QLabel("0")
        self._dash_missing_count_lbl.setStyleSheet(
            "font-size: 26px; font-weight: 800; background: transparent;"
        )
        missing_row.addWidget(self._dash_missing_count_lbl)
        missing_desc = QLabel(self.tr("run(s) are missing performance reports."))
        missing_desc.setWordWrap(True)
        missing_desc.setStyleSheet("color: #94a3b8; font-size: 9.5pt; background: transparent;")
        missing_row.addWidget(missing_desc, 1)
        missing_lay.addLayout(missing_row)
        missing_open_btn = QPushButton(self.tr("Open"))
        missing_open_btn.clicked.connect(lambda: self._nav_to(4))
        missing_lay.addWidget(missing_open_btn)
        right_col.addWidget(missing_card)

        right_col.addStretch(1)
        main_row.addLayout(right_col, 1)

        root.addLayout(main_row, 1)

        # -- footer ------------------------------------------------------
        footer_row = QHBoxLayout()
        self._dash_updated_lbl = QLabel("")
        self._dash_updated_lbl.setStyleSheet(
            "color: #64748b; font-size: 9pt; background: transparent;"
        )
        footer_row.addWidget(self._dash_updated_lbl)
        footer_row.addStretch(1)
        version_lbl = QLabel(self._dash_app_version())
        version_lbl.setStyleSheet("color: #64748b; font-size: 9pt; background: transparent;")
        footer_row.addWidget(version_lbl)
        root.addLayout(footer_row)

        self._dash_page = 0
        self._dash_range_days = 30
        self._dash_range_btns[30].setChecked(True)
        self._dash_last_refreshed = None
        return scroll

    def _dash_app_version(self):
        try:
            from importlib.metadata import version

            return f"v{version('deepvac-insight')}"
        except Exception:
            return ""

    # ── filtering ─────────────────────────────────────────────────────────

    def _dash_range_filtered(self):
        """Runs within the selected date range -- drives stats, charts,
        insights, and the best-runs list. Status applies only to the
        table itself (see _dash_table_filtered), same split reports.py
        already uses between its stat tiles and its table."""
        runs = self.runs
        days = getattr(self, "_dash_range_days", None)
        if days is None:
            return runs
        parsed = [(r, _parse_start_time(r.get("start_time"))) for r in runs]
        known = [(r, t) for r, t in parsed if t is not None]
        if not known:
            return runs
        end = max(t for _, t in known)
        start = end - timedelta(days=days)
        return [r for r, t in known if start <= t <= end]

    def _dash_prev_period(self):
        days = getattr(self, "_dash_range_days", None)
        if days is None:
            return None
        parsed = [(r, _parse_start_time(r.get("start_time"))) for r in self.runs]
        known = [(r, t) for r, t in parsed if t is not None]
        if not known:
            return None
        end = max(t for _, t in known)
        current_start = end - timedelta(days=days)
        prev_start = current_start - timedelta(days=days)
        return [r for r, t in known if prev_start <= t < current_start]

    def _dash_table_filtered(self):
        rows = self._dash_range_filtered()
        chamber = (
            self._dash_chamber_combo.currentData() if hasattr(self, "_dash_chamber_combo") else None
        )
        if chamber:
            rows = [r for r in rows if r.get("chamber") == chamber]
        test_profile = (
            self._dash_profile_combo.currentData() if hasattr(self, "_dash_profile_combo") else None
        )
        if test_profile:
            rows = [r for r in rows if r.get("test_profile") == test_profile]
        return sorted(
            rows,
            key=lambda r: (
                _parse_start_time(r.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )

    # ── controls ──────────────────────────────────────────────────────────

    def _dash_set_range(self, days):
        self._dash_range_days = days
        for d, btn in self._dash_range_btns.items():
            btn.setChecked(d == days)
        idx = next((i for i, (_, d) in enumerate(_DASH_RANGES) if d == days), 1)
        self._dash_range_combo.blockSignals(True)
        self._dash_range_combo.setCurrentIndex(idx)
        self._dash_range_combo.blockSignals(False)
        self._dash_page = 0
        self._refresh_dashboard()

    def _dash_on_range_combo_changed(self):
        self._dash_set_range(self._dash_range_combo.currentData())

    def _dash_change_page(self, delta):
        self._dash_page = max(0, self._dash_page + delta)
        self._dash_refresh_table()

    def _dash_open_key(self, key):
        if not key:
            return
        self._open_run(key)
        self._nav_to(2)

    def _dash_open_row(self, item):
        key = self._dash_table.item(item.row(), 0).data(Qt.UserRole)
        self._dash_open_key(key)

    def _dash_export_csv(self):
        import csv

        from PySide6.QtWidgets import QFileDialog, QMessageBox

        rows = self._dash_table_filtered()
        if not rows:
            QMessageBox.information(self, self.tr("Export"), self.tr("Nothing to export."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Export dashboard runs"), "dashboard-runs.csv", self.tr("CSV (*.csv)")
        )
        if not path:
            return
        fields = ["id", "group", "tail_mae", "overshoot", "start_time"]
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(fields)
                for r in rows:
                    writer.writerow(
                        [
                            r.get("id"),
                            r.get("group"),
                            r.get("tail_mae"),
                            r.get("overshoot"),
                            r.get("start_time"),
                        ]
                    )
        except OSError as exc:
            QMessageBox.critical(self, self.tr("Export failed"), str(exc))
            return
        QMessageBox.information(
            self, self.tr("Export complete"), self.tr("Saved to {0}").format(path)
        )

    # ── refresh ───────────────────────────────────────────────────────────

    def _dash_refresh_filter_choices(self):
        """Repopulates the Chamber/Test Profile combos with whatever
        distinct values actually appear in self.runs, preserving the
        current selection if it's still present."""
        for combo, key, all_label in [
            (self._dash_chamber_combo, "chamber", self.tr("All Chambers")),
            (self._dash_profile_combo, "test_profile", self.tr("All Test Profiles")),
        ]:
            previous = combo.currentData()
            values = sorted({r[key] for r in self.runs if r.get(key)})
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(all_label, None)
            for value in values:
                combo.addItem(value, value)
            idx = combo.findData(previous) if previous else 0
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _refresh_dashboard(self):
        while self._dash_stats_row.count():
            item = self._dash_stats_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        self._dash_refresh_filter_choices()
        self._dash_last_refreshed = datetime.now(timezone.utc)
        self._dash_updated_lbl.setText(
            self.tr("Data updated: {0}").format(_time_ago(self._dash_last_refreshed))
        )

        if not self.runs:
            self._dash_refresh_table()
            self._draw_dashboard_charts()
            self._dash_refresh_insights()
            self._dash_refresh_best_runs()
            return

        current = self._dash_range_filtered()
        previous = self._dash_prev_period()

        maes = [r["tail_mae"] for r in current if r.get("tail_mae") is not None]
        overshoots = [r["overshoot"] for r in current if r.get("overshoot") is not None]
        best = (
            min(
                current,
                key=lambda r: r.get("tail_mae") if r.get("tail_mae") is not None else float("inf"),
            )
            if current
            else None
        )

        def _delta(metric_fn, lower_is_better):
            if previous is None:
                return None
            cur_vals = metric_fn(current)
            prev_vals = metric_fn(previous)
            if not prev_vals or not cur_vals:
                return None
            cur_v, prev_v = sum(cur_vals) / len(cur_vals), sum(prev_vals) / len(prev_vals)
            if prev_v == 0:
                return None
            pct = (cur_v - prev_v) / abs(prev_v) * 100
            good = (pct < 0) if lower_is_better else (pct > 0)
            arrow = "↓" if pct < 0 else "↑"
            color = "#22c55e" if good else "#ef4444"
            return f'<span style="color:{color}">{arrow} {abs(pct):.1f}%</span>'

        tiles = [
            (
                "database",
                self.tr("Total Runs"),
                str(len(current)),
                _delta(lambda rs: [len(rs)], False),
            ),
            (
                "activity",
                self.tr("Avg Tail MAE"),
                fmt(sum(maes) / len(maes)) if maes else "-",
                _delta(
                    lambda rs: [r["tail_mae"] for r in rs if r.get("tail_mae") is not None], True
                ),
            ),
            (
                "check-circle",
                self.tr("Best Tail MAE"),
                fmt(min(maes)) if maes else "-",
                _delta(
                    lambda rs: (
                        [
                            min(
                                [r["tail_mae"] for r in rs if r.get("tail_mae") is not None],
                                default=None,
                            )
                        ]
                        if any(r.get("tail_mae") is not None for r in rs)
                        else []
                    ),
                    True,
                ),
            ),
            (
                "graph-up",
                self.tr("Best Overshoot"),
                fmt(min(overshoots)) if overshoots else "-",
                _delta(
                    lambda rs: (
                        [
                            min(
                                [r["overshoot"] for r in rs if r.get("overshoot") is not None],
                                default=None,
                            )
                        ]
                        if any(r.get("overshoot") is not None for r in rs)
                        else []
                    ),
                    True,
                ),
            ),
        ]
        delta_tooltip = self.tr("vs previous {0}").format(self._dash_range_btn_label())
        for icon_name, label, val, delta_html in tiles:
            self._dash_stats_row.addWidget(
                self._dash_stat_tile(icon_name, label, val, delta_html, tooltip=delta_tooltip)
            )

        best_sub = None
        if best is not None:
            best_sub = (best.get("start_time") or "").split(" ")[0] or None
        self._dash_stats_row.addWidget(
            self._dash_stat_tile(
                "window-stack",
                self.tr("Best Run"),
                best["id"] if best else "-",
                best_sub,
                elide=True,
            )
        )

        sessions = list(getattr(self, "_chamber_sessions", {}).values())
        connected = [s for s in sessions if s.is_connected()]
        if connected:
            chamber_val = f"{len(connected)}/{MAX_CONNECTED_CHAMBERS}"
            names = ", ".join(s.chamber["name"] for s in connected)
            chamber_sub, chamber_color = names, "#22c55e"
        else:
            last_seens = [s.last_seen for s in sessions if s.last_seen is not None]
            seen = _time_ago(max(last_seens)) if last_seens else None
            chamber_val = f"0/{MAX_CONNECTED_CHAMBERS}"
            chamber_sub = (
                self.tr("Last seen: {0}").format(seen) if seen else self.tr("Never connected")
            )
            chamber_color = "#ef4444"
        self._dash_stats_row.addWidget(
            self._dash_stat_tile(
                "broadcast",
                self.tr("Chamber Status"),
                chamber_val,
                chamber_sub,
                value_color=chamber_color,
            )
        )
        self._dash_stats_row.addStretch(1)

        self._draw_dashboard_charts()
        self._dash_refresh_table()
        self._dash_refresh_insights()
        self._dash_refresh_best_runs()

    def _dash_range_btn_label(self):
        days = getattr(self, "_dash_range_days", None)
        return next((lbl for lbl, d in _DASH_RANGES if d == days), "period")

    def _dash_stat_tile(
        self, icon_name, label, val, sub=None, value_color=None, elide=False, tooltip=None
    ):
        box = QFrame()
        box.setObjectName("card")
        if tooltip:
            box.setToolTip(tooltip)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(4)
        cap_row = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(_svg_icon(icon_name, "#60a5fa", 16).pixmap(16, 16))
        cap_row.addWidget(icon_lbl)
        cap = QLabel(label)
        cap.setObjectName("sectionLabel")
        cap_row.addWidget(cap)
        cap_row.addStretch(1)
        bl.addLayout(cap_row)
        text = str(val)
        if elide and len(text) > 13:
            text = text[:12] + "…"
        num = QLabel(text)
        color = value_color or "#f8fafc" if self.dark else (value_color or "#0f172a")
        num.setStyleSheet(
            f"font-size: 18px; font-weight: 800; background: transparent; color: {color};"
        )
        num.setToolTip(str(val))
        bl.addWidget(num)
        if sub:
            sub_lbl = QLabel()
            sub_lbl.setTextFormat(Qt.RichText)
            sub_lbl.setText(sub)
            sub_lbl.setStyleSheet("font-size: 9pt; background: transparent; color: #94a3b8;")
            bl.addWidget(sub_lbl)
        return box

    def _draw_dashboard_charts(self):
        self._dash_mae_plot.clear()
        self._dash_ovr_plot.clear()
        rows = self._dash_range_filtered()
        if not rows:
            return

        ordered = sorted(
            rows,
            key=lambda r: (
                _parse_start_time(r.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
        xs = list(range(len(ordered)))
        maes = [
            (x, r["tail_mae"])
            for x, r in zip(xs, ordered, strict=False)
            if r.get("tail_mae") is not None
        ]
        if maes:
            vx, vy = zip(*maes, strict=False)
            self._dash_mae_plot.plot(
                list(vx),
                list(vy),
                pen=pg.mkPen("#60a5fa", width=1.8),
                symbol="o",
                symbolSize=5,
                symbolBrush="#60a5fa",
                symbolPen=None,
            )
        self._dash_mae_plot.setLabel("bottom", self.tr("Run (oldest → newest)"))
        self._dash_mae_plot.setLabel("left", self.tr("Tail MAE"))

        pairs = [
            (r["tail_mae"], r["overshoot"])
            for r in rows
            if r.get("tail_mae") is not None and r.get("overshoot") is not None
        ]
        if pairs:
            cx, cy = zip(*pairs, strict=False)
            scatter = pg.ScatterPlotItem(
                list(cx),
                list(cy),
                size=7,
                brush=pg.mkBrush("#f2bd52"),
                pen=pg.mkPen("#111827", width=0.5),
            )
            self._dash_ovr_plot.addItem(scatter)
        self._dash_ovr_plot.setLabel("bottom", self.tr("Tail MAE"))
        self._dash_ovr_plot.setLabel("left", self.tr("Overshoot"))

    def _dash_refresh_table(self):
        if not hasattr(self, "_dash_table"):
            return
        rows = self._dash_table_filtered()
        total = len(rows)
        pages = max(1, (total + _DASH_PAGE_SIZE - 1) // _DASH_PAGE_SIZE)
        self._dash_page = min(self._dash_page, pages - 1)
        start = self._dash_page * _DASH_PAGE_SIZE
        page_rows = rows[start : start + _DASH_PAGE_SIZE]

        cols = [
            self.tr("Run ID"),
            self.tr("Group"),
            self.tr("Tail MAE"),
            self.tr("Overshoot"),
            self.tr("Date"),
        ]
        table = self._dash_table
        table.setUpdatesEnabled(False)
        table.clearContents()
        table.setColumnCount(len(cols))
        table.setRowCount(len(page_rows))
        table.setHorizontalHeaderLabels(cols)
        for ri, r in enumerate(page_rows):
            date_str = (r.get("start_time") or "-").split(" ")[
                0
            ]  # just the date; the full "YYYY-MM-DD HH:MM:SS UTC" doesn't fit the column
            values = [
                r.get("id", ""),
                r.get("group", "-"),
                fmt(r.get("tail_mae")),
                fmt(r.get("overshoot")),
                date_str,
            ]
            for ci, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                if ci == 0:
                    item.setData(Qt.UserRole, r["key"])
                table.setItem(ri, ci, item)
        table.resizeColumnsToContents()
        table.setUpdatesEnabled(True)

        shown_from = 0 if total == 0 else start + 1
        shown_to = min(start + _DASH_PAGE_SIZE, total)
        self._dash_pager_label.setText(
            self.tr("Showing {0} to {1} of {2} runs").format(shown_from, shown_to, total)
        )
        self._dash_page_label.setText(f"{self._dash_page + 1} / {pages}")
        self._dash_prev_btn.setEnabled(self._dash_page > 0)
        self._dash_next_btn.setEnabled(self._dash_page + 1 < pages)

    def _dash_refresh_best_runs(self):
        self._dash_best_list.clear()
        rows = [r for r in self._dash_range_filtered() if r.get("tail_mae") is not None]
        best = sorted(rows, key=lambda r: r["tail_mae"])[:3]
        for i, r in enumerate(best, start=1):
            item = QListWidgetItem(f"{i}.  {r['id']}   —   MAE {fmt(r['tail_mae'])}")
            item.setData(Qt.UserRole, r["key"])
            self._dash_best_list.addItem(item)
        if not best:
            self._dash_best_list.addItem(self.tr("No runs in this range."))

    def _dash_clear_insights(self):
        while self._dash_insights_lay.count() > 1:  # keep the header row
            item = self._dash_insights_lay.takeAt(1)
            w = item.widget()
            if w:
                w.deleteLater()

    def _dash_insight_row(
        self, icon_name, icon_color, title, desc, action_label=None, action_slot=None
    ):
        row = QFrame()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 4)
        rl.setSpacing(10)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(_svg_icon(icon_name, icon_color, 16).pixmap(16, 16))
        icon_lbl.setFixedWidth(20)
        rl.addWidget(icon_lbl)
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 700; font-size: 10pt; background: transparent;")
        text_col.addWidget(t)
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setStyleSheet("color: #94a3b8; font-size: 9pt; background: transparent;")
        text_col.addWidget(d)
        rl.addLayout(text_col, 1)
        if action_label and action_slot:
            btn = QPushButton(action_label)
            btn.setFixedHeight(26)
            btn.clicked.connect(action_slot)
            rl.addWidget(btn)
        return row

    def _dash_refresh_insights(self):
        self._dash_clear_insights()
        rows = self._dash_range_filtered()

        missing = [r for r in rows if not _has_report(r)]
        if missing:
            self._dash_insights_lay.addWidget(
                self._dash_insight_row(
                    "file-earmark",
                    "#f2bd52",
                    self.tr("Missing Reports"),
                    self.tr("{0} run(s) are missing performance reports.").format(len(missing)),
                    self.tr("Investigate"),
                    lambda: self._nav_to(4),
                )
            )
        self._dash_missing_count_lbl.setText(str(len(missing)))

        alert_row = self._dash_recent_alert_row()
        self._dash_insights_lay.addWidget(alert_row)

        if not missing:
            self._dash_insights_lay.addWidget(
                self._dash_insight_row(
                    "check-circle",
                    "#22c55e",
                    self.tr("All clear"),
                    self.tr("No missing reports in this range."),
                )
            )

    def _dash_recent_alert_row(self):
        try:
            events = alarms_service.list_events(limit=50)
            unacked = next((e for e in events if not e.get("acknowledged_at")), None)
        except Exception:
            unacked = None
        if unacked:
            when = self.tr("still active") if not unacked.get("cleared_at") else self.tr("cleared")
            return self._dash_insight_row(
                "bell",
                "#ef4444",
                self.tr("Recent Alerts"),
                self.tr("{0} ({1}, {2}) is unacknowledged.").format(
                    unacked["rule_name"], unacked["severity"], when
                ),
                self.tr("Details"),
                self._dash_open_alarm_history,
            )
        return self._dash_insight_row(
            "check-circle",
            "#22c55e",
            self.tr("Recent Alerts"),
            self.tr("No unacknowledged alarms."),
        )

    def _dash_open_alarm_history(self):
        from app.alarm_history_dialog import AlarmHistoryDialog

        dlg = AlarmHistoryDialog(current_user=self.current_user, parent=self)
        dlg.exec()
