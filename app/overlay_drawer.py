"""Panel that slides in over its host widget."""

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QScrollArea, QVBoxLayout, QWidget

LEFT, RIGHT = "left", "right"


class OverlayDrawer(QWidget):
    open_changed = Signal(bool)

    def __init__(self, host: QWidget, width: int, side: str = LEFT):
        super().__init__(host)
        self._host = host
        self._width = width
        self._side = side
        self._open = False
        self.setFocusPolicy(Qt.StrongFocus)

        self.panel = QFrame(self)
        self.panel.setObjectName("drawerPanel")
        self.panel.setAttribute(Qt.WA_StyledBackground)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(24)
        shadow.setOffset(4 if side == LEFT else -4, 0)
        shadow.setColor(QColor(15, 23, 42, 90))
        self.panel.setGraphicsEffect(shadow)

        self._slide = QPropertyAnimation(self.panel, b"pos", self)
        self._slide.setDuration(180)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._slide.finished.connect(self._on_slide_finished)

        host.installEventFilter(self)
        self.hide()

    def set_content(self, content: QWidget, scrollable: bool = False):
        if not scrollable:
            self.panel.layout().addWidget(content)
            return
        area = QScrollArea(self.panel)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(content)
        self.panel.layout().addWidget(area)

    def is_open(self) -> bool:
        return self._open

    def open(self):
        if self._open:
            return
        self._open = True
        self.setGeometry(self._host.rect())
        self._place_panel()
        self.panel.move(self._hidden_pos())
        self.show()
        self.raise_()
        self.setFocus()
        self._slide.stop()
        self._slide.setStartValue(self.panel.pos())
        self._slide.setEndValue(self._shown_pos())
        self._slide.start()
        self.open_changed.emit(True)

    def close(self):  # noqa: A003
        if not self._open:
            return
        self._open = False
        self._slide.stop()
        self._slide.setStartValue(self.panel.pos())
        self._slide.setEndValue(self._hidden_pos())
        self._slide.start()
        self.open_changed.emit(False)

    def toggle(self):
        self.close() if self._open else self.open()

    def _on_slide_finished(self):
        if not self._open:
            self.hide()

    def _place_panel(self):
        self.panel.resize(min(self._width, self.width()), self.height())
        if self._open and self._slide.state() != QPropertyAnimation.Running:
            self.panel.move(self._shown_pos())

    def _shown_pos(self):
        return QPoint(0 if self._side == LEFT else self.width() - self.panel.width(), 0)

    def _hidden_pos(self):
        shadow = 30
        if self._side == LEFT:
            return QPoint(-self.panel.width() - shadow, 0)
        return QPoint(self.width() + shadow, 0)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self._host and event.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(self._host.rect())
            self._place_panel()
        return super().eventFilter(watched, event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 23, 42, 50))

    def mousePressEvent(self, event):  # noqa: N802
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
