"""Panels of the Analysis rail, shared by the Live tab and the run tabs."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.common import _svg_icon
from app.theme_colors import HMI


def add_section(lay, title, open_=True):
    """Adds a foldable section and returns the layout of its body."""
    page = lay.parentWidget()
    if lay.count():
        lay.addSpacing(8)
        line = QFrame(page)
        line.setObjectName("cardDivider")
        line.setFixedHeight(1)
        lay.addWidget(line)
    header = QPushButton(page)
    header.setObjectName("drawerSection")
    header.setCheckable(True)
    header.setChecked(open_)
    header.setFocusPolicy(Qt.NoFocus)
    head = QHBoxLayout(header)
    head.setContentsMargins(0, 0, 0, 0)
    name = QLabel(title, header)
    name.setObjectName("drawerSectionTitle")
    arrow = QLabel(header)
    for label in (name, arrow):
        label.setAttribute(Qt.WA_TransparentForMouseEvents)
    head.addWidget(name)
    head.addStretch(1)
    head.addWidget(arrow)
    body = QWidget(page)
    body.setObjectName("sidePage")
    body_lay = QVBoxLayout(body)
    body_lay.setContentsMargins(0, 0, 0, 0)
    body_lay.setSpacing(6)

    def fold(on):
        icon = _svg_icon("rail-down" if on else "rail-right", HMI["steel"], 14)
        arrow.setPixmap(icon.pixmap(14, 14))
        body.setVisible(on)

    fold(open_)
    header.toggled.connect(fold)
    lay.addWidget(header)
    lay.addWidget(body)
    return body_lay


def caption(text):
    """A small grey caption over a field."""
    label = QLabel(text)
    label.setProperty("fieldLabel", True)
    return label


def segments(labels, exclusive=True):
    """Builds a row of joined toggle buttons."""
    row = QHBoxLayout()
    row.setSpacing(0)
    group = QButtonGroup(row) if exclusive else None
    buttons = []
    for i, text in enumerate(labels):
        button = QPushButton(text)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setProperty("softSegment", True)
        button.setProperty("segmentFirst", i == 0)
        button.setProperty("segmentLast", i == len(labels) - 1)
        if group is not None:
            group.addButton(button)
        row.addWidget(button, 1)
        buttons.append(button)
    if group is not None:
        row._group = group
    return row, buttons


def link_button(text):
    button = QPushButton(text)
    button.setProperty("link", True)
    button.setFocusPolicy(Qt.NoFocus)
    button.setCursor(Qt.PointingHandCursor)
    return button


def panel_page():
    """Builds a panel page and returns it with its layout."""
    page = QWidget()
    page.setObjectName("sidePage")
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 8, 8)
    lay.setSpacing(6)
    return page, lay


INSPECT, BOX_ZOOM, ANNOTATE = "inspect", "box", "annotate"


class ChartTools(QWidget):
    """The chart's tools shown in the rail."""

    annotate_toggled = Signal(bool)

    def __init__(self, side_bar, chart, annotate=True):
        super().__init__()
        self.chart = chart
        self.tool = INSPECT
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        bar = side_bar
        self.buttons = {
            INSPECT: bar.rail_button(
                self.tr("Inspect: drag to move the view; the tooltip follows the pointer"),
                "rail-pointer",
                self.tr("Inspect"),
                True,
            ),
            BOX_ZOOM: bar.rail_button(
                self.tr("Box zoom: drag a box over the chart to zoom into it"),
                "rail-boxzoom",
                self.tr("Box zoom"),
                True,
            ),
        }
        self.fit_btn = bar.rail_button(
            self.tr("Fit: the whole data, following new values"), "rail-fit", self.tr("Fit")
        )
        if annotate:
            self.buttons[ANNOTATE] = bar.rail_button(
                self.tr("Annotate: drag across the chart to mark and label a time range"),
                "rail-annotate",
                self.tr("Annotate"),
                True,
            )
        self.labels_btn = bar.rail_button(
            self.tr("Labels: the box with the line names and colours"),
            "rail-labels",
            self.tr("Labels"),
            True,
        )
        self.tooltip_btn = bar.rail_button(
            self.tr("Tooltip: the values where the pointer is"),
            "rail-tooltip",
            self.tr("Tooltip"),
            True,
        )
        for name, button in self.buttons.items():
            button.clicked.connect(lambda _=False, n=name: self.set_tool(n))
        self.fit_btn.clicked.connect(chart.reset_view)
        self.labels_btn.setChecked(True)
        self.labels_btn.toggled.connect(self._set_labels)
        self.tooltip_btn.setChecked(True)
        self.tooltip_btn.toggled.connect(chart.set_tooltip_enabled)
        for name in (INSPECT, BOX_ZOOM):
            lay.addWidget(self.buttons[name])
        lay.addWidget(self.fit_btn)
        if ANNOTATE in self.buttons:
            lay.addWidget(self.buttons[ANNOTATE])
        line = QFrame()
        line.setObjectName("cardDivider")
        line.setFixedHeight(1)
        lay.addWidget(line)
        lay.addWidget(self.labels_btn)
        lay.addWidget(self.tooltip_btn)
        self.set_tool(INSPECT)

    def set_tool(self, name):
        import pyqtgraph as pg

        was_annotating = self.tool == ANNOTATE
        self.tool = name
        for n, button in self.buttons.items():
            button.setChecked(n == name)
        mode = pg.ViewBox.RectMode if name == BOX_ZOOM else pg.ViewBox.PanMode
        self.chart.plot.getViewBox().setMouseMode(mode)
        if (name == ANNOTATE) != was_annotating:
            self.chart.set_annotate_mode(name == ANNOTATE)
            self.annotate_toggled.emit(name == ANNOTATE)

    def _set_labels(self, visible):
        if self.chart.legend is not None:
            self.chart.legend.setVisible(visible)


