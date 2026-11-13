"""AI Agents page: a test campaign from requirements to report."""

from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.theme_colors import HMI

STAGES = [
    ("requirement", "1. Requirements"),
    ("plan", "2. Plan"),
    ("approval", "3. Approval"),
    ("run", "4. Run"),
    ("report", "5. Report"),
]
ROGUES = [
    ("analyst_output", "Analyst forces an output"),
    ("worker_alarm", "Worker turns off an alarm"),
    ("worker_job", "Worker sends its own job"),
    ("operator_alarm", "Operator (Edit level) edits an alarm"),
    ("worker_vent_cold", "Worker vents the chamber now"),
    ("worker_ramp_fast", "Worker ramps at 10 °C/min"),
    ("worker_no_vacuum", "Worker cycles without vacuum"),
    ("analyst_pid", "Analyst changes the PID gains"),
    ("worker_out_of_range", "Worker sets +150 °C"),
    ("planner_self_approve", "Planner approves its own plan"),
]
SENSOR_FAULTS = [
    ("open", "Open circuit"),
    ("frozen", "Frozen reading"),
    ("spikes", "Spikes"),
    ("ok", "Repair it"),
]
THINKING = (
    "oscillation",
    "anomaly",
    "stabilise",
    "interrupted",
    "pump_slow",
    "pressure",
    "sensor",
)
TIME_STEPS_S = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600]
SUP = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")


def _j(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"ok": False, "error": "no answer"}


def sgn(v):
    if v is None:
        return "–"
    r = round(v, 1)
    return ("+" if r > 0 else "−" if r < 0 else "") + f"{abs(r):.1f}"


def f1(v):
    return "–" if v is None else f"{round(v, 1):.1f}".replace("-", "−")


def sci(p):
    if p is None:
        return "–"
    if p >= 1:
        return str(round(p)) if p >= 100 else f"{p:.1f}"
    e = math.floor(math.log10(p))
    return f"{p / 10**e:.1f}×10{str(e).translate(SUP)}"


def hms(seconds):
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def clock_of(series):
    t = series["t_h"][-1] if series and series.get("t_h") else 0.0
    return f"{int(t)}:{int((t - int(t)) * 60):02d}"


def _elapsed_label():
    label = _label("", "runClock")
    label.setTextFormat(Qt.RichText)
    return label


def _set_elapsed(label, series):
    """ "Elapsed:" small and regular, the campaign's hours:minutes large and bold."""
    label.setText(
        f"<span style='font-size:9.5pt;font-weight:500'>Elapsed:</span> {clock_of(series)}"
    )


def local_time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%I:%M:%S %p").lstrip("0")
    except (TypeError, ValueError):
        return ""


def stage_of(c):
    if not c:
        return "requirement"
    state = c.get("state")
    if state == "draft":
        return "plan"
    if state == "approval":
        return "approval"
    if state in ("running", "holding"):
        return "run"
    if state in ("completed", "aborted"):
        return "report"
    return "requirement"


def _plain(parent=None):
    w = QWidget(parent)
    w.setObjectName("agentsPlain")
    return w


def _label(text="", name=None, wrap=False):
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _button(text, name=None):
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.PointingHandCursor)
    return b


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


class StageRail(QFrame):
    """The campaign's five stages."""

    def __init__(self):
        super().__init__()
        self.setObjectName("stageRail")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.segments = {}
        for key, text in STAGES:
            seg = _label(text, "stageSeg")
            seg.setAlignment(Qt.AlignCenter)
            lay.addWidget(seg, 1)
            self.segments[key] = seg

    def set_stage(self, stage):
        keys = [k for k, _ in STAGES]
        now = keys.index(stage)
        for i, key in enumerate(keys):
            seg = self.segments[key]
            state = "done" if i < now else "now" if i == now else ""
            if seg.property("state") != state:
                seg.setProperty("state", state)
                seg.style().unpolish(seg)
                seg.style().polish(seg)


