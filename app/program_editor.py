"""Editing widgets of the Controller page."""

from __future__ import annotations

import contextlib
import json
import math
import re
from datetime import datetime

from PySide6.QtCore import (
    QCoreApplication,
    QDateTime,
    QEvent,
    QPoint,
    QSettings,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QDoubleValidator, QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.program_model import (
    AWAIT_BIT,
    CUSTOM_RATE_BIT,
    HOLD_UNTIL_STOPPED,
    INFINITE_CYCLES,
    RAMP_BIT,
    ProgramModel,
    bit,
    set_bit,
)

RATE_MIN, RATE_MAX = 0.01, 10.0
FULL_SPEED, LINEAR_RAMP, CUSTOM_RATE = 0, 1, 2


def _tr(text):
    return QCoreApplication.translate("ProgramEditor", text)


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def muted(text="", parent=None):
    label = QLabel(text, parent)
    label.setProperty("muted", True)
    return label


def bold(label):
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def number_text(value, prec=1):
    return f"{float(value):.{int(prec)}f}".replace("-", "−")


def hms(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def duration_text(total) -> str:
    total = int(total)
    if total >= 3600:
        return _tr("{0} h {1:02d} min").format(total // 3600, (total // 60) % 60)
    return _tr("{0} min").format((total + 59) // 60)


def sci(value) -> str:
    return f"{float(value):.2e}"


class NumberEdit(QLineEdit):
    """Numeric line edit with limits and precision."""

    user_editing_finished = Signal()

    def __init__(self, low, high, prec=1, parent=None):
        super().__init__(parent)
        self.low, self.high, self.prec = float(low), float(high), int(prec)
        self.scientific = False
        validator = QDoubleValidator(self)
        validator.setNotation(QDoubleValidator.Notation.ScientificNotation)
        self.setValidator(validator)
        self.editingFinished.connect(self._finished)
        self._last = None

    def set_scientific(self, on):
        self.scientific = bool(on)

    def set_range(self, low, high, prec=None):
        self.low, self.high = float(low), float(high)
        if prec is not None:
            self.prec = int(prec)

    def number(self):
        text = self.text().strip().replace("−", "-").replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        if self.low < self.high:
            value = min(max(value, self.low), self.high)
        return value

    def formatted(self, value) -> str:
        return sci(value) if self.scientific else f"{float(value):.{self.prec}f}"

    def _finished(self):
        if not self.isModified():
            return
        self.setModified(False)
        self.user_editing_finished.emit()


_DURATION = re.compile(r"^\s*(?:(\d+):)?(\d{1,3}):(\d{1,2})\s*$")


def parse_duration(text: str) -> int | None:
    """Seconds of "h:mm:ss", "mm:ss", "90m", "1h 30m", "45s" or a number of minutes."""
    text = (text or "").strip().lower()
    if not text:
        return None
    match = _DURATION.match(text)
    if match:
        h, m, s = match.groups()
        if h is None:
            return int(m) * 60 + int(s)
        return int(h) * 3600 + int(m) * 60 + int(s)
    units = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", text)
    if units and re.fullmatch(r"(\s*\d+(?:\.\d+)?\s*[hms]\s*)+", text):
        factor = {"h": 3600, "m": 60, "s": 1}
        return int(round(sum(float(n) * factor[u] for n, u in units)))
    try:
        return int(round(float(text) * 60))
    except ValueError:
        return None


class DurationEdit(QLineEdit):
    """Duration line edit in h:mm:ss."""

    user_edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._seconds = 0
        self.setToolTip(self.tr("h:mm:ss, or e.g. 90m, 1h 30m, 45s"))
        self.editingFinished.connect(self._finished)

    def seconds(self) -> int:
        return self._seconds

    def set_seconds(self, seconds):
        self._seconds = int(max(0, seconds))
        self.setText(hms(self._seconds))
        self.setModified(False)

    def _finished(self):
        if not self.isModified():
            return
        self.setModified(False)
        parsed = parse_duration(self.text())
        if parsed is None or parsed <= 0:
            self.setText(hms(self._seconds))
            return
        self._seconds = min(parsed, HOLD_UNTIL_STOPPED - 1)
        self.setText(hms(self._seconds))
        self.user_edited.emit()


class ElidedLabel(QLabel):
    """Label that elides text to fit."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""

    def set_full_text(self, text):
        self._full = text
        self.setToolTip(text)
        self.updateGeometry()
        self._elide()

    def full_text(self):
        return self._full

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        metrics = QFontMetrics(self.font())
        hint.setWidth(metrics.horizontalAdvance(self._full) + metrics.averageCharWidth())
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        shown = QFontMetrics(self.font()).elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())
        )
        if shown != self.text():
            self.setText(shown)


class ProgramStepper(QWidget):
    """The steps of an experiment one after the other, each a card with its number and title."""

    next_requested = Signal(int)
    current_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column = QWidget(self.area)
        column.setObjectName("stepperColumn")
        self._lay = QVBoxLayout(column)
        self._lay.setContentsMargins(0, 0, 8, 0)
        self._lay.setSpacing(8)
        self._lay.addStretch()
        self.area.setWidget(column)
        outer.addWidget(self.area)
        self._steps = []
        self._current = 0
        self._locked = False
        self._press_at = QPoint()

    def add_step(self, title, content) -> int:
        index = len(self._steps)
        card = QFrame(self)
        card.setObjectName("stepCard")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 10, 14, 12)
        card_lay.setSpacing(10)

        head = QFrame(card)
        head.setObjectName("stepHead")
        head.setProperty("step", index)
        head.installEventFilter(self)
        head_lay = QHBoxLayout(head)
        head_lay.setContentsMargins(0, 0, 0, 0)
        head_lay.setSpacing(10)
        number = QLabel(str(index + 1), head)
        number.setObjectName("stepNumber")
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number.setFixedSize(26, 26)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        title_lbl = QLabel(title, head)
        title_lbl.setObjectName("stepTitle")
        font = title_lbl.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.1)
        title_lbl.setFont(font)
        summary = muted("", head)
        summary.setWordWrap(True)
        texts.addWidget(title_lbl)
        texts.addWidget(summary)
        head_lay.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
        head_lay.addLayout(texts, 1)
        card_lay.addWidget(head)

        body = QWidget(card)
        body.setObjectName("stepBody")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(12)
        content.setParent(body)
        body_lay.addWidget(content)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        back = QPushButton(self.tr("Back"), body)
        nxt = QPushButton(self.tr("Next"), body)
        nxt.setObjectName("primaryButton")
        why = muted("", body)
        why.setWordWrap(True)
        for button in (back, nxt):
            button.setMinimumWidth(90)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        buttons.addWidget(back)
        buttons.addWidget(why, 1)
        buttons.addWidget(nxt)
        body_lay.addLayout(buttons)
        card_lay.addWidget(body)

        back.clicked.connect(
            lambda: (not self._locked and index > 0) and self.set_current(index - 1)
        )
        nxt.clicked.connect(lambda: self.next_requested.emit(index))

        self._lay.insertWidget(self._lay.count() - 1, card)
        self._steps.append(
            {
                "card": card,
                "head": head,
                "number": number,
                "title": title_lbl,
                "summary": summary,
                "header_widget": None,
                "body": body,
                "back": back,
                "next": nxt,
                "why": why,
            }
        )
        self._refresh()
        return index

    def set_header_widget(self, step, widget):
        if 0 <= step < len(self._steps):
            self._steps[step]["header_widget"] = widget
            self._steps[step]["head"].layout().addWidget(widget, 0, Qt.AlignmentFlag.AlignTop)
            self._refresh()

    def set_summary(self, step, text):
        if 0 <= step < len(self._steps) and self._steps[step]["summary"].text() != text:
            self._steps[step]["summary"].setText(text)
            self._refresh()

    def summary(self, step):
        return self._steps[step]["summary"].text()

    def set_next_enabled(self, step, enabled, why=""):
        if 0 <= step < len(self._steps):
            self._steps[step]["next"].setEnabled(enabled)
            self._steps[step]["why"].setText("" if enabled else why)

    def next_button(self, step):
        return self._steps[step]["next"]

    def set_current(self, step):
        if not 0 <= step < len(self._steps) or step == self._current:
            return
        self._current = step
        self._refresh()
        self.current_changed.emit(step)

    def current(self):
        return self._current

    def set_locked(self, locked):
        if locked != self._locked:
            self._locked = locked
            self._refresh()

    def locked(self):
        return self._locked

    def _refresh(self):
        last = len(self._steps) - 1
        for i, step in enumerate(self._steps):
            open_, done = i == self._current, i < self._current
            step["body"].setVisible(open_)
            step["summary"].setVisible(not open_ and bool(step["summary"].text()))
            if step["header_widget"] is not None:
                step["header_widget"].setVisible(open_)
            step["number"].setText("✓" if done else str(i + 1))
            step["head"].setCursor(
                Qt.CursorShape.PointingHandCursor
                if done and not self._locked
                else Qt.CursorShape.ArrowCursor
            )
            step["back"].setVisible(i > 0)
            step["back"].setEnabled(not self._locked)
            step["next"].setVisible(i < last)
            step["card"].setProperty("state", "open" if open_ else ("done" if done else "todo"))
            for widget in (step["card"], step["number"], step["title"]):
                _repolish(widget)

    def eventFilter(self, watched, event):
        step = watched.property("step") if isinstance(watched, QFrame) else None
        if step is not None:
            if event.type() == QEvent.Type.MouseButtonPress:
                self._press_at = event.globalPosition().toPoint()
            elif event.type() == QEvent.Type.MouseButtonRelease:
                moved = event.globalPosition().toPoint() - self._press_at
                if moved.manhattanLength() < 12 and not self._locked and step < self._current:
                    self.set_current(int(step))
        return super().eventFilter(watched, event)


_DETAILS_KEY = "controller/next_run"


def load_next_run() -> dict:
    """Loads the details saved for the next experiment."""
    try:
        data = json.loads(QSettings("DeepVac", "Insight").value(_DETAILS_KEY, "") or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save_next_run(details: dict) -> None:
    QSettings("DeepVac", "Insight").setValue(_DETAILS_KEY, json.dumps(details))


def _parse_number(text):
    try:
        value = float(str(text).strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class RunDetailsPanel(QWidget):
    """Form for the experiment's details."""

    edited = Signal()

    def __init__(self, user_name="", parent=None):
        super().__init__(parent)
        self._user_name = user_name or ""
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        def line(placeholder):
            edit = QLineEdit(self)
            edit.setPlaceholderText(placeholder)
            return edit

        def field(title, edit):
            label = muted("", self)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setText(title)
            lay.addWidget(label)
            lay.addWidget(edit)
            lay.addSpacing(6)
            return label

        required = " <span style='color:#ef4444'>*</span>"
        self.purpose = line(self.tr("Required"))
        self.operator = line(self.tr("Required"))
        self.dut = line(self.tr("Device under test, e.g. serial number"))
        self.mass = line(self.tr("e.g. 12.5"))
        self.density = line(self.tr("e.g. 2700"))
        for edit in (self.mass, self.density):
            validator = QDoubleValidator(0.0, 1e9, 6, edit)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            edit.setValidator(validator)
        field(self.tr("Name or purpose") + required, self.purpose)
        field(self.tr("Operator") + required, self.operator)
        field(self.tr("DUT"), self.dut)
        row = QHBoxLayout()
        row.setSpacing(8)
        for title, edit in (
            (self.tr("DUT mass, kg"), self.mass),
            (self.tr("DUT density, kg/m³"), self.density),
        ):
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(muted(title, self))
            column.addWidget(edit)
            row.addLayout(column, 1)
        lay.addLayout(row)

        self._store_timer = QTimer(self)
        self._store_timer.setSingleShot(True)
        self._store_timer.setInterval(400)
        self._store_timer.timeout.connect(self.store)
        for edit in (self.purpose, self.operator, self.dut, self.mass, self.density):
            edit.textChanged.connect(self._changed)
        self.load()

    def _changed(self, *_):
        if not self._loading:
            self._store_timer.start()
        self.edited.emit()

    def load(self):
        data = load_next_run()
        self._loading = True
        self.purpose.setText(data.get("purpose", ""))
        self.dut.setText(data.get("dut", ""))
        for edit, key in ((self.mass, "dut_mass_kg"), (self.density, "dut_density_kg_m3")):
            value = data.get(key)
            edit.setText(f"{value:g}" if isinstance(value, (int, float)) else "")
        operator = data.get("operator") or self._user_name
        self.operator.setText(operator)
        self._loading = False
        if not data.get("operator") and operator:
            self.store()

    def store(self):
        save_next_run(self.metadata())

    def metadata(self) -> dict:
        return {
            "purpose": self.purpose.text().strip(),
            "operator": self.operator.text().strip(),
            "dut": self.dut.text().strip(),
            "dut_mass_kg": _parse_number(self.mass.text()),
            "dut_density_kg_m3": _parse_number(self.density.text()),
        }

    def complete(self) -> bool:
        return bool(self.purpose.text().strip()) and bool(self.operator.text().strip())

    def fill_default_name(self):
        """Fills a default run name."""
        untitled = self.tr("Untitled")
        current = self.purpose.text().strip()
        pattern = re.compile(rf"^{re.escape(untitled)} \d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}$")
        if current and not pattern.match(current):
            return
        name = f"{untitled} {datetime.now():%Y-%m-%d %H:%M}"
        if name != current:
            self.purpose.setText(name)
            self.store()


class IntervalCardList(QWidget):
    """List of interval cards."""

    label_edited = Signal(int, str)
    duplicate_requested = Signal(int)
    hold_requested = Signal(int)
    delete_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model: ProgramModel | None = None
        self._labels: list[str] = []
        self._notes: list[str] = []
        self._pressure_label = ""
        self._start_temp: float | None = None
        self._editable = True
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(6)
        self._lay.addStretch()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._cards = []
        self._open = -1
        self._committing = False
        self._press_at = QPoint()
        self._editor = None
        self._w = {}

    def set_model(self, model):
        if self.model is not None:
            for signal, slot in (
                (self.model.number_of_intervals_changed, self._rebuild),
                (self.model.model_changed, self._model_changed),
                (self.model.interval_selected, self.open_card),
            ):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
        self.model = model
        if model is not None:
            model.number_of_intervals_changed.connect(self._rebuild)
            model.model_changed.connect(self._model_changed)
            model.interval_selected.connect(self.open_card)
        self._rebuild()

    def set_labels(self, labels):
        self._labels = list(labels)
        self._refresh_texts()
        edit = self._w.get("label")
        if edit is not None and not edit.hasFocus() and edit.text() != self._label(self._open):
            edit.setText(self._label(self._open))

    def set_notes(self, notes):
        if list(notes) != self._notes:
            self._notes = list(notes)
            self._refresh_texts()

    def set_editable(self, editable):
        self._editable = bool(editable)
        self._fill_editor()

    def set_pressure_label(self, label):
        self._pressure_label = label or ""
        self._fill_editor()

    def set_start_temperature(self, temperature):
        """Sets the temperature the first interval ramps from."""
        same_kind = (temperature is None) == (self._start_temp is None)
        if same_kind and (temperature is None or abs(temperature - self._start_temp) < 0.1):
            return
        self._start_temp = temperature
        self._refresh_texts()
        self._fill_editor()

    def prepare_for_start(self):
        """Updates the first interval's custom ramp time."""
        if self._apply_custom_rates():
            self._refresh_texts()
            self._fill_editor()

    def show_interval(self, interval):
        self.open_card(interval)

    def open_index(self):
        return self._open

    def make_hold(self, interval, seconds):
        """Turns an interval into a hold at the previous target."""
        m = self.model
        if m is None or not 0 <= interval < m.count():
            return
        flags = m.flags(interval)
        flags = set_bit(flags, m.row_of("temp"), True)
        flags = set_bit(flags, RAMP_BIT, False)
        flags = set_bit(flags, CUSTOM_RATE_BIT, False)
        flags = set_bit(flags, AWAIT_BIT, True)
        self._committing = True
        m.set_flags(interval, flags)
        m.set_rate(interval, None)
        m.set_value(interval, "duration", int(seconds))
        self._committing = False
        self._commit()

    def _label(self, interval):
        return self._labels[interval] if 0 <= interval < len(self._labels) else ""

    def _pressure_unit(self):
        label = self._pressure_label
        return label.rsplit(",", 1)[1].strip() if "," in label else "Torr"

    def _variable_row(self, flags):
        m = self.model
        temp, pres = m.row_of("temp"), m.row_of("pres")
        if temp < 0:
            return pres
        only_pressure = pres >= 0 and bit(flags, pres) and not bit(flags, temp)
        return pres if only_pressure else temp

    def _is_pressure(self, flags):
        row = self._variable_row(flags)
        return row >= 0 and row == self.model.row_of("pres")

    @staticmethod
    def mode_of(flags):
        if bit(flags, RAMP_BIT):
            return CUSTOM_RATE if bit(flags, CUSTOM_RATE_BIT) else LINEAR_RAMP
        return FULL_SPEED if bit(flags, AWAIT_BIT) else LINEAR_RAMP

    def ramp_change(self, interval):
        """Returns the temperature change of an interval."""
        m = self.model
        if m.row_of("temp") < 0:
            return None
        if interval < 1:
            if self._start_temp is None:
                return None
            return abs(float(m.value(0, "temp")) - self._start_temp)
        return abs(float(m.value(interval, "temp")) - float(m.value(interval - 1, "temp")))

    def ramp_rate(self, interval):
        """°C/min"""
        m = self.model
        if self.mode_of(m.flags(interval)) == CUSTOM_RATE and m.rate(interval) is not None:
            return m.rate(interval)
        change = self.ramp_change(interval)
        if change is None or m.row_of("duration") < 0:
            return None
        duration = float(m.value(interval, "duration"))
        return change / duration * 60.0 if duration > 0 else None

    def _apply_custom_rates(self) -> bool:
        """Updates custom ramp durations."""
        m = self.model
        if m is None or m.row_of("duration") < 0:
            return False
        changed = False
        for interval in range(m.count()):
            change = self.ramp_change(interval)
            if self.mode_of(m.flags(interval)) != CUSTOM_RATE or change is None:
                continue
            rate = m.rate(interval)
            if rate is None:
                duration = float(m.value(interval, "duration"))
                if duration > 0 and change > 0:
                    m.set_rate(interval, change / duration * 60.0)
                continue
            duration = int(round(change / rate * 60.0))
            if duration != int(m.value(interval, "duration")):
                m.set_value(interval, "duration", max(1, duration))
                changed = True
        return changed

    def _commit(self):
        self._committing = True
        self._apply_custom_rates()
        self.model.emit_changed()
        self._committing = False
        self._refresh_texts()
        self._fill_editor()

    def _model_changed(self):
        if self._committing:
            return
        if self._apply_custom_rates():
            self._commit()
        else:
            self._refresh_texts()
            self._fill_editor()

    def _end_until_stopped(self, interval):
        """Resets a ramp held until stopped."""
        m = self.model
        flags = m.flags(interval)
        if (
            m.row_of("duration") < 0
            or self._is_pressure(flags)
            or self.mode_of(flags) == FULL_SPEED
            or float(m.value(interval, "duration")) < HOLD_UNTIL_STOPPED
        ):
            return
        m.set_value(interval, "duration", 3600)

    def _close_editor(self):
        if self._editor is not None:
            self._editor.hide()
            self._editor.deleteLater()
        self._editor = None
        self._w = {}

    def _rebuild(self, *_):
        self._close_editor()
        for card in self._cards:
            card["frame"].hide()
            card["frame"].deleteLater()
        self._cards = []
        self._open = -1
        m = self.model
        if m is None:
            return
        if self._apply_custom_rates():
            self._committing = True
            m.emit_changed()
            self._committing = False
        for interval in range(m.count()):
            frame = QFrame(self)
            frame.setObjectName("intervalCard")
            card_lay = QVBoxLayout(frame)
            card_lay.setContentsMargins(8, 6, 8, 6)
            card_lay.setSpacing(6)
            head = QFrame(frame)
            head.setObjectName("intervalHead")
            head.setProperty("interval", interval)
            head.installEventFilter(self)
            head.setCursor(Qt.CursorShape.PointingHandCursor)
            head_lay = QHBoxLayout(head)
            head_lay.setContentsMargins(0, 0, 0, 0)
            head_lay.setSpacing(8)
            number = QLabel(f"{interval + 1:02d}", head)
            number.setObjectName("intervalNumber")
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number.setFixedSize(28, 22)
            texts = QVBoxLayout()
            texts.setSpacing(0)
            title = bold(QLabel(head))
            title.setObjectName("intervalTitle")
            subtitle = muted("", head)
            font = subtitle.font()
            font.setPointSizeF(font.pointSizeF() * 0.85)
            subtitle.setFont(font)
            texts.addWidget(title)
            texts.addWidget(subtitle)
            chevron = muted("", head)
            head_lay.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
            head_lay.addLayout(texts, 1)
            head_lay.addWidget(chevron)
            card_lay.addWidget(head)
            self._lay.insertWidget(self._lay.count() - 1, frame)
            self._cards.append(
                {
                    "frame": frame,
                    "head": head,
                    "number": number,
                    "title": title,
                    "subtitle": subtitle,
                    "chevron": chevron,
                }
            )
        self._refresh_texts()
        if m.count():
            self.open_card(min(max(0, m.selected()), m.count() - 1))

    def card_title(self, interval):
        return self._cards[interval]["title"].text()

    def card_subtitle(self, interval):
        return self._cards[interval]["subtitle"].text()

    def _refresh_texts(self):
        m = self.model
        if m is None or len(self._cards) != m.count():
            return
        temp_row, duration_row, flags_row = m.row_of("temp"), m.row_of("duration"), m.flags_row
        for interval, card in enumerate(self._cards):
            flags = m.flags(interval)
            if temp_row >= 0 and bit(flags, temp_row):
                target = number_text(m.value(interval, "temp"), m.param(temp_row).get("prec", 1))
                rate = self.ramp_rate(interval)
                if self.mode_of(flags) == CUSTOM_RATE and rate is not None:
                    title = self.tr("Ramp to {0} °C at {1} °C/min").format(
                        target, number_text(rate, 2)
                    )
                elif bit(flags, RAMP_BIT):
                    title = self.tr("Ramp to {0} °C").format(target)
                elif bit(flags, AWAIT_BIT):
                    title = self.tr("Go to {0} °C and hold").format(target)
                else:
                    title = self.tr("Go to {0} °C").format(target)
            else:
                others = []
                for row, name in enumerate(m.order):
                    if row in (duration_row, flags_row, temp_row) or not bit(flags, row):
                        continue
                    p = m.param(row)
                    value = m.value(interval, name)
                    shown = (
                        sci(value)
                        if p.get("notation") == "scientific" or name == "pres"
                        else number_text(value, p.get("prec", 1))
                    )
                    others.append(f"{p.get('label', name)} {shown}")
                title = " · ".join(others) if others else self.tr("No setpoint")
            card["title"].setText(title)
            duration = int(m.value(interval, "duration")) if duration_row >= 0 else 0
            parts = [self.tr("until stopped") if duration >= HOLD_UNTIL_STOPPED else hms(duration)]
            if interval < len(self._notes) and self._notes[interval]:
                parts.append(self._notes[interval])
            if self._label(interval):
                parts.append(self._label(interval))
            card["subtitle"].setText("  ·  ".join(parts))
            card["chevron"].setText("▴" if interval == self._open else "▾")

    def open_card(self, interval):
        if interval == self._open or not 0 <= interval < len(self._cards):
            return
        self._close_editor()
        self._open = interval
        self._build_editor(interval)
        if self.model is not None:
            self.model.select(interval)
        self._style_cards()
        self._refresh_texts()
        frame = self._cards[interval]["frame"]
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if parent is not None:
            QTimer.singleShot(0, lambda a=parent, f=frame: self._ensure_visible(a, f))

    def _ensure_visible(self, area, frame):
        try:
            if frame.isVisible():
                area.ensureWidgetVisible(frame, 0, 8)
                area.horizontalScrollBar().setValue(0)
        except RuntimeError:
            pass

    def close_card(self):
        self._close_editor()
        self._open = -1
        self._style_cards()
        self._refresh_texts()

    def _style_cards(self):
        for interval, card in enumerate(self._cards):
            card["frame"].setProperty("open", interval == self._open)
            _repolish(card["frame"])
            _repolish(card["number"])

    def eventFilter(self, watched, event):
        interval = watched.property("interval") if isinstance(watched, QFrame) else None
        if interval is not None:
            if event.type() == QEvent.Type.MouseButtonPress:
                self._press_at = event.globalPosition().toPoint()
            elif event.type() == QEvent.Type.MouseButtonRelease:
                moved = event.globalPosition().toPoint() - self._press_at
                if moved.manhattanLength() < 12:
                    if int(interval) == self._open:
                        self.close_card()
                    else:
                        self.open_card(int(interval))
        return super().eventFilter(watched, event)

    def _build_editor(self, interval):
        m = self.model
        card = self._cards[interval]
        editor = QWidget(card["frame"])
        self._editor = editor
        lay = QVBoxLayout(editor)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        field = [0]
        w = self._w = {"values": {}}

        def place(edit, at):
            grid.addWidget(edit, (at // 2) * 2 + 1, at % 2)
            edit.setMinimumWidth(0)
            edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        def add_field(title, edit):
            label = muted(title, editor)
            grid.addWidget(label, (field[0] // 2) * 2, field[0] % 2)
            place(edit, field[0])
            field[0] += 1
            return label

        temp_row, pres_row = m.row_of("temp"), m.row_of("pres")

        label_edit = QLineEdit(editor)
        label_edit.setPlaceholderText(self.tr("Optional"))
        add_field(self.tr("Label"), label_edit)
        label_edit.textChanged.connect(self._label_changed)
        w["label"] = label_edit

        variable = QComboBox(editor)
        for row in (temp_row, pres_row):
            if row >= 0:
                variable.addItem(str(m.param(row).get("label", m.order[row])), row)
        add_field(self.tr("Variable"), variable)
        variable.activated.connect(self._variable_chosen)
        w["variable"] = variable

        setpoint_field = field[0]
        for row in (temp_row, pres_row):
            if row < 0 or m.param_type(row) != 0:
                continue
            edit = self._value_edit(row, editor)
            if "setpoint_label" not in w:
                w["setpoint_label"] = add_field(self.tr("Setpoint"), edit)
            else:
                place(edit, setpoint_field)

        mode = QComboBox(editor)
        mode.addItem(self.tr("Full speed, then hold"), FULL_SPEED)
        mode.addItem(self.tr("Linear ramp"), LINEAR_RAMP)
        mode.addItem(self.tr("Custom ramp rate"), CUSTOM_RATE)
        mode.setPlaceholderText("—")
        add_field(self.tr("Mode"), mode)
        mode.activated.connect(self._mode_chosen)
        w["mode"] = mode

        if m.row_of("duration") >= 0:
            duration = DurationEdit(editor)
            w["duration_label"] = add_field(self.tr("Duration"), duration)
            duration.user_edited.connect(self._duration_edited)
            w["duration"] = duration
            until = QCheckBox(self.tr("Hold until stopped"), editor)
            until.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            until.setToolTip(self.tr("No hold time: held until Stop, or until Start replaces it"))
            until.clicked.connect(self._until_stopped_clicked)
            w["until"] = until

        rate = NumberEdit(RATE_MIN, RATE_MAX, 2, editor)
        add_field(self.tr("Ramp rate, °C/min"), rate)
        rate.user_editing_finished.connect(self._rate_edited)
        w["rate"] = rate

        for row in range(len(m.order)):
            if row not in (temp_row, pres_row) and m.param_type(row) == 0:
                add_field(
                    str(m.param(row).get("label", m.order[row])), self._value_edit(row, editor)
                )

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        if "until" in w:
            lay.addWidget(w["until"])

        actions = QHBoxLayout()
        dup = QPushButton(self.tr("Duplicate"), editor)
        dup.setToolTip(self.tr("A copy of this interval right after it"))
        hold = QPushButton(self.tr("Then hold"), editor)
        hold.setToolTip(
            self.tr(
                "Right after this ramp: an interval that holds its target (full speed, then hold)"
            )
        )
        delete = QPushButton(self.tr("Delete"), editor)
        delete.setObjectName("dangerButton")
        for button in (dup, hold, delete):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        actions.addWidget(dup)
        actions.addWidget(hold)
        actions.addStretch()
        actions.addWidget(delete)
        lay.addLayout(actions)
        dup.clicked.connect(lambda: self._open >= 0 and self.duplicate_requested.emit(self._open))
        hold.clicked.connect(lambda: self._open >= 0 and self.hold_requested.emit(self._open))
        delete.clicked.connect(lambda: self._open >= 0 and self.delete_requested.emit(self._open))
        w.update({"duplicate": dup, "hold": hold, "delete": delete})

        card["frame"].layout().addWidget(editor)
        self._fill_editor()

    def editor_widget(self, name):
        return self._w.get(name)

    def _value_edit(self, row, parent):
        m = self.model
        p = m.param(row)
        edit = NumberEdit(p.get("min", -1e12), p.get("max", 1e12), p.get("prec", 1), parent)
        edit.set_scientific(p.get("notation") == "scientific")
        self._w["values"][row] = edit
        edit.user_editing_finished.connect(lambda r=row, e=edit: self._value_edited(r, e))
        return edit

    def _label_changed(self, text):
        if self._open >= 0 and text.strip() != self._label(self._open):
            self.label_edited.emit(self._open, text.strip())

    def _variable_chosen(self, index):
        m = self.model
        if self._open < 0 or m is None:
            return
        row = self._w["variable"].itemData(index)
        temp_row, pres_row = m.row_of("temp"), m.row_of("pres")
        flags = m.flags(self._open)
        for variable in (temp_row, pres_row):
            if variable >= 0:
                flags = set_bit(flags, variable, variable == row)
        if row == pres_row:
            flags = set_bit(flags, RAMP_BIT, False)
            flags = set_bit(flags, AWAIT_BIT, False)
            flags = set_bit(flags, CUSTOM_RATE_BIT, False)
        elif not bit(flags, RAMP_BIT) and not bit(flags, AWAIT_BIT):
            flags = set_bit(flags, RAMP_BIT, True)
        self._committing = True
        m.set_flags(self._open, flags)
        self._end_until_stopped(self._open)
        self._committing = False
        self._commit()

    def _value_edited(self, row, edit):
        m = self.model
        if self._open < 0 or m is None:
            return
        value = edit.number()
        if value is None:
            self._fill_editor()
            return
        self._committing = True
        m.set_value(self._open, m.order[row], value)
        flags = set_bit(m.flags(self._open), row, True)
        if row == m.row_of("temp") and not bit(flags, RAMP_BIT) and not bit(flags, AWAIT_BIT):
            flags = set_bit(flags, RAMP_BIT, True)
        m.set_flags(self._open, flags)
        self._committing = False
        self._commit()

    def _mode_chosen(self, index):
        m = self.model
        if self._open < 0 or m is None:
            return
        mode = self._w["mode"].itemData(index)
        flags = m.flags(self._open)
        rate = None if self.mode_of(flags) == FULL_SPEED else self.ramp_rate(self._open)
        flags = set_bit(flags, RAMP_BIT, mode != FULL_SPEED)
        flags = set_bit(flags, AWAIT_BIT, mode == FULL_SPEED)
        flags = set_bit(flags, CUSTOM_RATE_BIT, mode == CUSTOM_RATE)
        self._committing = True
        m.set_flags(self._open, flags)
        m.set_rate(
            self._open,
            min(max(rate if rate else 1.0, RATE_MIN), RATE_MAX) if mode == CUSTOM_RATE else None,
        )
        self._end_until_stopped(self._open)
        self._committing = False
        self._commit()

    def _duration_edited(self):
        if self._open < 0 or self.model is None:
            return
        self._committing = True
        self.model.set_value(self._open, "duration", self._w["duration"].seconds())
        self._committing = False
        self._commit()

    def _until_stopped_clicked(self, checked):
        if self._open < 0 or self.model is None:
            return
        self._committing = True
        self.model.set_value(self._open, "duration", HOLD_UNTIL_STOPPED if checked else 3600)
        self._committing = False
        self._commit()

    def _rate_edited(self):
        if self._open < 0 or self.model is None:
            return
        rate = self._w["rate"].number()
        if rate is None or rate <= 0:
            self._fill_editor()
            return
        self._committing = True
        self.model.set_rate(self._open, min(max(rate, RATE_MIN), RATE_MAX))
        self._committing = False
        self._commit()

    def _fill_editor(self):
        m, w = self.model, self._w
        if self._editor is None or m is None or not 0 <= self._open < m.count():
            return
        i = self._open
        flags = m.flags(i)
        temp_row, pres_row = m.row_of("temp"), m.row_of("pres")
        variable = self._variable_row(flags)
        pressure = self._is_pressure(flags)
        mode = self.mode_of(flags)
        ramp_known = self.ramp_change(i) is not None
        custom = mode == CUSTOM_RATE and ramp_known

        label = w["label"]
        if not label.hasFocus() and label.text() != self._label(i):
            label.blockSignals(True)
            label.setText(self._label(i))
            label.blockSignals(False)
        label.setEnabled(self._editable)

        combo = w["variable"]
        combo.setCurrentIndex(combo.findData(variable))
        combo.setEnabled(self._editable and combo.count() > 1)

        for row, edit in w["values"].items():
            if not edit.hasFocus():
                edit.setText(edit.formatted(m.value_at(row, i)))
                edit.setModified(False)
            edit.setEnabled(self._editable)
            if row in (temp_row, pres_row):
                edit.setVisible(row == variable)
        if "setpoint_label" in w:
            w["setpoint_label"].setText(
                self.tr("Setpoint, {0}").format(self._pressure_unit())
                if pressure
                else self.tr("Setpoint, °C")
            )

        mode_combo = w["mode"]
        if pressure:
            mode_combo.setCurrentIndex(-1)
            mode_combo.setToolTip(
                self.tr(
                    "Pressure goes to its setpoint directly: ramps and holds are for temperature only"
                )
            )
        else:
            mode_combo.setCurrentIndex(mode_combo.findData(mode))
            mode_combo.setToolTip(
                self.tr(
                    "Full speed: to the target as fast as possible, then held for the hold time.\n"
                    "Linear ramp: from the previous target to this one over the ramp time.\n"
                    "Custom ramp rate: a linear ramp at the rate given; its ramp time is calculated."
                )
            )
        mode_combo.setEnabled(self._editable and not pressure)
        item = mode_combo.model().item(mode_combo.findData(CUSTOM_RATE))
        if item is not None:
            item.setEnabled(ramp_known)

        if "duration" in w:
            duration = float(m.value(i, "duration"))
            until_stopped = duration >= HOLD_UNTIL_STOPPED
            holds = pressure or mode == FULL_SPEED
            edit = w["duration"]
            if until_stopped:
                edit.clear()
                edit.setPlaceholderText(self.tr("Until stopped"))
            elif not edit.hasFocus():
                edit.set_seconds(duration)
            w["duration_label"].setText(
                self.tr("Duration")
                if pressure
                else (self.tr("Hold time") if mode == FULL_SPEED else self.tr("Ramp time"))
            )
            edit.setEnabled(self._editable and (pressure or not custom) and not until_stopped)
            w["until"].setChecked(until_stopped)
            w["until"].setVisible(until_stopped or (holds and i == len(self._cards) - 1))
            w["until"].setEnabled(self._editable)

        ramp = not pressure and mode != FULL_SPEED
        rate = self.ramp_rate(i) if ramp else None
        rate_edit = w["rate"]
        if not rate_edit.hasFocus():
            rate_edit.setText(rate_edit.formatted(rate) if rate is not None else "")
            rate_edit.setModified(False)
        rate_edit.setEnabled(self._editable and ramp and custom)
        if not ramp:
            tip = ""
        elif not ramp_known:
            tip = self.tr(
                "The first interval ramps from the chamber's temperature, which is not known now"
            )
        elif custom:
            tip = (
                self.tr(
                    "From the chamber's temperature: the ramp time is calculated again at Start"
                )
                if i == 0
                else ""
            )
        elif i == 0:
            tip = self.tr(
                "Calculated: the change from the chamber's temperature now over the ramp time"
            )
        else:
            tip = self.tr("Calculated: the change from the previous target over the ramp time")
        rate_edit.setToolTip(tip)

        w["duplicate"].setEnabled(self._editable)
        w["hold"].setVisible(ramp)
        w["hold"].setEnabled(self._editable)
        w["delete"].setVisible(i > 0)
        w["delete"].setEnabled(self._editable)


def cycle_text(cycle: dict) -> str:
    count = int(cycle.get("count", 0))
    if count <= 0:
        return _tr("None")
    times = _tr("infinite") if count == INFINITE_CYCLES else str(count)
    return _tr("Intervals {0} – {1} × {2}").format(
        int(cycle.get("start", 0)) + 1, int(cycle.get("end", 0)) + 1, times
    )


class CycleDialog(QDialog):
    """Dialog to set the program cycle."""

    def __init__(self, cycle: dict, intervals: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Edit cycle"))
        self.result_cycle = dict(cycle)
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        self.start = QSpinBox(self)
        self.end = QSpinBox(self)
        self.count = QSpinBox(self)
        for spin in (self.start, self.end):
            spin.setRange(1, max(1, intervals))
        self.count.setRange(2, 1000)
        self.infinite = QCheckBox(self.tr("Infinite repeat"), self)
        if int(cycle.get("count", 0)) > 1:
            self.start.setValue(int(cycle.get("start", 0)) + 1)
            self.end.setValue(int(cycle.get("end", 0)) + 1)
            if int(cycle["count"]) == INFINITE_CYCLES:
                self.count.setValue(3)
                self.infinite.setChecked(True)
            else:
                self.count.setValue(int(cycle["count"]))
        else:
            self.start.setValue(1)
            self.end.setValue(max(1, intervals))
            self.count.setValue(3)
        self.count.setEnabled(not self.infinite.isChecked())
        self.infinite.toggled.connect(lambda on: self.count.setEnabled(not on))
        self.start.valueChanged.connect(lambda v: self.end.setMinimum(v))
        self.end.valueChanged.connect(lambda v: self.start.setMaximum(v))
        self.end.setMinimum(self.start.value())
        self.start.setMaximum(self.end.value())
        grid.addWidget(QLabel(self.tr("Start interval:")), 0, 0)
        grid.addWidget(self.start, 0, 1)
        grid.addWidget(QLabel(self.tr("End interval:")), 0, 2)
        grid.addWidget(self.end, 0, 3)
        grid.addWidget(QLabel(self.tr("Repeat count:")), 1, 0)
        grid.addWidget(self.count, 1, 1)
        grid.addWidget(self.infinite, 1, 2, 1, 2)
        lay.addLayout(grid)
        buttons = QHBoxLayout()
        delete = QPushButton(self.tr("Delete"), self)
        cancel = QPushButton(self.tr("Cancel"), self)
        save = QPushButton(self.tr("Save"), self)
        save.setObjectName("primaryButton")
        buttons.addWidget(delete)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        lay.addLayout(buttons)
        cancel.clicked.connect(self.reject)
        delete.clicked.connect(self._delete)
        save.clicked.connect(self._save)

    def _delete(self):
        self.result_cycle = {"start": 0, "end": 0, "count": 0}
        self.accept()

    def _save(self):
        self.result_cycle = {
            "start": self.start.value() - 1,
            "end": self.end.value() - 1,
            "count": INFINITE_CYCLES if self.infinite.isChecked() else self.count.value(),
        }
        self.accept()


class CycleWidget(QWidget):
    """The cycle in words, and Edit."""

    params_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._params = {"start": 0, "end": 0, "count": 0}
        self._intervals = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.text = bold(QLabel(self.tr("None"), self))
        self.edit_btn = QPushButton(self.tr("Edit"), self)
        self.edit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.edit_btn)
        self.edit_btn.clicked.connect(self._edit)

    def params(self):
        return dict(self._params)

    def set_params(self, params):
        params = {k: int(params.get(k, 0)) for k in ("start", "end", "count")}
        if params != self._params:
            self._params = params
            self.text.setText(cycle_text(params))

    def set_intervals_count(self, count):
        if count <= self._params.get("end", 0) and self._params.get("count", 0):
            self.set_params({"start": 0, "end": 0, "count": 0})
            self.params_changed.emit(self.params())
        self._intervals = count

    def _edit(self):
        dialog = CycleDialog(self._params, self._intervals, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._params = dialog.result_cycle
            self.text.setText(cycle_text(self._params))
            self.params_changed.emit(self.params())


class DelaySelectDialog(QDialog):
    """Dialog to choose when the program starts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Delayed start"))
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        self.now_edit = QDateTimeEdit(QDateTime.currentDateTime(), self)
        self.now_edit.setDisplayFormat("yyyy-MM-dd   HH:mm:ss")
        self.now_edit.setReadOnly(True)
        self.start_edit = QDateTimeEdit(QDateTime.currentDateTime().addSecs(60), self)
        self.start_edit.setDisplayFormat("yyyy-MM-dd   HH:mm:ss")
        self.start_edit.setCalendarPopup(True)
        for edit in (self.now_edit, self.start_edit):
            font = edit.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 1.6)
            edit.setFont(font)
        grid.addWidget(QLabel(self.tr("Current time:")), 0, 0)
        grid.addWidget(self.now_edit, 0, 1)
        grid.addWidget(QLabel(self.tr("Start time:")), 1, 0)
        grid.addWidget(self.start_edit, 1, 1)
        lay.addLayout(grid)
        buttons = QHBoxLayout()
        ok = QPushButton(self.tr("Set"), self)
        ok.setObjectName("primaryButton")
        cancel = QPushButton(self.tr("Cancel"), self)
        buttons.addWidget(ok)
        buttons.addStretch()
        buttons.addWidget(cancel)
        lay.addLayout(buttons)
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    def _tick(self):
        now = QDateTime.currentDateTime()
        self.now_edit.setDateTime(now)
        if self.start_edit.dateTime() < now:
            self.start_edit.setDateTime(now.addSecs(60))
        self.start_edit.setMinimumDateTime(now)

    def start_time(self) -> QDateTime:
        return self.start_edit.dateTime()


class DelayCountdownDialog(QDialog):
    """Dialog that waits for the program's start time."""

    def __init__(self, title, start: QDateTime, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self._start = start
        lay = QVBoxLayout(self)
        self.info = QLabel(self)
        font = self.info.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.info.setFont(font)
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.info)
        buttons = QHBoxLayout()
        now_btn = QPushButton(self.tr("Start now"), self)
        cancel = QPushButton(self.tr("Cancel"), self)
        buttons.addWidget(now_btn)
        buttons.addStretch()
        buttons.addWidget(cancel)
        lay.addLayout(buttons)
        now_btn.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)
        self._tick()

    def _tick(self):
        left = QDateTime.currentDateTime().secsTo(self._start)
        if left <= 0:
            self._timer.stop()
            self.accept()
            return
        self.info.setText(
            self.tr("The program starts at {0}\n(in {1})").format(
                self._start.toString("yyyy-MM-dd HH:mm:ss"), hms(left)
            )
        )