class ChannelList(QWidget):
    """Channel list with visibility, name, unit and colour."""

    toggled = Signal(str, bool)
    color_changed = Signal(str, str)
    manage_requested = Signal()

    def __init__(self, manage=True, parent=None):
        super().__init__(parent)
        self.setObjectName("sidePage")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 8, 8)
        self._lay.setSpacing(2)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(2)
        self._lay.addLayout(self._rows)
        self._manage = None
        if manage:
            self._manage = link_button(self.tr("+ New calculated channel"))
            self._manage.setToolTip(self.tr("Define, change or remove calculated channels"))
            self._manage.clicked.connect(self.manage_requested)
            self._lay.addSpacing(6)
            self._lay.addWidget(self._manage, 0, Qt.AlignLeft)
        self._lay.addStretch(1)
        self.checks = {}

    def set_channels(self, channels, calculated=()):
        """Sets the channels and calculated channels."""
        while self._rows.count():
            item = self._rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.checks = {}
        for row in channels:
            self._rows.addWidget(self._row(row))
        if calculated:
            heading = QLabel(self.tr("CALCULATED CHANNELS"))
            heading.setObjectName("sectionLabel")
            heading.setContentsMargins(0, 10, 0, 2)
            self._rows.addWidget(heading)
            for row in calculated:
                self._rows.addWidget(self._row(row))

    def _row(self, row):
        box = QWidget()
        box.setObjectName("channelRow")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)
        check = QCheckBox(row.get("label") or row["key"])
        check.setChecked(bool(row.get("checked")))
        check.setToolTip(row.get("tooltip") or row["key"])
        check.toggled.connect(lambda on, k=row["key"]: self.toggled.emit(k, on))
        self.checks[row["key"]] = check
        lay.addWidget(check, 1)
        if row.get("unit"):
            unit = QLabel(row["unit"])
            unit.setProperty("muted", True)
            lay.addWidget(unit)
        swatch = QPushButton()
        swatch.setObjectName("colorSwatch")
        swatch.setFixedSize(QSize(18, 18))
        swatch.setToolTip(self.tr("Colour of {0}").format(row.get("label") or row["key"]))
        swatch.setStyleSheet(
            f"QPushButton#colorSwatch{{background:{row['color']};border:1px solid rgba(0,0,0,0.18);"
            "border-radius:4px;padding:0;}"
        )
        swatch.clicked.connect(lambda _=False, r=row: self._pick(r))
        lay.addWidget(swatch)
        return box

    def _pick(self, row):
        name = row.get("label") or row["key"]
        picked = QColorDialog.getColor(
            QColor(row["color"]), self, self.tr("Colour of {0}").format(name)
        )
        if picked.isValid():
            self.color_changed.emit(row["key"], picked.name())

    def shown_text(self):
        shown = sum(c.isChecked() for c in self.checks.values())
        return self.tr("{0} of {1} shown").format(shown, len(self.checks))


class RunDetailsForm(QWidget):
    """Form for the experiment's details."""

    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidePage")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        def line(placeholder=""):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.textEdited.connect(self.edited)
            return edit

        self.dut = line(self.tr("Device under test, e.g. serial number"))
        self.mass = line(self.tr("e.g. 12.5"))
        self.density = line(self.tr("e.g. 2700"))
        self.operator = line(self.tr("Logged-in user"))
        self.purpose = line(self.tr("e.g. thermal cycling qualification"))
        self.tags = line(self.tr("comma separated, e.g. qualification, batch-7"))
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(84)
        self.notes.setPlaceholderText(self.tr("Anything the report should say"))
        self.notes.textChanged.connect(self.edited)
        for title, edit in (
            (self.tr("DUT"), self.dut),
            (self.tr("DUT mass, kg"), self.mass),
            (self.tr("DUT density, kg/m³"), self.density),
            (self.tr("Operator"), self.operator),
            (self.tr("Purpose"), self.purpose),
            (self.tr("Tags"), self.tags),
            (self.tr("Notes"), self.notes),
        ):
            lay.addWidget(caption(title))
            lay.addWidget(edit)

    def set_metadata(self, metadata):
        m = metadata or {}
        widgets = (self.dut, self.mass, self.density, self.operator, self.purpose, self.tags)
        for w in (*widgets, self.notes):
            w.blockSignals(True)
        self.dut.setText(m.get("dut") or "")
        self.mass.setText("" if m.get("dut_mass") is None else f"{m['dut_mass']:g}")
        self.density.setText("" if m.get("dut_density") is None else f"{m['dut_density']:g}")
        self.operator.setText(m.get("operator") or "")
        self.purpose.setText(m.get("purpose") or "")
        self.tags.setText(", ".join(m.get("tags") or []))
        self.notes.setPlainText(m.get("notes") or "")
        for w in (*widgets, self.notes):
            w.blockSignals(False)

    def metadata(self):
        from app.services.data_service import clean_metadata

        return clean_metadata(
            {
                "dut": self.dut.text(),
                "dut_mass": self.mass.text().replace(",", "."),
                "dut_density": self.density.text().replace(",", "."),
                "operator": self.operator.text(),
                "purpose": self.purpose.text(),
                "tags": self.tags.text(),
                "notes": self.notes.toPlainText(),
            }
        )


def info_rows(rows):
    """Builds the Info panel's name and value rows."""
    from html import escape

    cells = "".join(
        f"<tr><td style='color:{HMI['steel']};padding-right:12px'>{escape(str(n))}</td>"
        f"<td>{escape(str(v))}</td></tr>"
        for n, v in rows
        if v not in (None, "", "—")
    )
    return f"<table cellpadding='3'>{cells}</table>"