class SpecGrid(QFrame):
    """Grid of name, value and source cells."""

    def __init__(self, rows=(), values=None):
        super().__init__()
        self.setObjectName("specGrid")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(1, 1, 1, 1)
        self.grid.setSpacing(1)
        self.values = {}
        for i, row in enumerate(rows):
            self._cell(i, *row)
        for i, (key, name) in enumerate(values or []):
            self.values[key] = self._cell(i, name, "–", "")

    def _cell(self, i, name, value, source=""):
        cell = QFrame()
        cell.setObjectName("specCell")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(1)
        lay.addWidget(_label(name, "cellName"))
        val = _label(value, "cellValue", wrap=True)
        lay.addWidget(val)
        if source:
            lay.addWidget(
                _label(source.replace(", clause to confirm", ""), "cellSource", wrap=True)
            )
        lay.addStretch(1)
        self.grid.addWidget(cell, i // 2, i % 2)
        return val

    def set_value(self, key, text):
        label = self.values.get(key)
        if label is not None and label.text() != text:
            label.setText(text)


def _step_icon(state, dark):
    """An empty circle, the current step's ring, or a done step's tick."""
    size = 18
    pix = QPixmap(size * 2, size * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    muted = QColor(HMI["haze"] if dark else HMI["steel"])
    rect = QRectF(1.5, 1.5, size - 3, size - 3)
    if state == "now":
        p.setPen(QPen(QColor(HMI["sky"] if dark else HMI["cobalt"]), 4.5))
        p.drawEllipse(rect.adjusted(1.5, 1.5, -1.5, -1.5))
    else:
        p.setPen(QPen(muted, 1.4))
        p.drawEllipse(rect)
        if state == "done":
            p.setPen(QPen(muted, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline([QPointF(5.4, 9.2), QPointF(7.9, 11.6), QPointF(12.6, 6.6)])
    p.end()
    return pix


class StepList(QScrollArea):
    """List of the campaign's steps."""

    def __init__(self, steps, current=None, dark=False):
        super().__init__()
        self.setObjectName("stepList")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMaximumHeight(300)
        body = _plain()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        now_row = None
        for i, step in enumerate(steps):
            label = step.get("label", "")
            group, text = _step_text(label, step.get("kind"))
            state = (
                "" if current is None else "done" if i < current else "now" if i == current else ""
            )
            row = QFrame()
            row.setObjectName("stepRow")
            row.setProperty("state", state)
            row.setProperty("first", i == 0)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(14, 7, 14, 7)
            rl.setSpacing(12)
            icon = QLabel()
            icon.setObjectName("stepIcon")
            icon.setPixmap(_step_icon(state, dark))
            rl.addWidget(icon)
            words = _plain()
            wl = QVBoxLayout(words)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(0)
            wl.addWidget(_label(group, "stepGroup"))
            wl.addWidget(_label(text, "stepText", wrap=True))
            rl.addWidget(words, 1)
            lay.addWidget(row)
            if state == "now":
                now_row = row
        self.setWidget(body)
        if now_row is not None:
            QTimer.singleShot(0, lambda: self.ensureWidgetVisible(now_row, 0, 60))


def _step_text(label, kind=None):
    if label.startswith("Cycle ") and ":" in label:
        head, _, rest = label.partition(":")
        return head, rest.strip()
    return ("Before cycling" if kind == "pump" else "After cycling"), label


def _row_list(rows):
    """Builds a framed list of records."""
    frame = QFrame()
    frame.setObjectName("rowList")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    for i, (head, main, detail, badge, bad) in enumerate(rows):
        row = QFrame()
        row.setObjectName("stepRow")
        row.setProperty("first", i == 0)
        rl = QVBoxLayout(row)
        rl.setContentsMargins(14, 8, 14, 8)
        rl.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        group = _label(head, "stepGroup", wrap=True)
        group.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(group, 1)
        if badge:
            chip = _label(badge, "rowBadge")
            chip.setProperty("bad", bool(bad))
            top.addWidget(chip, 0, Qt.AlignTop)
        rl.addLayout(top)
        rl.addWidget(_label(main, "rowMain", wrap=True))
        if detail:
            rl.addWidget(_label(detail, "stepGroup", wrap=True))
        lay.addWidget(row)
    return frame


class LogAxis(pg.AxisItem):
    """Logarithmic pressure axis."""

    def tickSpacing(self, minVal, maxVal, size):  # noqa: N802
        return [(2.0 if maxVal - minVal > 8 else 1.0, 0.0)]

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        return ["10" + str(int(round(v))).translate(SUP) for v in values]


class HmsAxis(pg.AxisItem):
    """Time since the campaign started, hh:mm:ss, at round steps."""

    def tickSpacing(self, minVal, maxVal, size):  # noqa: N802
        span = maxVal - minVal
        step = next((s for s in TIME_STEPS_S if span / s <= 6), 86400)
        return [(float(step), 0.0)]

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        return [hms(max(0.0, v)) for v in values]


class CampaignChart(QFrame):
    """Campaign chart of temperatures and pressure."""

    def __init__(self):
        super().__init__()
        self.setObjectName("pageCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(_label(self.tr("Temperature and pressure"), "chartTitle"))
        head.addStretch(1)
        self.legend = _label("", "chartLegend")
        self.legend.setTextFormat(Qt.RichText)
        head.addWidget(self.legend)
        lay.addLayout(head)

        self.plot = pg.PlotWidget(
            axisItems={"right": LogAxis("right"), "bottom": HmsAxis("bottom")}
        )
        self.plot.setMinimumHeight(260)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        item = self.plot.getPlotItem()
        item.hideButtons()
        item.showAxis("right")
        self.pvb = pg.ViewBox()
        self.pvb.setMouseEnabled(x=False, y=False)
        item.scene().addItem(self.pvb)
        self.pvb.setZValue(item.zValue() + 1)
        item.getAxis("right").linkToView(self.pvb)
        self.pvb.setXLink(item)
        item.vb.sigResized.connect(self._sync_views)
        self.duts = [item.plot([], [], connect="finite") for _ in range(4)]
        self.platen = item.plot([], [], connect="finite")
        self.setpoint = item.plot([], [], connect="finite")
        self.temp = item.plot([], [], connect="finite")
        self.pres_sp = pg.PlotDataItem([], [], connect="finite")
        self.pres = pg.PlotDataItem([], [], connect="finite")
        for curve in (self.pres_sp, self.pres):
            self.pvb.addItem(curve)
        self.threshold = pg.InfiniteLine(angle=0, movable=False)
        self.threshold_label = pg.TextItem("", anchor=(1, 1))
        self.pvb.addItem(self.threshold, ignoreBounds=True)
        self.pvb.addItem(self.threshold_label, ignoreBounds=True)
        self.hold_line = pg.InfiniteLine(angle=0, movable=False)
        self.hold_label = pg.TextItem("", anchor=(1, 1))
        self.pvb.addItem(self.hold_line, ignoreBounds=True)
        self.pvb.addItem(self.hold_label, ignoreBounds=True)
        self.empty = pg.TextItem("", anchor=(0.5, 0.5))
        item.addItem(self.empty, ignoreBounds=True)
        lay.addWidget(self.plot, 1)

        lay.addWidget(_label(self.tr("Test-article temperatures"), "tileHead"))
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tiles, self.tile_frames, self.tile_names, self.tile_notes = [], [], [], []
        self.sensor_status = ["ok"] * 4
        for i in range(4):
            tile = QFrame()
            tile.setObjectName("tile")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(10, 6, 10, 6)
            tl.setSpacing(0)
            name = _label(f"temp{i + 1}", "tileName")
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            tl.addWidget(name)
            value = _label("–", "tileValue")
            tl.addWidget(value)
            note = _label("", "tileNote")
            note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            tl.addWidget(note)
            tiles.addWidget(tile, 1)
            self.tiles.append(value)
            self.tile_frames.append(tile)
            self.tile_names.append(name)
            self.tile_notes.append(note)
        lay.addLayout(tiles)
        self.set_dark(False)

    def _sync_views(self):
        item = self.plot.getPlotItem()
        self.pvb.setGeometry(item.vb.sceneBoundingRect())
        self.pvb.linkedViewChanged(item.vb, self.pvb.XAxis)

    def set_dark(self, dark):
        h = HMI
        self.dark = dark
        card, area, grid, axis = (
            (h["slate_raised"], h["slate"], h["slate_line"], h["haze"])
            if dark
            else (h["white"], h["plot"], h["grid"], h["axis"])
        )
        temp_c, pres_c, tc_c = h["temperature"], (h["sky"] if dark else h["pressure"]), h["haze"]
        self.plot.setBackground(card)
        item = self.plot.getPlotItem()
        item.vb.setBackgroundColor(area)
        for name in ("left", "bottom", "right"):
            ax = item.getAxis(name)
            ax.setPen(pg.mkPen(axis))
            ax.setTextPen(pg.mkPen(axis))
            ax.setTickPen(pg.mkPen(grid, style=Qt.DashLine))
        item.showGrid(x=True, y=True, alpha=1.0)
        item.getAxis("right").setGrid(False)
        item.setLabel("left", self.tr("Temperature, °C"), color=temp_c)
        item.setLabel("right", self.tr("Pressure, Torr"), color=pres_c)
        item.setLabel("bottom", self.tr("Time, hh:mm:ss"), color=axis)
        for curve in self.duts:
            curve.setPen(pg.mkPen(tc_c, width=1.6))
        self.setpoint.setPen(pg.mkPen(temp_c, width=2.2, style=Qt.DashLine))
        self.temp.setPen(pg.mkPen(temp_c, width=2.8))
        self.pres_sp.setPen(pg.mkPen(pres_c, width=2.2, style=Qt.DashLine))
        self.pres.setPen(pg.mkPen(pres_c, width=2.8))
        self.threshold.setPen(pg.mkPen(pres_c, width=1.5, style=Qt.DotLine))
        self.threshold_label.setColor(pres_c)
        platen_c = h["heat"]
        self.platen.setPen(pg.mkPen(platen_c, width=2.0))
        self.hold_line.setPen(pg.mkPen(h["alarm"], width=1.5, style=Qt.DotLine))
        self.hold_label.setColor(h["alarm"])
        self.empty.setColor(axis)

        def line(color, dashed=False, thin=False):
            mark = "╌╌" if dashed else "▬"
            size = "10px" if thin else "12px"
            return f"<span style='color:{color};font-size:{size}'>{mark}</span>"

        muted = h["haze"] if dark else h["steel"]
        self.legend.setText(
            f"<span style='color:{muted}'>"
            f"{line(temp_c)} Shroud &nbsp; {line(temp_c, True)} Setpoint, °C &nbsp; "
            f"{line(platen_c)} Platen &nbsp; {line(tc_c, thin=True)} Test article &nbsp; "
            f"{line(pres_c)} Pressure &nbsp; {line(pres_c, True)} Setpoint, Torr</span>"
        )

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._sync_views()

    def set_sensors(self, sensors):
        """Names the thermocouple tiles and shows their status."""
        sensors = list(sensors or [])
        for i in range(4):
            s = sensors[i] if i < len(sensors) else {"name": f"temp{i + 1}", "status": "ok"}
            status = s.get("status") or "ok"
            self.sensor_status[i] = status
            self.tile_names[i].setText(s["name"])
            self.tile_names[i].setToolTip(f"{s['name']} (temp{i + 1})")
            if status == "invalid":
                reason = (s.get("reason") or "").split(":")[-1].strip()
                note = self.tr("Invalid") + (f": {reason}" if reason else "")
            elif status == "excluded":
                note = self.tr("Excluded")
            elif status == "recheck":
                note = self.tr("Being checked again")
            else:
                note = ""
            self.tile_notes[i].setText(note)
            self.tile_notes[i].setToolTip(s.get("reason") or "")
            bad = status in ("invalid", "excluded")
            frame = self.tile_frames[i]
            if frame.property("bad") != bad:
                frame.setProperty("bad", bad)
                for w in (frame, self.tiles[i], self.tile_notes[i]):
                    w.style().unpolish(w)
                    w.style().polish(w)
            self.duts[i].setVisible(not bad)

    def update_series(self, series, threshold=None, duts_now=None, empty_text="", hold_limit=None):
        self._sync_views()
        s = series or {}
        t = np.array([x * 3600.0 for x in s.get("t_h") or []], dtype=float)

        def arr(values, log=False):
            out = np.array([np.nan if v is None else v for v in values or []], dtype=float)
            if log:
                with np.errstate(divide="ignore", invalid="ignore"):
                    out = np.where(out > 0, np.log10(out), np.nan)
            return out

        if not len(t):
            for curve in (
                *self.duts,
                self.platen,
                self.setpoint,
                self.temp,
                self.pres,
                self.pres_sp,
            ):
                curve.setData([], [])
            self.empty.setText(empty_text)
            self.empty.setPos(0.5, 0.5)
            for w in (self.threshold, self.threshold_label, self.hold_line, self.hold_label):
                w.hide()
        else:
            self.empty.setText("")
            temp, sp = arr(s.get("temp")), arr(s.get("setpoint"))
            duts = [arr(d) for d in (s.get("duts") or [[], [], [], []])]
            duts = [
                d if status not in ("invalid", "excluded") else np.full(len(t), np.nan)
                for d, status in zip(duts, self.sensor_status, strict=False)
            ]
            platen = arr(s.get("platen"))
            pres, psp = arr(s.get("pres"), True), arr(s.get("pres_setpoint"), True)
            for curve, ys in zip(self.duts, duts, strict=False):
                curve.setData(t, ys if len(ys) == len(t) else np.full(len(t), np.nan))
            self.platen.setData(t, platen if len(platen) == len(t) else np.full(len(t), np.nan))
            self.setpoint.setData(t, sp if len(sp) == len(t) else np.full(len(t), np.nan))
            self.temp.setData(t, temp if len(temp) == len(t) else np.full(len(t), np.nan))
            for curve, ys in ((self.pres, pres), (self.pres_sp, psp)):
                curve.setData(t, ys if len(ys) == len(t) else np.full(len(t), np.nan))
            tvals = np.concatenate(
                [v for v in (temp, sp, platen, *duts) if len(v) == len(t)] or [temp]
            )
            tvals = tvals[np.isfinite(tvals)]
            if len(tvals):
                lo, hi = float(tvals.min()), float(tvals.max())
                if hi - lo < 4:
                    mid = (hi + lo) / 2
                    lo, hi = mid - 2, mid + 2
                pad = (hi - lo) * 0.08
                self.plot.getPlotItem().vb.setYRange(lo - pad, hi + pad, padding=0)
            pvals = np.concatenate([v for v in (pres, psp) if len(v) == len(t)] or [pres])
            pvals = list(pvals[np.isfinite(pvals)])
            if not pvals:
                threshold = hold_limit = None
            for limit in (threshold, hold_limit):
                if limit:
                    pvals.append(math.log10(limit))
            if pvals:
                plo, phi = math.floor(min(pvals)), math.ceil(max(pvals))
                self.pvb.setYRange(plo, max(phi, plo + 1), padding=0)
            x1 = max(t[-1], t[0] + 60.0)
            self.plot.getPlotItem().vb.setXRange(t[0], x1, padding=0)
            if threshold:
                self.threshold.setPos(math.log10(threshold))
                self.threshold_label.setText(self.tr("Start pressure {0}").format(sci(threshold)))
                self.threshold_label.setPos(x1, math.log10(threshold))
                self.threshold.show()
                self.threshold_label.show()
            else:
                self.threshold.hide()
                self.threshold_label.hide()
            if hold_limit:
                self.hold_line.setPos(math.log10(hold_limit))
                self.hold_label.setText(self.tr("Hold limit {0}").format(sci(hold_limit)))
                self.hold_label.setPos(x1, math.log10(hold_limit))
                self.hold_line.show()
                self.hold_label.show()
            else:
                self.hold_line.hide()
                self.hold_label.hide()
        for label, value, status in zip(
            self.tiles, duts_now or [None] * 4, self.sensor_status, strict=False
        ):
            label.setText("–" if status in ("invalid", "excluded") else f"{sgn(value)}°")


class FeedPanel(QFrame):
    """Agents' messages and the signed log."""

    def __init__(self, act):
        super().__init__()
        self.setObjectName("pageCard")
        self._act = act
        self._feed_key = self._log_key = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.feed_tab = _button(self.tr("Agents"), "feedTab")
        self.log_tab = _button(self.tr("Signed log"), "feedTab")
        for b in (self.feed_tab, self.log_tab):
            b.setCheckable(True)
            tabs.addWidget(b, 1)
        self.feed_tab.setChecked(True)
        lay.addLayout(tabs)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.feed_list, self.feed_scroll = self._list()
        self.stack.addWidget(self.feed_scroll)
        log_page = _plain()
        ll = QVBoxLayout(log_page)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        self.verify = _label("", "verifyLabel", wrap=True)
        ll.addWidget(self.verify)
        row = QHBoxLayout()
        self.verify_btn = _button(self.tr("Verify log"))
        self.tamper_btn = _button(self.tr("Simulate tampering"))
        row.addWidget(self.verify_btn)
        row.addWidget(self.tamper_btn)
        row.addStretch(1)
        ll.addLayout(row)
        self.log_list, log_scroll = self._list(spacing=8)
        ll.addWidget(log_scroll, 1)
        self.stack.addWidget(log_page)
        self.feed_tab.clicked.connect(lambda: self._show(0))
        self.log_tab.clicked.connect(lambda: self._show(1))
        self._campaign = None
        self.verify_btn.clicked.connect(lambda: self._act("verify"))
        self.tamper_btn.clicked.connect(self._tamper)

    def _list(self, spacing=22):
        scroll = QScrollArea()
        scroll.setObjectName("agentsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = _plain()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(spacing)
        lay.addStretch(1)
        scroll.setWidget(body)
        return lay, scroll

    def _show(self, index):
        self.stack.setCurrentIndex(index)
        self.feed_tab.setChecked(index == 0)
        self.log_tab.setChecked(index == 1)

    def _tamper(self):
        c = self._campaign
        if c:
            self._act("untamper" if c.get("tampered") else "tamper", c["id"])

    def update_campaign(self, c):
        self._campaign = c
        if not c:
            key = "none"
            if self._feed_key != key:
                self._feed_key = key
                _clear(self.feed_list)
                self.feed_list.addWidget(
                    _label(
                        self.tr(
                            "The agents' messages appear here as the campaign runs. Start by "
                            "drafting a campaign."
                        ),
                        "msgEmpty",
                        wrap=True,
                    )
                )
                self.feed_list.addStretch(1)
            self.verify.setText("")
            return
        key = (c["id"], c.get("log_count"))
        if self._feed_key != key:
            self._feed_key = key
            _clear(self.feed_list)
            for e in reversed(c.get("log") or []):
                msg = _plain()
                ml = QVBoxLayout(msg)
                ml.setContentsMargins(0, 0, 0, 0)
                ml.setSpacing(2)
                ml.addWidget(_label(f"{e['action']} ({e['actor']})", "msgAct", wrap=True))
                if e.get("detail"):
                    ml.addWidget(_label(e["detail"], "msgText", wrap=True))
                ml.addWidget(_label(local_time(e.get("time")), "msgTime"))
                self.feed_list.addWidget(msg)
            self.feed_list.addStretch(1)
        verify = c.get("verify") or {}
        ok = verify.get("ok")
        self.verify.setProperty("bad", not ok)
        self.verify.style().unpolish(self.verify)
        self.verify.style().polish(self.verify)
        self.verify.setText(
            self.tr("Chain intact: {0} entries, last hash {1}…").format(
                verify.get("entries"), str(verify.get("last_hash"))[:16]
            )
            if ok
            else self.tr(
                "Chain broken at entry {0}: an entry was changed after it was signed."
            ).format(verify.get("broken_at"))
        )
        self.tamper_btn.setText(
            self.tr("Undo the tampering") if c.get("tampered") else self.tr("Simulate tampering")
        )
        broken = None if ok else verify.get("broken_at")
        key = (c["id"], c.get("log_count"), broken)
        if self._log_key != key:
            self._log_key = key
            _clear(self.log_list)
            for e in reversed(c.get("log") or []):
                entry = QFrame()
                entry.setObjectName("logEntry")
                entry.setProperty("bad", broken is not None and e["seq"] >= broken)
                el = QVBoxLayout(entry)
                el.setContentsMargins(10, 4, 4, 4)
                el.setSpacing(1)
                el.addWidget(
                    _label(f"{e['seq']}. {e['actor']}: {e['action']}", "msgAct", wrap=True)
                )
                if e.get("detail"):
                    el.addWidget(_label(e["detail"], "msgText", wrap=True))
                el.addWidget(_label(e.get("hash", ""), "logHash", wrap=True))
                self.log_list.addWidget(entry)
            self.log_list.addStretch(1)


class DecisionDialog(QDialog):
    """Dialog for a campaign decision point."""

    def __init__(self, parent, hold, user, decide):
        super().__init__(parent)
        self.setObjectName("decisionDialog")
        self.setWindowTitle(self.tr("Decision point"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self.key = None
        self._decide = decide
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(22, 20, 22, 18)
        self._lay.setSpacing(10)
        self.update_hold(hold, user)

    def update_hold(self, hold, user):
        rec = hold.get("recommendation")
        key = (hold["id"], (rec or {}).get("at"))
        if key == self.key:
            return
        self.key = key
        _clear(self._lay)
        self._lay.addWidget(
            _label(self.tr("Decision point raised by the run supervisor"), "decisionTag")
        )
        self._lay.addWidget(_label(hold.get("title", ""), "decisionTitle", wrap=True))
        if hold.get("detail"):
            self._lay.addWidget(_label(hold["detail"], "msgText", wrap=True))
        if rec:
            box = QFrame()
            box.setObjectName("decisionRec")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(12, 10, 12, 10)
            bl.addWidget(_label(self.tr("{0} recommends").format(rec.get("by", "")), "msgAct"))
            bl.addWidget(_label(rec.get("rationale", ""), "msgText", wrap=True))
            self._lay.addWidget(box)
        elif hold.get("kind") in THINKING:
            self._lay.addWidget(_label(self.tr("The Analyst is reading the data…"), "msgTime"))
        for option in hold.get("options") or []:
            recommended = bool(rec and rec.get("option") == option["key"])
            text = option["label"] + ("   ★ Recommended" if recommended else "")
            b = _button(text, "decisionOption")
            b.setProperty("recommended", recommended)
            b.clicked.connect(lambda _=False, k=option["key"]: self._decide(hold["id"], k))
            self._lay.addWidget(b)
        self._lay.addWidget(
            _label(self.tr("Person logged in the decision: {0}.").format(user or "—"), "msgTime")
        )


class _Stage(QWidget):
    def __init__(self, page):
        super().__init__()
        self.setObjectName("agentsPlain")
        self.page = page
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)

    def heading(self, text):
        self.lay.addSpacing(6)
        self.lay.addWidget(_label(text, "agentsHeading"))

    def chamber_head(self, c, *extra):
        """Adds the chamber status row and the title."""
        row = QHBoxLayout()
        row.setSpacing(8)
        self.state_dot = _label("", "stateDot")
        self.state_dot.setTextFormat(Qt.RichText)
        self.state_text = _label("", "chamberHeadText")
        self.state_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.state_dot)
        row.addWidget(self.state_text, 1)
        for widget in extra:
            row.addWidget(widget)
        self.lay.addLayout(row)
        self.update_head(c)
        self.lay.addWidget(_label(c.get("title", ""), "planTitle", wrap=True))

    def update_head(self, c):
        ch = self.page.state.get("chamber") or {}
        name = c.get("chamber_name") or c.get("chamber") or "the chamber"
        if not ch.get("connected"):
            word, color = self.tr("Offline"), HMI["no_connection"]
        elif ch.get("status") == "work":
            word, color = self.tr("Running"), HMI["live"]
        else:
            word, color = self.tr("Idle"), HMI["idle"]
        self.state_dot.setText(f"<span style='color:{color};font-size:14px'>●</span>")
        self.state_text.setText(f"{word} · {name}")

    def update(self, c):
        pass


class RequirementForm(_Stage):
    def __init__(self, page, back):
        super().__init__(page)
        cat = page.catalog
        self.lay.addWidget(_label(self.tr("Test requirements"), "planTitle"))
        self.article = QComboBox()
        self.article.setEditable(True)
        for a in cat.get("articles") or []:
            self.article.addItem(a["name"])
        self.article.currentTextChanged.connect(self._article_changed)
        self._field(self.tr("Test article"), self.article)
        self.tmin, self.tmax = self._degrees(), self._degrees()
        self._row(
            (self.tr("Lowest predicted, °C"), self.tmin),
            (self.tr("Highest predicted, °C"), self.tmax),
        )
        type_row = QHBoxLayout()
        type_row.setSpacing(8)
        self.cycling = _button(self.tr("Thermal cycling"), "typeChoice")
        self.ramp = _button(self.tr("Single ramp"), "typeChoice")
        for b in (self.cycling, self.ramp):
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, w=b: self._set_type(w is self.ramp))
            type_row.addWidget(b, 1)
        self.lay.addWidget(_label(self.tr("Test type"), "fieldCaption"))
        self.lay.addLayout(type_row)
        self.cyc_box = _plain()
        cl = QVBoxLayout(self.cyc_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        self.level = QComboBox()
        for name in cat.get("levels") or ["qualification", "acceptance"]:
            self.level.addItem(name)
        self.level.currentTextChanged.connect(self._fill_level)
        cl.addWidget(_label(self.tr("Level"), "fieldCaption"))
        cl.addWidget(self.level)
        self.margin, self.cycles = self._margin(), QSpinBox()
        self.cycles.setRange(1, 20)
        cl.addLayout(
            self._pair((self.tr("Margin, °C"), self.margin), (self.tr("Cycles"), self.cycles))
        )
        self.lay.addWidget(self.cyc_box)
        self.ramp_box = _plain()
        rl = QVBoxLayout(self.ramp_box)
        rl.setContentsMargins(0, 0, 0, 0)
        self.direction = QComboBox()
        self.direction.addItem(self.tr("Up to hot"), "up")
        self.direction.addItem(self.tr("Down to cold"), "down")
        self.ramp_margin = self._margin()
        self.ramp_margin.setValue(float(cat.get("ramp_margin_c") or 10.0))
        rl.addLayout(
            self._pair(
                (self.tr("Direction"), self.direction), (self.tr("Margin, °C"), self.ramp_margin)
            )
        )
        self.lay.addWidget(self.ramp_box)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText(self.tr("Anything the report should say about this test"))
        self._field(self.tr("Notes"), self.notes)
        self.error = _label("", "formError", wrap=True)
        self.lay.addWidget(self.error)
        buttons = QHBoxLayout()
        self.draft_btn = _button(self.tr("Draft the campaign"), "primaryButton")
        self.draft_btn.clicked.connect(self.draft)
        buttons.addWidget(self.draft_btn)
        if back is not None:
            back_btn = _button(self.tr("Back"))
            back_btn.clicked.connect(back)
            buttons.addWidget(back_btn)
        buttons.addStretch(1)
        self.lay.addLayout(buttons)
        self.lay.addStretch(1)
        self._set_type(False)
        self._fill_level(self.level.currentText())
        self._article_changed(self.article.currentText())

    def _degrees(self):
        spin = QDoubleSpinBox()
        spin.setRange(-150.0, 250.0)
        spin.setDecimals(1)
        return spin

    def _margin(self):
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 30.0)
        spin.setDecimals(1)
        return spin

    def _field(self, caption, widget):
        self.lay.addWidget(_label(caption, "fieldCaption"))
        self.lay.addWidget(widget)

    def _pair(self, *fields):
        row = QHBoxLayout()
        row.setSpacing(10)
        for caption, widget in fields:
            col = QVBoxLayout()
            col.setSpacing(4)
            col.addWidget(_label(caption, "fieldCaption"))
            col.addWidget(widget)
            row.addLayout(col, 1)
        return row

    def _row(self, *fields):
        self.lay.addLayout(self._pair(*fields))

    def _article_changed(self, name):
        a = next((x for x in self.page.catalog.get("articles") or [] if x["name"] == name), None)
        if a:
            self.tmin.setValue(float(a["op_min_c"]))
            self.tmax.setValue(float(a["op_max_c"]))

    def _fill_level(self, name):
        values = (self.page.catalog.get("level_values") or {}).get(name) or {}
        if values:
            self.margin.setValue(float(values.get("margin_c", 10.0)))
            self.cycles.setValue(int(values.get("cycles", 4)))

    def _set_type(self, ramp):
        self.cycling.setChecked(not ramp)
        self.ramp.setChecked(ramp)
        self.cyc_box.setVisible(not ramp)
        self.ramp_box.setVisible(ramp)

    def draft(self):
        ramp = self.ramp.isChecked()
        req = {
            "chamber_id": self.page.chamber_id,
            "article": self.article.currentText().strip(),
            "op_min_c": self.tmin.value(),
            "op_max_c": self.tmax.value(),
            "test_type": "ramp" if ramp else "cycling",
            "level": self.level.currentText(),
            "direction": self.direction.currentData(),
            "margin_c": self.ramp_margin.value() if ramp else self.margin.value(),
            "cycles": None if ramp else self.cycles.value(),
            "notes": self.notes.text().strip(),
        }
        r = _j(self.page.bridge.draft(json.dumps(req)))
        if not r.get("ok"):
            self.error.setText(r.get("error", ""))
            return
        self.page.new_campaign_form = False
        self.page.refresh()


class PlanView(_Stage):
    def __init__(self, page, c, approval=False):
        super().__init__(page)
        self.chamber_head(c)
        if approval:
            self.lay.addWidget(_chip(self.tr("Waiting for approval")), 0, Qt.AlignLeft)
            self.lay.addWidget(_label(self.tr("Nothing moves until a person approves."), "msgTime"))
        else:
            self.lay.addWidget(
                _label(
                    self.tr("{0}, about {1} h plus the pump-down.").format(
                        c.get("specification", ""), c.get("estimated_hours")
                    ),
                    "msgTime",
                    wrap=True,
                )
            )
        self.heading(self.tr("Steps"))
        self.lay.addWidget(StepList(c.get("steps_full") or [], None, page.dark))
        self.heading(self.tr("Specification"))
        self.lay.addWidget(
            SpecGrid([(r["item"], r["value"], r["source"]) for r in c.get("spec_rows") or []])
        )
        if not approval:
            row = QHBoxLayout()
            submit = _button(self.tr("Submit for approval"), "primaryButton")
            submit.clicked.connect(lambda: page.act("submit", c["id"]))
            cancel = _button(self.tr("Cancel"), "stopButton")
            cancel.clicked.connect(lambda: page.act("cancel", c["id"]))
            row.addWidget(submit)
            row.addWidget(cancel)
            row.addStretch(1)
            self.lay.addSpacing(6)
            self.lay.addLayout(row)
        self.lay.addStretch(1)

    def update(self, c):
        self.update_head(c)


def _chip(text, ok=False):
    chip = _label(text, "statusChip")
    chip.setProperty("ok", ok)
    return chip


class RunView(_Stage):
    def __init__(self, page, c):
        super().__init__(page)
        self.clock = _elapsed_label()
        extra = []
        if c.get("current"):
            stop = _button(self.tr("Stop"), "stopButton")
            stop.clicked.connect(lambda: self._stop(c["id"]))
            extra = [self.clock, stop]
        self.chamber_head(c, *extra)
        if c.get("hold_point"):
            self.lay.addWidget(_chip(self.tr("Decision point: a person decides")), 0, Qt.AlignLeft)
        self.rule_chip = _label("", "statusChip", wrap=True)
        self.rule_chip.hide()
        self.lay.addWidget(self.rule_chip)
        self.info = SpecGrid(
            values=[
                ("step", self.tr("Step")),
                ("now", self.tr("Now")),
                ("temp", self.tr("Shroud")),
                ("sp", self.tr("Setpoint")),
                ("platen", self.tr("Platen")),
                ("article", self.tr("Test article")),
                ("pres", self.tr("Pressure")),
                ("progress", self.tr("Progress")),
            ]
        )
        self.lay.addWidget(self.info)
        self.heading(self.tr("Steps"))
        self.lay.addWidget(StepList(c.get("steps_full") or [], c.get("step_index"), page.dark))
        from app.analysis_panels import add_section

        details = _plain()
        dl = QVBoxLayout(details)
        dl.setContentsMargins(0, 6, 0, 0)
        spec = add_section(dl, self.tr("Specification"), open_=False)
        spec.addWidget(
            SpecGrid([(r["item"], r["value"], r["source"]) for r in c.get("spec_rows") or []])
        )
        self.results_box = add_section(dl, self.tr("Plateau results"))
        self.deviations_box = add_section(dl, self.tr("Deviations"))
        self.lay.addWidget(details)
        self._results_key = self._deviations_key = None
        if c.get("current"):
            self.heading(self.tr("Safety tests"))
            fault = _button(self.tr("Mistype the PID gains"))
            fault.setToolTip(
                self.tr(
                    "Writes wrong PID gains for the band in use at the chamber, as a mistyped table "
                    "would; the run supervisor sees the oscillation and asks a person"
                )
            )
            fault.clicked.connect(self._fault)
            self.lay.addWidget(fault, 0, Qt.AlignLeft)
            self.sensor = QComboBox()
            for i, tc in enumerate(c.get("thermocouples") or [], start=1):
                self.sensor.addItem(tc["name"], i)
            self.sensor.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.sensor.setMinimumContentsLength(12)
            self.sensor_fault = QComboBox()
            for key, text in SENSOR_FAULTS:
                self.sensor_fault.addItem(self.tr(text), key)
            inject = _button(self.tr("Inject"))
            inject.setToolTip(
                self.tr(
                    "Gives the thermocouple the fault at the simulator; the run supervisor has to "
                    "find it on its own"
                )
            )
            inject.clicked.connect(self._sensor)
            if self.sensor.count():
                self.lay.addWidget(self.sensor)
                srow = QHBoxLayout()
                srow.addWidget(self.sensor_fault, 1)
                srow.addWidget(inject)
                self.lay.addLayout(srow)
            burst = _button(self.tr("Release a gas burst"))
            burst.setToolTip(
                self.tr(
                    "A part outgassing at the simulator: the pressure jumps and decays in a few "
                    "minutes; above the hold limit the pressure rule acts"
                )
            )
            burst.clicked.connect(self._outgas)
            self.lay.addWidget(burst, 0, Qt.AlignLeft)
            row = QHBoxLayout()
            self.rogue = QComboBox()
            for key, text in ROGUES:
                self.rogue.addItem(self.tr(text), key)
            self.rogue.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.rogue.setMinimumContentsLength(12)
            go = _button(self.tr("Simulate a rogue command"))
            go.clicked.connect(self._rogue)
            row.addWidget(self.rogue, 1)
            self.lay.addLayout(row)
            self.lay.addWidget(go, 0, Qt.AlignLeft)
        self.lay.addStretch(1)
        self._c = c
        self.update(c)

    def _stop(self, cid):
        if (
            QMessageBox.question(
                self, self.tr("Stop"), self.tr("Stop the campaign now? The chamber's job stops.")
            )
            == QMessageBox.Yes
        ):
            self.page.act("stop", cid)

    def _fault(self):
        r = self.page.act("fault", self.page.chamber_id)
        if r.get("ok"):
            self.page.toast(self.tr("PID gains of the band in use changed at the chamber."))

    def _sensor(self):
        index, fault = self.sensor.currentData(), self.sensor_fault.currentData()
        r = self.page.act("sensorFault", self.page.chamber_id, index, fault)
        if r.get("ok"):
            name = self.sensor.currentText().split(" (")[0]
            self.page.toast(
                self.tr("{0} repaired at the chamber.").format(name)
                if fault == "ok"
                else self.tr("{0}: {1} at the chamber. The run supervisor has to find it.").format(
                    name, self.sensor_fault.currentText().lower()
                )
            )

    def _outgas(self):
        r = self.page.act("outgas", self.page.chamber_id)
        if r.get("ok"):
            self.page.toast(self.tr("A burst of gas at the chamber: watch the pressure."))

    def _rogue(self):
        r = self.page.act("rogue", self._c["id"], self.rogue.currentData())
        if r.get("ok"):
            self.page.toast(
                self.tr("{0} asked to {1}: the gateway checks it.").format(
                    r.get("actor"), r.get("command")
                )
            )

    def update(self, c):
        self._c = c
        self.update_head(c)
        ch = self.page.state.get("chamber") or {}
        step = c.get("step") or {}
        group, text = _step_text(step.get("label", ""), None) if step else ("", "")
        self.info.set_value("step", f"{step['index']} / {step['of']}" if step else "–")
        self.info.set_value(
            "now", (f"{group} · {text}" if group.startswith("Cycle") else text) if step else "–"
        )
        self.info.set_value("temp", f"{sgn(ch.get('temp'))} °C")
        self.info.set_value("sp", f"{sgn(ch.get('setpoint'))} °C")
        self.info.set_value(
            "platen", "–" if ch.get("platen") is None else f"{sgn(ch['platen'])} °C"
        )
        self.info.set_value("article", _article_range(c, ch))
        self.info.set_value("pres", "–" if ch.get("pres") is None else f"{sci(ch['pres'])} Torr")
        self.info.set_value("progress", step.get("status") or "–")
        rule = c.get("pressure_rule") or {}
        held = rule.get("held")
        if held:
            self.rule_chip.setText(
                self.tr("Pressure rule: the ramp is held at {0} until {1} Torr").format(
                    f"{sgn(held['temp_c'])} °C", sci(rule["resume_torr"])
                )
                if held["kind"] == "ramp"
                else self.tr("Pressure rule: the dwell clock is paused until {0} Torr").format(
                    sci(rule["resume_torr"])
                )
            )
        self.rule_chip.setVisible(bool(held))
        _set_elapsed(self.clock, self.page.state.get("series"))
        results = c.get("results") or []
        if self._results_key != len(results):
            self._results_key = len(results)
            _clear(self.results_box)
            self.results_box.addWidget(_results(results))
        deviations = c.get("deviations") or []
        key = tuple((d.get("text"), d.get("decision")) for d in deviations)
        if self._deviations_key != key:
            self._deviations_key = key
            _clear(self.deviations_box)
            self.deviations_box.addWidget(_deviations(deviations))


def _article_range(c, ch):
    """Returns the range of the valid stabilisation sensors."""
    values = []
    duts = ch.get("duts") or []
    for i, tc in enumerate(c.get("thermocouples") or []):
        value = duts[i] if i < len(duts) else None
        if tc.get("stabilisation") and tc.get("status") == "ok" and value is not None:
            values.append(value)
    if not values:
        return "–"
    low, high = min(values), max(values)
    return f"{sgn(low)} °C" if abs(high - low) < 0.05 else f"{sgn(low)} … {sgn(high)} °C"


def _minutes(value):
    try:
        return f"{float(value):.0f} min"
    except (TypeError, ValueError):
        return "– min"


def _results(results):
    """Builds the plateau results list."""
    if not results:
        return _label("No plateau completed yet.", "msgTime")
    return _row_list(
        [
            (
                f"Cycle {r.get('cycle')} · {str(r.get('plateau') or '').capitalize()} plateau",
                f"{sgn(r.get('target_c'))} °C for {_minutes(r.get('dwell_min'))}",
                f"Shroud {f1(r.get('min_c'))} to {f1(r.get('max_c'))} °C"
                + (
                    f"\nTest article {f1(r['dut_min_c'])} to {f1(r['dut_max_c'])} °C"
                    if r.get("dut_min_c") is not None
                    else ""
                )
                + (
                    f", stabilised after {r['stabilised_after_min']} min"
                    if r.get("stabilised_after_min") is not None and r.get("dut_min_c") is not None
                    else ""
                ),
                ("In band" if r.get("within_band") else "Out of band")
                + (", forced" if r.get("forced") else ""),
                not r.get("within_band") or r.get("forced"),
            )
            for r in results
        ]
    )


def _deviations(deviations):
    """Builds the deviations list."""
    if not deviations:
        return _label("No deviations.", "msgTime")
    rows = []
    for d in deviations:
        where = (d.get("where") or "").strip()
        head = " · ".join(
            x for x in (local_time(d.get("time")), where[:1].upper() + where[1:]) if x
        )
        decision = d.get("decision") or ""
        rows.append(
            (head, d.get("text", ""), f"Decision: {decision}" if decision else "", "", False)
        )
    return _row_list(rows)


def _blocked(blocked):
    """Builds the blocked commands list."""
    rows = []
    for b in blocked:
        command = (b.get("command") or "").strip()
        head = " · ".join(x for x in (local_time(b.get("time")), b.get("actor") or "") if x)
        rows.append(
            (
                head,
                command[:1].upper() + command[1:],
                " ".join(b.get("reasons") or []),
                "Blocked",
                True,
            )
        )
    return _row_list(rows)


class ReportView(_Stage):
    def __init__(self, page, c):
        super().__init__(page)
        self.clock = _elapsed_label()
        _set_elapsed(self.clock, page.state.get("series"))
        self.chamber_head(c, self.clock)
        state = c.get("state", "")
        reason = (c.get("end_reason") or "").strip()
        if reason.lower().startswith(state.lower()):
            outcome = reason[:1].upper() + reason[1:]
        else:
            outcome = state.capitalize() + (f": {reason}" if reason else "")
        self.lay.addWidget(_chip(outcome, ok=state == "completed"), 0, Qt.AlignLeft)
        if c.get("summary"):
            self.lay.addWidget(_label(c["summary"], "msgText", wrap=True))
            self.lay.addWidget(_label(c.get("summary_source") or "", "msgTime", wrap=True))
        if c.get("notes"):
            self.lay.addWidget(
                _label(self.tr("Notes: {0}").format(c["notes"]), "msgText", wrap=True)
            )
        from app.analysis_panels import add_section

        details = _plain()
        dl = QVBoxLayout(details)
        dl.setContentsMargins(0, 6, 0, 0)
        add_section(dl, self.tr("Plateau results")).addWidget(_results(c.get("results") or []))
        add_section(dl, self.tr("Deviations")).addWidget(_deviations(c.get("deviations") or []))
        if c.get("blocked"):
            add_section(dl, self.tr("Blocked by the gateway")).addWidget(_blocked(c["blocked"]))
        spec = add_section(dl, self.tr("Specification"), open_=False)
        spec.addWidget(
            SpecGrid([(r["item"], r["value"], r["source"]) for r in c.get("spec_rows") or []])
        )
        self.lay.addWidget(details)
        files = c.get("report_files") or {}
        row = QHBoxLayout()
        if not c.get("report_issued_at"):
            if c.get("writing_report"):
                row.addWidget(_chip(self.tr("Writing the report…")))
            else:
                offline = page.state.get("mode") == "offline"
                write = _button(
                    self.tr("Write the report (local model)")
                    if offline
                    else self.tr("Ask the Professor"),
                    "primaryButton",
                )
                write.clicked.connect(lambda: self._write(c["id"]))
                template = _button(self.tr("From a template"))
                template.clicked.connect(lambda: page.act("templateReport", c["id"]))
                row.addWidget(write)
                row.addWidget(template)
        else:
            for key, text in (
                ("pdf", self.tr("Open the PDF")),
                ("markdown", self.tr("Open the markdown")),
            ):
                if files.get(key):
                    b = _button(text)
                    b.clicked.connect(lambda _=False, p=files[key]: page.act("openFile", p))
                    row.addWidget(b)
            if not c.get("signed_off_by"):
                sign = _button(self.tr("Sign off"), "primaryButton")
                sign.clicked.connect(lambda: self._sign(c["id"]))
                row.addWidget(sign)
        row.addStretch(1)
        self.lay.addSpacing(6)
        self.lay.addLayout(row)
        if c.get("signed_off_by"):
            when = local_time(c.get("signed_off_at"))
            self.lay.addWidget(
                _label(
                    self.tr("Signed off by {0}{1}.").format(
                        c["signed_off_by"], f", {when}" if when else ""
                    ),
                    "signedLabel",
                )
            )
        new = _button(self.tr("New campaign"))
        new.clicked.connect(page.open_new_campaign_form)
        self.lay.addWidget(new, 0, Qt.AlignLeft)
        self.lay.addStretch(1)

    def update(self, c):
        self.update_head(c)
        _set_elapsed(self.clock, self.page.state.get("series"))

    def _write(self, cid):
        r = self.page.act("issueReport", cid)
        if r.get("asked"):
            self.page.toast(self.tr("Asked the Professor in the chat."))

    def _sign(self, cid):
        user = self.page.state.get("user") or self.tr("the signed-in user")
        if (
            QMessageBox.question(
                self, self.tr("Sign off"), self.tr("Sign the report off as {0}?").format(user)
            )
            == QMessageBox.Yes
        ):
            r = self.page.act("signOff", cid)
            if r.get("ok"):
                self.page.toast(self.tr("Report signed off."))


class AiAgentsPage(QWidget):
    """AI Agents page."""

    def __init__(self, host=None, parent=None):
        super().__init__(parent)
        self.setObjectName("aiAgentsPage")
        self.host = host
        self.view = None
        self.bridge = None
        self.dark = False
        self.catalog = {}
        self.state = {}
        self.chamber_id = 0
        self.new_campaign_form = False
        self._stage_key = None
        self._stage = None
        self._dialog = None
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 12, 16, 12)
        self._lay.setSpacing(12)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def apply_theme(self, dark, background=None):
        self.dark = bool(dark)
        if self.view is not None:
            self.chart.set_dark(self.dark)
            self._stage_key = None
            self.refresh()

    def shutdown(self):
        self._timer.stop()
        if self._dialog is not None:
            self._dialog.close()

    def showEvent(self, event):
        super().showEvent(event)
        if self.view is None:
            self._build()
        self.refresh()
        self._timer.start(1000)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _build(self):
        from app.views.demo_bridge import DemoBridge

        self.bridge = DemoBridge(host=self.host, parent=self)
        self.catalog = _j(self.bridge.catalog())
        self.view = _plain(self)
        root = QVBoxLayout(self.view)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        self.rail = StageRail()
        root.addWidget(self.rail)
        self.banner = QFrame()
        self.banner.setObjectName("aiBanner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 8, 14, 8)
        self.banner_text = _label("", "bannerText", wrap=True)
        self.retry_btn = _button(self.tr("Try again"))
        self.retry_btn.clicked.connect(lambda: self.act("demoStart", self._mode()))
        self.api_btn = _button(self.tr("Turn on the Agent API"))
        self.api_btn.clicked.connect(lambda: self.act("startAgentApi"))
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.retry_btn)
        bl.addWidget(self.api_btn)
        self.banner.hide()
        root.addWidget(self.banner)

        body = QHBoxLayout()
        body.setSpacing(16)
        self._body = body
        self.left_pad, self.right_pad = _plain(), _plain()
        self.stage_card = QFrame()
        self.stage_card.setObjectName("pageCard")
        sl = QVBoxLayout(self.stage_card)
        sl.setContentsMargins(16, 14, 8, 14)
        sl.setSpacing(10)
        self.stage_scroll = QScrollArea()
        self.stage_scroll.setObjectName("agentsScroll")
        self.stage_scroll.setWidgetResizable(True)
        self.stage_scroll.setFrameShape(QFrame.NoFrame)
        self.stage_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = _plain()
        self.stage_lay = QVBoxLayout(holder)
        self.stage_lay.setContentsMargins(0, 0, 8, 0)
        self.stage_scroll.setWidget(holder)
        sl.addWidget(self.stage_scroll, 1)
        self.chart = CampaignChart()
        self.chart.set_dark(self.dark)
        self.feed = FeedPanel(self._feed_act)
        self.feed.setFixedWidth(340)
        for w in (self.left_pad, self.stage_card, self.chart, self.feed, self.right_pad):
            body.addWidget(w)
        body.setStretch(2, 1)
        root.addLayout(body, 1)
        self._lay.addWidget(self.view, 1)
        self.toast_label = _label("", "aiToast")
        self.toast_label.setParent(self)
        self.toast_label.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast_label.hide)

    def _mode(self):
        return "online" if self.state.get("mode") == "online" else "offline"

    def _tick(self):
        if self.isVisible():
            self.refresh()

    def refresh(self):
        if self.bridge is None:
            return
        s = _j(self.bridge.state(self.chamber_id, 0))
        if not s or s.get("ok") is False:
            return
        self.state = s
        if s.get("chamber_id") and s["chamber_id"] != self.chamber_id:
            self.chamber_id = s["chamber_id"]
        self._render()

    def act(self, name, *args):
        if name == "verify":
            self.refresh()
            self.toast(self.tr("Every entry's hash checked against the one before."))
            return {"ok": True}
        r = _j(getattr(self.bridge, name)(*args))
        if r.get("ok") is False:
            self.toast(r.get("error", ""))
        self.refresh()
        return r

    def _feed_act(self, name, *args):
        return self.act(name, *args)

    def toast(self, text):
        if not text:
            return
        self.toast_label.setText(text)
        self.toast_label.adjustSize()
        self.toast_label.move(
            (self.width() - self.toast_label.width()) // 2,
            self.height() - self.toast_label.height() - 24,
        )
        self.toast_label.raise_()
        self.toast_label.show()
        self._toast_timer.start(3500)

    def open_new_campaign_form(self):
        self.new_campaign_form = True
        self._stage_key = None
        self._render()

    def _render(self):
        s = self.state
        c = s.get("campaign")
        stage = stage_of(c)
        form = stage == "requirement" or self.new_campaign_form
        self.rail.set_stage("requirement" if form else stage)
        self._render_banner()
        self.chart.setVisible(not form)
        self.feed.setVisible(not form)
        self.left_pad.setVisible(form)
        self.right_pad.setVisible(form)
        self.stage_card.setFixedWidth(560 if form else 380)
        self._render_stage(c, stage, form)
        self._fit_stage_card(form)
        if not form:
            self._render_chart(c)
            self.feed.update_campaign(c)
        self._render_decision(c)

    def _render_stage(self, c, stage, form):
        if form:
            key = ("form", bool(c))
        elif stage == "run":
            key = (
                "run",
                c["id"],
                c.get("step_index"),
                (c.get("hold_point") or {}).get("id"),
                c.get("current"),
            )
        else:
            key = (
                stage,
                c["id"],
                c.get("state"),
                c.get("report_issued_at"),
                c.get("signed_off_by"),
                c.get("writing_report"),
                c.get("log_count") if stage == "report" else None,
            )
        if key != self._stage_key:
            self._stage_key = key
            _clear(self.stage_lay)
            if form:
                back = self._close_form if c else None
                self._stage = RequirementForm(self, back)
            elif stage == "run":
                self._stage = RunView(self, c)
            elif stage in ("plan", "approval"):
                self._stage = PlanView(self, c, approval=stage == "approval")
            else:
                self._stage = ReportView(self, c)
            self.stage_lay.addWidget(self._stage)
        elif self._stage is not None and c:
            self._stage.update(c)

    def _fit_stage_card(self, form):
        """Sizes the stage card to the form's height, or to the full height otherwise."""
        if form and self._stage is not None:
            margins = self.stage_card.layout().contentsMargins()
            inner = self.stage_lay.contentsMargins()
            width = (
                self.stage_card.width()
                - margins.left()
                - margins.right()
                - inner.left()
                - inner.right()
            )
            self._stage.layout().activate()
            form_height = (
                self._stage.heightForWidth(width)
                if self._stage.hasHeightForWidth()
                else self._stage.sizeHint().height()
            )
            form_height = max(form_height, self._stage.minimumSizeHint().height())
            height = form_height + margins.top() + margins.bottom() + 4
            available = self.view.height() - self.rail.height() - 12
            if available > 0:
                height = min(height, available)
            self.stage_card.setFixedHeight(height)
            self._body.setAlignment(self.stage_card, Qt.AlignTop)
        else:
            self.stage_card.setMinimumHeight(0)
            self.stage_card.setMaximumHeight(16777215)
            self._body.setAlignment(self.stage_card, Qt.AlignmentFlag(0))

    def _close_form(self):
        self.new_campaign_form = False
        self._stage_key = None
        self._render()

    def _render_chart(self, c):
        s = self.state
        threshold = None
        spec = (c or {}).get("spec") or {}
        if spec.get("vacuum") == "auto":
            from app.services.pressure import hpa_to_torr

            threshold = hpa_to_torr(spec.get("vacuum_start_hpa"))
        ch = s.get("chamber") or {}
        rule = (c or {}).get("pressure_rule") or {}
        self.chart.set_sensors((c or {}).get("thermocouples"))
        self.chart.update_series(
            s.get("series"),
            threshold,
            ch.get("duts"),
            self.tr("Waiting for the chamber's samples."),
            hold_limit=rule.get("hold_torr"),
        )

    def _render_banner(self):
        s = self.state
        d = s.get("demo")
        msgs, retry = [], False
        if d and d.get("starting"):
            msgs = [self.tr("Starting the chamber and the {0} AI…").format(self._mode())]
        else:
            if d:
                msgs += [
                    f"{x['label']}: {x['detail']}"
                    for x in d.get("steps") or []
                    if x.get("state") in ("fail", "warn")
                ]
                retry = not d.get("running") and any(
                    x.get("state") == "fail" for x in d.get("steps") or []
                )
            if not s.get("chambers") and not (d and not d.get("running")):
                msgs.append(
                    self.tr("No PLC chamber is registered: add one in the Controller page.")
                )
        api = not s.get("agent_api") and not (d and d.get("starting"))
        if api:
            msgs.append(self.tr("The run supervisor runs with the Agent API."))
        text = " · ".join(msgs)
        if self.banner_text.text() != text:
            self.banner_text.setText(text)
        self.retry_btn.setVisible(retry)
        self.api_btn.setVisible(api)
        self.banner.setVisible(bool(msgs))

    def _render_decision(self, c):
        hold = (c or {}).get("hold_point")
        if not hold:
            if self._dialog is not None:
                self._dialog.close()
                self._dialog = None
            return
        if self._dialog is None:
            self._dialog = DecisionDialog(self, hold, self.state.get("user"), self._decide)
            self._dialog.rejected.connect(self._dialog_closed)
            self._dialog.show()
        else:
            self._dialog.update_hold(hold, self.state.get("user"))

    def _dialog_closed(self):
        self._dialog = None

    def _decide(self, hold_id, choice):
        dialog = self._dialog
        self._dialog = None
        if dialog is not None:
            dialog.accept()
        self.act("decide", hold_id, choice)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self.view is not None and self.toast_label.isVisible():
            self.toast_label.move(
                (self.width() - self.toast_label.width()) // 2,
                self.height() - self.toast_label.height() - 24,
            )

    def sizeHint(self):  # noqa: N802
        return QSize(1280, 800)
