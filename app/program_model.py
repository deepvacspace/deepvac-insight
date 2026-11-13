"""Model of the program edited on the Controller page."""

from __future__ import annotations

import copy
import math

from PySide6.QtCore import QObject, Signal

from app.services import plc_gate

PARAM_DURATION = plc_gate.PARAM_TYPE_DURATION
PARAM_FLAGS = plc_gate.PARAM_TYPE_FLAGS
AWAIT_BIT = plc_gate.AWAIT_BIT
RAMP_BIT = plc_gate.RAMP_BIT
CUSTOM_RATE_BIT = 10
HOLD_UNTIL_STOPPED = plc_gate.LONG_TIME_S
INFINITE_CYCLES = 65535

PROG_SETPOINT = plc_gate.PROG_TYPE_SETPOINT
PROG_PROGRAM = plc_gate.PROG_TYPE_PROG

_RATE = "_rate"


def bit(flags: int, index: int) -> bool:
    return index >= 0 and bool(int(flags) & (1 << index))


def set_bit(flags: int, index: int, on: bool) -> int:
    if index < 0:
        return int(flags)
    return (int(flags) | (1 << index)) if on else (int(flags) & ~(1 << index))


def cycle_total_s(durations: list[float], cycle: dict) -> float | None:
    """Returns the program length with its cycle, or None."""
    total = 0.0
    for d in durations:
        if d >= HOLD_UNTIL_STOPPED:
            return None
        total += d
    count = int(cycle.get("count", 0))
    if count == INFINITE_CYCLES:
        return None
    if count > 1:
        start, end = int(cycle.get("start", 0)), int(cycle.get("end", 0))
        total += (count - 1) * sum(durations[start : end + 1])
    return total


