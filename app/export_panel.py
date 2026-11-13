"""Export panel of the Analysis tabs."""

from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

VIEW, EXPERIMENT, DAY, CUSTOM = "view", "experiment", "day", "custom"
IMAGE_FORMATS = ("png", "bmp", "jpeg")
DATA_FORMATS = ("csv", "txt")
PREVIEW_ROWS = 2000


def is_image(fmt: str) -> bool:
    return fmt in IMAGE_FORMATS


def is_data(fmt: str) -> bool:
    return fmt in DATA_FORMATS


def extension(fmt: str) -> str:
    return {"comparison-csv": "csv", "jpeg": "jpg"}.get(fmt, fmt)


class ExportPanel(QWidget):
    export_requested = Signal(str, str)
    preview_requested = Signal(str)

    def __init__(
        self,
        can_report=True,
        can_bundle=True,
        experiment_label=None,
        parent=None,
    ):
        super().__init__(parent)
        self._day = date.today()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(12)

        format_card = self._card()
        format_lay = QVBoxLayout(format_card)
        format_lay.setContentsMargins(14, 12, 14, 14)
        format_lay.setSpacing(6)
        self._formats = QButtonGroup(self)
        self._formats.setExclusive(True)
        self._format_chips = {}

        def group(title, formats, note=""):
            format_lay.addWidget(self._caption(title))
            row = QHBoxLayout()
            row.setSpacing(6)
            for fmt, label, enabled in formats:
                chip = QPushButton(label)
                chip.setObjectName("exportChip")
                chip.setCheckable(True)
                chip.setMinimumSize(64, 30)
                chip.setFocusPolicy(Qt.NoFocus)
                chip.setProperty("format", fmt)
                chip.setEnabled(enabled)
                if not enabled and note:
                    chip.setToolTip(note)
                self._formats.addButton(chip)
                self._format_chips[fmt] = chip
                row.addWidget(chip)
            row.addStretch(1)
            format_lay.addLayout(row)
            format_lay.addSpacing(4)
            return row

        group(
            self.tr("Chart image"),
            [("png", "PNG", True), ("bmp", "BMP", True), ("jpeg", "JPEG", True)],
        )
        group(self.tr("Data"), [("csv", "CSV", True), ("txt", "TXT", True)])
        group(
            self.tr("Experiment"),
            [
                ("html", self.tr("Report HTML"), can_report),
                ("pdf", self.tr("Report PDF"), can_report),
                ("json", self.tr("Bundle JSON"), can_bundle),
            ],
            self.tr("Only for runs saved in the database"),
        )
        self._comparison_caption = self._caption(self.tr("Comparison"))
        format_lay.addWidget(self._comparison_caption)
        comparison_row = QHBoxLayout()
        chip = QPushButton(self.tr("Comparison CSV"))
        chip.setObjectName("exportChip")
        chip.setCheckable(True)
        chip.setMinimumSize(64, 30)
        chip.setFocusPolicy(Qt.NoFocus)
        chip.setProperty("format", "comparison-csv")
        self._formats.addButton(chip)
        self._format_chips["comparison-csv"] = chip
        comparison_row.addWidget(chip)
        comparison_row.addStretch(1)
        format_lay.addLayout(comparison_row)
        self._format_chips["png"].setChecked(True)
        lay.addWidget(format_card)

        range_card = self._card()
        range_lay = QVBoxLayout(range_card)
        range_lay.setContentsMargins(14, 12, 14, 14)
        range_lay.setSpacing(6)
        range_lay.addWidget(self._caption(self.tr("Range")))
        self._ranges = QButtonGroup(self)
        self._range_buttons = {}
        for key, text in (
            (VIEW, self.tr("Visible view")),
            (EXPERIMENT, experiment_label or self.tr("Whole experiment shown")),
            (DAY, ""),
            (CUSTOM, self.tr("Custom")),
        ):
            radio = QRadioButton(text)
            radio.setProperty("range", key)
            self._ranges.addButton(radio)
            self._range_buttons[key] = radio
            range_lay.addWidget(radio)
        self._range_buttons[VIEW].setChecked(True)

        grid = QGridLayout()
        grid.setContentsMargins(24, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        self._from_edit = QDateTimeEdit()
        self._to_edit = QDateTimeEdit()
        self._chamber_combo = QComboBox()
        self._file_widgets = []
        for row, (label, widget) in enumerate(
            (
                (self.tr("From"), self._from_edit),
                (self.tr("To"), self._to_edit),
                (self.tr("Chamber"), self._chamber_combo),
            )
        ):
            if isinstance(widget, QDateTimeEdit):
                widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
                widget.setCalendarPopup(True)
            name = QLabel(label)
            grid.addWidget(name, row, 0)
            grid.addWidget(widget, row, 1)
            self._file_widgets += [name, widget]
        range_lay.addLayout(grid)
        self._range_note = QLabel()
        self._range_note.setObjectName("analysisMeta")
        self._range_note.setWordWrap(True)
        range_lay.addWidget(self._range_note)
        lay.addWidget(range_card)

        buttons = QHBoxLayout()
        self._preview_btn = QPushButton(self.tr("Preview"))
        export_btn = QPushButton(self.tr("Export…"))
        export_btn.setObjectName("primaryButton")
        for button in (self._preview_btn, export_btn):
            button.setMinimumSize(100, 32)
            button.setFocusPolicy(Qt.NoFocus)
        buttons.addWidget(self._preview_btn)
        buttons.addWidget(export_btn, 1)
        lay.addStretch(1)
        lay.addLayout(buttons)

        self._formats.buttonToggled.connect(self._update_state)
        self._ranges.buttonToggled.connect(self._update_state)
        export_btn.clicked.connect(lambda: self.export_requested.emit(self.format(), self.range()))
        self._preview_btn.clicked.connect(lambda: self.preview_requested.emit(self.range()))
        self.set_comparison_available(False)
        self.set_chambers([])

    def set_day(self, day: date, start_epoch: float | None = None, end_epoch: float | None = None):
        """Sets the day of the data shown."""
        self._day = day
        self._range_buttons[DAY].setText(self.tr("Day {0}").format(f"{day:%Y-%m-%d}"))
        start = (
            datetime.fromtimestamp(start_epoch) if start_epoch else datetime.combine(day, time())
        )
        end = (
            datetime.fromtimestamp(end_epoch)
            if end_epoch
            else datetime.combine(day, time(23, 59, 59))
        )
        self._from_edit.setDateTime(QDateTime(start))
        self._to_edit.setDateTime(QDateTime(end))

    def set_chambers(self, chambers: list[str], current: str | None = None):
        """Sets the chambers with day recordings."""
        self._chamber_combo.clear()
        for name in chambers:
            self._chamber_combo.addItem(name)
        if current and current in chambers:
            self._chamber_combo.setCurrentText(current)
        can_use_files = bool(chambers)
        for key in (DAY, CUSTOM):
            self._range_buttons[key].setVisible(can_use_files)
        for widget in self._file_widgets:
            widget.setVisible(can_use_files)
        if not can_use_files and self.range() in (DAY, CUSTOM):
            self._range_buttons[VIEW].setChecked(True)
        self._update_state()

    def set_comparison_available(self, available: bool):
        self._comparison_caption.setVisible(available)
        self._format_chips["comparison-csv"].setVisible(available)
        if not available and self.format() == "comparison-csv":
            self._format_chips["png"].setChecked(True)

    def format(self) -> str:
        button = self._formats.checkedButton()
        return button.property("format") if button else ""

    def select_format(self, fmt: str):
        self._format_chips[fmt].setChecked(True)

    def range(self) -> str:
        button = self._ranges.checkedButton()
        return button.property("range") if button else VIEW

    def select_range(self, key: str):
        self._range_buttons[key].setChecked(True)

    def from_epoch(self) -> float:
        if self.range() == DAY:
            return datetime.combine(self._day, time()).timestamp()
        return min(self._from_edit.dateTime(), self._to_edit.dateTime()).toSecsSinceEpoch()

    def to_epoch(self) -> float:
        if self.range() == DAY:
            return (datetime.combine(self._day, time()) + timedelta(days=1)).timestamp() - 1
        return max(self._from_edit.dateTime(), self._to_edit.dateTime()).toSecsSinceEpoch()

    def set_custom_range(self, start_epoch: float, end_epoch: float):
        self._from_edit.setDateTime(QDateTime(datetime.fromtimestamp(start_epoch)))
        self._to_edit.setDateTime(QDateTime(datetime.fromtimestamp(end_epoch)))

    def chamber(self) -> str:
        return self._chamber_combo.currentText()

    def _update_state(self, *_):
        fmt = self.format()
        comparison = fmt == "comparison-csv"
        bundle = fmt == "json" or comparison
        for radio in self._range_buttons.values():
            radio.setEnabled(not bundle)
        custom = not bundle and self.range() == CUSTOM
        self._from_edit.setEnabled(custom)
        self._to_edit.setEnabled(custom)
        from_files = not bundle and self.range() in (DAY, CUSTOM)
        self._chamber_combo.setEnabled(from_files)
        self._preview_btn.setVisible(from_files and (is_image(fmt) or is_data(fmt)))
        if comparison:
            note = self.tr(
                "One row per run and channel (min, max, mean, final), aligned on the run start."
            )
        elif bundle:
            note = self.tr("The bundle holds the whole run as saved in the database.")
        elif from_files:
            note = self.tr("Read from the chamber's recorded data on this computer.")
        else:
            note = ""
        self._range_note.setText(note)
        self._range_note.setVisible(bool(note))

    def _card(self):
        card = QFrame()
        card.setObjectName("exportCard")
        return card

    def _caption(self, text):
        label = QLabel(text.upper())
        label.setObjectName("sectionLabel")
        return label


def show_data_preview(parent, run):
    """Shows the samples of a run to export in a table."""
    from app.services.run_export import date_time

    dialog = QDialog(parent)
    dialog.setWindowTitle(dialog.tr("Preview · {0}").format(run.title))
    lay = QVBoxLayout(dialog)
    names = list(run.channels)
    shown = min(len(run.times), PREVIEW_ROWS)
    info = QLabel(
        dialog.tr("{0} samples; the first {1} are shown.").format(len(run.times), shown)
        if len(run.times) > shown
        else dialog.tr("{0} samples.").format(len(run.times))
    )
    info.setObjectName("analysisMeta")
    lay.addWidget(info)
    table = QTableWidget(shown, 2 + len(names))
    table.setHorizontalHeaderLabels(["Time", "Elapsed, s", *(run.label(n) for n in names)])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setAlternatingRowColors(True)
    for r in range(shown):
        t = run.times[r]
        cells = [date_time(run.epoch(t)), f"{t:.1f}"]
        for n in names:
            v = run.channels[n][r]
            cells.append("" if v != v else f"{v:.4g}")
        for c, text in enumerate(cells):
            table.setItem(r, c, QTableWidgetItem(text))
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    lay.addWidget(table, 1)
    dialog.resize(900, 560)
    dialog.open()
    return dialog
