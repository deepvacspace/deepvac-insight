"""Rail of tool buttons beside a chart with a floating panel."""

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.common import _svg_icon
from app.theme_colors import HMI

RAIL_BUTTON = QSize(64, 56)
GAP = 8
PANEL_WIDTH = 320
SHADOW = 12


class _PanelShadow(QWidget):
    """Holds the panel with a margin around it for a soft shadow, a little lower than the panel."""

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        panel = QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW).translated(0, 3)
        for k in range(SHADOW, 0, -1):
            path = QPainterPath()
            path.addRoundedRect(panel.adjusted(-k, -k, k, k), 12 + k, 12 + k)
            painter.fillPath(path, QColor(15, 23, 42, 5))


class ChartSideBar(QFrame):
    opened = Signal(str)
    closed = Signal()

    def __init__(self, page):
        """Chart rail; page is the widget the panel floats over."""
        super().__init__(page)
        self.setObjectName("sideBar")
        self._page = page
        self._pages = {}
        self._current = ""
        self._icons = []
        self._icon_color = HMI["cobalt"]

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._overlay = _PanelShadow(page)
        overlay_lay = QVBoxLayout(self._overlay)
        overlay_lay.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
        self._panel = QFrame(self._overlay)
        self._panel.setObjectName("sidePanel")
        self._panel.setFixedWidth(PANEL_WIDTH)
        panel_lay = QVBoxLayout(self._panel)
        panel_lay.setContentsMargins(16, 12, 10, 12)
        panel_lay.setSpacing(4)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 4, 0)
        self._title = QLabel(self._panel)
        self._title.setObjectName("sidePanelTitle")
        self._close_btn = QToolButton(self._panel)
        self._close_btn.setObjectName("panelClose")
        self._close_btn.setToolTip(self.tr("Close"))
        self._close_btn.setFocusPolicy(Qt.NoFocus)
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.setIconSize(QSize(16, 16))
        self._close_btn.clicked.connect(self.close_panel)
        self._icons.append((self._close_btn, "rail-close"))
        header.addWidget(self._title, 1)
        header.addWidget(self._close_btn)
        panel_lay.addLayout(header)
        self._subtitle = QLabel(self._panel)
        self._subtitle.setProperty("muted", True)
        self._subtitle.hide()
        panel_lay.addWidget(self._subtitle)
        panel_lay.addSpacing(6)
        self._stack = QStackedWidget(self._panel)
        panel_lay.addWidget(self._stack, 1)
        overlay_lay.addWidget(self._panel)
        self._overlay.hide()

        rail = QFrame(self)
        rail.setObjectName("sideRail")
        rail.setFixedWidth(RAIL_BUTTON.width() + 2 * GAP + 2)
        rail_outer = QVBoxLayout(rail)
        rail_outer.setContentsMargins(0, 0, 0, 0)
        self._rail_stack = QStackedWidget(rail)
        rail_outer.addWidget(self._rail_stack)
        buttons = QWidget(self._rail_stack)
        self._rail_stack.addWidget(buttons)
        rail_lay = QVBoxLayout(buttons)
        rail_lay.setContentsMargins(GAP, GAP, GAP, GAP)
        rail_lay.setSpacing(GAP // 2)
        self._top = QVBoxLayout()
        self._bottom = QVBoxLayout()
        self._top.setSpacing(GAP // 2)
        self._bottom.setSpacing(GAP // 2)
        rail_lay.addLayout(self._top)
        rail_lay.addStretch(1)
        rail_lay.addLayout(self._bottom)
        lay.addWidget(rail)

    def rail_button(self, title, icon, label="", checkable=False):
        """Builds a rail button."""
        button = QToolButton()
        button.setObjectName("railButton")
        button.setToolTip(title)
        button.setText(label or title)
        button.setFocusPolicy(Qt.NoFocus)
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setFixedSize(RAIL_BUTTON)
        button.setIconSize(QSize(22, 22))
        button.setCheckable(checkable)
        self._icons.append((button, icon))
        button.setIcon(_svg_icon(icon, self._icon_color, 22))
        return button

    def _placed(self, button, bottom):
        (self._bottom if bottom else self._top).addWidget(button, 0, Qt.AlignHCenter)
        return button

    def add_page(self, key, title, icon, page, bottom=False, scroll=True):
        button = self._placed(self.rail_button(title, icon, checkable=True), bottom)
        holder = page
        if scroll:
            area = QScrollArea(self._stack)
            area.setObjectName("sidePanelScroll")
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setWidget(page)
            holder = area
        page.setObjectName(page.objectName() or "sidePage")
        self._stack.addWidget(holder)
        self._pages[key] = {"button": button, "holder": holder, "title": title, "subtitle": ""}
        button.clicked.connect(lambda *_: self.toggle(key))
        return button

    def add_action(self, title, icon, bottom=False):
        """Adds a rail button that runs an action."""
        return self._placed(self.rail_button(title, icon), bottom)

    def set_tools(self, title, label, icon, tools):
        """Sets the tools shown from the rail's first button."""
        open_btn = self.rail_button(title, icon, label=label)
        self._top.insertWidget(0, open_btn, 0, Qt.AlignHCenter)
        page = QWidget(self._rail_stack)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(GAP, GAP, GAP, GAP)
        lay.setSpacing(GAP // 2)
        back = self.rail_button(self.tr("Back to the options"), "rail-left", label=self.tr("Back"))
        lay.addWidget(back, 0, Qt.AlignHCenter)
        line = QFrame(page)
        line.setObjectName("cardDivider")
        line.setFixedHeight(1)
        lay.addWidget(line)
        tools.setParent(page)
        lay.addWidget(tools, 0, Qt.AlignHCenter)
        lay.addStretch(1)
        self._rail_stack.addWidget(page)
        open_btn.clicked.connect(lambda *_: self.show_tools(True))
        back.clicked.connect(lambda *_: self.show_tools(False))

    def show_tools(self, shown):
        if shown and self._rail_stack.count() > 1:
            self.close_panel()
            self._rail_stack.setCurrentIndex(1)
        else:
            self._rail_stack.setCurrentIndex(0)

    def set_subtitle(self, key, text):
        if key in self._pages:
            self._pages[key]["subtitle"] = text
            if self._current == key:
                self._show_subtitle()

    def _show_subtitle(self):
        text = self._pages[self._current]["subtitle"] if self._current else ""
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))

    def set_icon_color(self, color):
        self._icon_color = color
        for button, icon in self._icons:
            button.setIcon(_svg_icon(icon, color, button.iconSize().width()))

    def current(self):
        return self._current

    def open_panel(self, key):
        page = self._pages.get(key)
        if page is None or not page["button"].isEnabled():
            return
        for k, p in self._pages.items():
            p["button"].setChecked(k == key)
        self._current = key
        self._title.setText(page["title"])
        self._show_subtitle()
        self._stack.setCurrentWidget(page["holder"])
        self._place_panel()
        self._overlay.raise_()
        self._overlay.show()
        QApplication.instance().installEventFilter(self)
        self.opened.emit(key)

    def close_panel(self):
        if not self._current:
            return
        for p in self._pages.values():
            p["button"].setChecked(False)
        self._current = ""
        self._overlay.hide()
        QApplication.instance().removeEventFilter(self)
        self.closed.emit()

    def toggle(self, key):
        if self._current == key:
            self.close_panel()
        else:
            self.open_panel(key)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._place_panel()

    def moveEvent(self, event):  # noqa: N802
        super().moveEvent(event)
        self._place_panel()

    def _place_panel(self):
        """Right of the rail, a gap apart, as tall as the rail."""
        rail = QRect(self.mapTo(self._page, QPoint(0, 0)), self.size())
        panel = QRect(rail.right() + 1 + GAP, rail.top(), PANEL_WIDTH, rail.height())
        self._overlay.setGeometry(panel.adjusted(-SHADOW, -SHADOW, SHADOW, SHADOW))

    def eventFilter(self, watched, event):  # noqa: N802
        """Closes the panel on a click outside it."""
        if event.type() != QEvent.MouseButtonPress or not isinstance(watched, QWidget):
            return False
        page = self._page
        if not (watched is page or page.isAncestorOf(watched)):
            return False
        at = page.mapFromGlobal(event.globalPosition().toPoint())
        panel = QRect(self._panel.mapTo(page, QPoint(0, 0)), self._panel.size())
        rail = QRect(self.mapTo(page, QPoint(0, 0)), self.size())
        if not panel.contains(at) and not rail.contains(at):
            self.close_panel()
        return False
