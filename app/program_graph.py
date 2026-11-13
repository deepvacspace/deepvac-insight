"""Profile graph of the program on the Controller page."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.program_model import RAMP_BIT, ProgramModel, bit


def _tr(text):
    return QCoreApplication.translate("ProgramGraph", text)


def pressure_to_torr(value, label):
    """Converts a pressure to Torr."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return math.nan
    if not math.isfinite(value) or abs(value) > 1e9:
        return math.nan
    unit = label.rsplit(",", 1)[1].strip().lower() if "," in (label or "") else ""
    factor = {
        "mbar": 0.750062,
        "hpa": 0.750062,
        "pa": 0.00750062,
        "kpa": 7.50062,
        "bar": 750.062,
        "atm": 760.0,
    }.get(unit, 1.0)
    return value * factor


class ProgramGraph(QWidget):
    interval_double_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model: ProgramModel | None = None
        self._pressure_label = ""
        self._start_temp: float | None = None
        self._dark = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.showGrid(x=False, y=True, alpha=0.15)
        lay.addWidget(self.plot)
        self._vb = self.plot.getPlotItem().getViewBox()
        self._curves = {}
        self._boundaries = []
        self._pres_vb = None
        self._region = pg.LinearRegionItem(values=(0, 1), movable=False)
        self._region.setZValue(-10)
        for line in self._region.lines:
            line.setPen(pg.mkPen(None))
        self.plot.addItem(self._region)
        self.plot.scene().sigMouseClicked.connect(self._clicked)
        self.update_theme(True)

    def set_pressure_label(self, label):
        self._pressure_label = label or ""

    def set_start_temperature(self, temperature):
        same_kind = (temperature is None) == (self._start_temp is None)
        if same_kind and (temperature is None or abs(temperature - self._start_temp) < 0.1):
            return
        self._start_temp = temperature
        if self.model is not None:
            self.redraw()

    def set_model(self, model: ProgramModel):
        if model is self.model:
            return
        self.model = model
        item = self.plot.getPlotItem()
        for curve in self._curves.values():
            item.removeItem(curve)
        self._curves = {}
        if self._pres_vb is not None:
            item.scene().removeItem(self._pres_vb)
            self._pres_vb = None
            item.hideAxis("right")
        quantities = []
        for row, name in enumerate(model.order):
            p = model.param(row)
            if model.param_type(row) != 0:
                continue
            color = QColor(str(p.get("color", "")))
            if name == "pres":
                pen = pg.mkPen(color if color.isValid() else "#60a5fa", width=3)
                self._pres_vb = pg.ViewBox()
                self._pres_vb.setMouseEnabled(x=False, y=False)
                item.showAxis("right")
                item.scene().addItem(self._pres_vb)
                item.getAxis("right").linkToView(self._pres_vb)
                item.getAxis("right").setLogMode(True)
                item.getAxis("right").setLabel(_tr("Pressure, Torr"))
                self._pres_vb.setXLink(item)
                curve = pg.PlotDataItem(pen=pen, connect="finite")
                self._pres_vb.addItem(curve)
                self._vb.sigResized.connect(self._sync_views)
            else:
                pen = pg.mkPen(color if color.isValid() else "#ef4444", width=3)
                curve = self.plot.plot(pen=pen, connect="finite")
                quantities.append(
                    _tr("Temperature, °C") if name == "temp" else str(p.get("label", name))
                )
            self._curves[name] = curve
        item.setLabel("left", " | ".join(quantities))
        item.setLabel("bottom", _tr("Interval"))
        model.number_of_intervals_changed.connect(self.redraw)
        model.model_changed.connect(self.redraw)
        model.interval_selected.connect(self.select_interval)
        self.redraw()

    def _sync_views(self):
        if self._pres_vb is not None:
            self._pres_vb.setGeometry(self._vb.sceneBoundingRect())
            self._pres_vb.linkedViewChanged(self._vb, self._pres_vb.XAxis)

    def update_theme(self, dark):
        self._dark = dark
        self.plot.setBackground("#111827" if dark else "#ffffff")
        axis_color = "#94a3b8" if dark else "#64748b"
        for name in ("left", "bottom", "right"):
            axis = self.plot.getPlotItem().getAxis(name)
            axis.setPen(pg.mkPen(axis_color))
            axis.setTextPen(pg.mkPen(axis_color))
        self._region.setBrush(pg.mkBrush(96, 165, 250, 45 if dark else 35))
        for line in self._boundaries:
            line.setPen(pg.mkPen(axis_color, width=1, style=Qt.PenStyle.DotLine))

    def series(self):
        """Returns the drawn series."""
        m = self.model
        out = {name: ([], []) for name in self._curves}
        for i in range(m.count()):
            flags = m.flags(i)
            ramp = bit(flags, RAMP_BIT)
            for row, name in enumerate(m.order):
                if name not in out:
                    continue
                xs, ys = out[name]
                xs += [i, i + 1]
                value = float(m.value(i, name))
                controlled = bit(flags, row)
                if name == "temp":
                    start = (
                        (self._start_temp if self._start_temp is not None else 25.0)
                        if i == 0
                        else float(m.value(i - 1, "temp"))
                    )
                    ys += [
                        (start if ramp else value) if controlled else math.nan,
                        value if controlled else math.nan,
                    ]
                elif name == "pres":
                    torr = pressure_to_torr(value, self._pressure_label) if controlled else math.nan
                    shown = math.log10(torr) if torr > 0 else math.nan
                    ys += [shown, shown]
                else:
                    shown = value if controlled else math.nan
                    ys += [shown, shown]
        return out

    def redraw(self, *_):
        m = self.model
        if m is None:
            return
        data = self.series()
        for name, (xs, ys) in data.items():
            self._curves[name].setData(xs, ys)
        count = m.count()
        axis = self.plot.getPlotItem().getAxis("bottom")
        axis.setTicks([[(i + 0.5, str(i + 1)) for i in range(count)], []])
        for line in self._boundaries:
            self.plot.removeItem(line)
        axis_color = "#94a3b8" if self._dark else "#64748b"
        self._boundaries = []
        for i in range(1, count):
            line = pg.InfiniteLine(
                pos=i, angle=90, pen=pg.mkPen(axis_color, width=1, style=Qt.PenStyle.DotLine)
            )
            line.setZValue(-5)
            self.plot.addItem(line)
            self._boundaries.append(line)
        self.plot.setXRange(0, max(1, count), padding=0.01)
        values = [
            y
            for name, (_xs, ys) in data.items()
            if name != "pres"
            for y in ys
            if isinstance(y, float) and math.isfinite(y)
        ]
        if values:
            self.plot.setYRange(min(values) - 5, max(values) + 5, padding=0)
        if self._pres_vb is not None:
            logs = [y for y in data.get("pres", ([], []))[1] if math.isfinite(y)]
            if logs:
                low, high = math.floor(min(logs) - 0.5), math.ceil(max(logs) + 0.5)
            else:
                low, high = -3, 3
            self._pres_vb.setYRange(low, high, padding=0)
            self._sync_views()
        self.select_interval(m.selected())

    def select_interval(self, index):
        m = self.model
        self._region.setRegion((index, index + 1))
        self._region.setVisible(m is not None and m.count() > 1)

    def _index_at(self, scene_pos):
        if not self._vb.sceneBoundingRect().contains(scene_pos):
            return -1
        x = self._vb.mapSceneToView(scene_pos).x()
        index = math.ceil(x) - 1
        return index if self.model is not None and 0 <= index < self.model.count() else -1

    def _clicked(self, event):
        index = self._index_at(event.scenePos())
        if index < 0:
            return
        if event.double():
            self.interval_double_clicked.emit(index)
        else:
            self.model.select(index)