class ProgramModel(QObject):
    number_of_intervals_changed = Signal(int)
    cycle_params_updated = Signal(dict)
    interval_selected = Signal(int)
    model_changed = Signal()

    def __init__(self, params: list[dict], parent=None):
        super().__init__(parent)
        self.params = [dict(p) for p in params]
        self.order = [str(p.get("name", "")) for p in self.params]
        self._intervals: list[dict] = []
        self._selected = 0
        self._cycle = {"start": 0, "end": 0, "count": 0}
        self.prog_type = PROG_PROGRAM

    def row_of(self, name: str) -> int:
        return self.order.index(name) if name in self.order else -1

    def param(self, row: int) -> dict:
        return self.params[row] if 0 <= row < len(self.params) else {}

    def param_type(self, row: int) -> int:
        return int(self.param(row).get("type", 0))

    @property
    def flags_row(self) -> int:
        return self.row_of("flags")

    def _default(self, row: int):
        p = self.param(row)
        value = p.get("value", 0)
        return self._typed(row, value)

    def _typed(self, row: int, value):
        p = self.param(row)
        kind = int(p.get("type", 0))
        if kind in (PARAM_DURATION, PARAM_FLAGS):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        if "min" in p and "max" in p and math.isfinite(number):
            number = min(max(number, float(p["min"])), float(p["max"]))
        return number

    def count(self) -> int:
        return len(self._intervals)

    def value(self, interval: int, name: str):
        return self._intervals[interval].get(name, 0)

    def value_at(self, row: int, interval: int):
        return self.value(interval, self.order[row])

    def set_value(self, interval: int, name: str, value) -> None:
        """Sets a value without emitting a signal."""
        row = self.row_of(name)
        if row < 0 or not 0 <= interval < self.count():
            return
        self._intervals[interval][name] = self._typed(row, value)

    def flags(self, interval: int) -> int:
        if not 0 <= interval < self.count():
            return 0
        return int(self._intervals[interval].get("flags", 0))

    def set_flags(self, interval: int, flags: int) -> None:
        if 0 <= interval < self.count():
            self._intervals[interval]["flags"] = int(flags)

    def rate(self, interval: int) -> float | None:
        if not 0 <= interval < self.count():
            return None
        return self._intervals[interval].get(_RATE)

    def set_rate(self, interval: int, rate: float | None) -> None:
        if 0 <= interval < self.count():
            if rate is None:
                self._intervals[interval].pop(_RATE, None)
            else:
                self._intervals[interval][_RATE] = float(rate)

    def interval_map(self, interval: int) -> dict:
        return {name: self._intervals[interval].get(name, 0) for name in self.order}

    def _interval_from_map(self, values: dict) -> dict:
        return {
            name: self._typed(row, values.get(name, self.param(row).get("value", 0)))
            for row, name in enumerate(self.order)
        }

    def selected(self) -> int:
        return self._selected

    def select(self, index: int) -> None:
        if not 0 <= index < self.count() or index == self._selected:
            return
        self._selected = index
        self.interval_selected.emit(index)

    def new_item(self, index: int) -> None:
        """Inserts a new interval."""
        if index > 0 and index - 1 < self.count():
            item = copy.deepcopy(self._intervals[index - 1])
        else:
            item = {name: self._default(row) for row, name in enumerate(self.order)}
        self._intervals.insert(max(0, min(index, self.count())), item)
        self.reset_cycle()
        self.number_of_intervals_changed.emit(self.count())

    def del_item(self, index: int) -> None:
        if not 0 <= index < self.count():
            return
        selected_before = self._selected
        del self._intervals[index]
        self.reset_cycle()
        self.number_of_intervals_changed.emit(self.count())
        if index == selected_before:
            self._selected = 0 if selected_before == 0 else selected_before - 1
            self.interval_selected.emit(self._selected)
        elif index < selected_before:
            self._selected = selected_before - 1

    def emit_changed(self) -> None:
        self.model_changed.emit()

    def cycle(self) -> dict:
        return dict(self._cycle)

    def set_cycle(self, cycle: dict) -> None:
        self._cycle = {
            "start": int(cycle.get("start", 0)),
            "end": int(cycle.get("end", 0)),
            "count": int(cycle.get("count", 0)),
        }

    def reset_cycle(self) -> None:
        self._cycle = {"start": 0, "end": 0, "count": 0}
        self.cycle_params_updated.emit(self.cycle())

    def program(self) -> dict:
        return {
            "intervals": [self.interval_map(i) for i in range(self.count())],
            "cycle": self.cycle(),
        }

    def set_program(self, program: dict) -> bool:
        """Loads a program; returns False when its channels don't match."""
        intervals = (program or {}).get("intervals") or []
        if not intervals or not isinstance(intervals[0], dict) or not intervals[0]:
            return False
        names = set(intervals[0])
        if not names <= set(self.order) or "duration" not in names or "flags" not in names:
            return False
        cycle = program.get("cycle") or {}
        ok_cycle = all(k in cycle for k in ("start", "end", "count"))
        self._cycle = (
            {k: int(cycle[k]) for k in ("start", "end", "count")}
            if ok_cycle
            else {"start": 0, "end": 0, "count": 0}
        )
        self._intervals = [self._interval_from_map(dict(i)) for i in intervals]
        self._selected = 0
        self.interval_selected.emit(0)
        self.cycle_params_updated.emit(self.cycle())
        self.number_of_intervals_changed.emit(self.count())
        return True

    def durations(self) -> list[float]:
        return [float(self.value(i, "duration")) for i in range(self.count())]

    def controlled_rows(self) -> list[int]:
        """Returns the rows an interval can set."""
        return [row for row in range(len(self.order)) if self.param_type(row) == 0]

    def interval_without_setpoint(self) -> int:
        """Returns the first interval without a setpoint, or -1."""
        rows = self.controlled_rows()
        for interval in range(self.count()):
            flags = self.flags(interval)
            if not any(bit(flags, row) for row in rows):
                return interval
        return -1
