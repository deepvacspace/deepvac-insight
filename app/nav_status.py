"""Chamber status box of the navigation bar."""

import math
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from app.theme_colors import HMI

STATES = {
    "work": ("Running", HMI["live"]),
    "idle": ("Idle", HMI["idle"]),
    "online": ("Idle", HMI["idle"]),
    "alarm": ("Alarm", HMI["alarm"]),
}


def _pressure(value):
    """Formats a pressure."""
    if value is None or not math.isfinite(value):
        return "—"
    if value and abs(value) < 0.01:
        mantissa, exponent = f"{value:.1E}".split("E")
        return f"{mantissa}·E{exponent}"
    return f"{value:.3g}"


def _temperature(value):
    return "—" if value is None or not math.isfinite(value) else f"{value:.1f}"


class NavStatus(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navStatus")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 7, 8, 8)
        lay.setSpacing(2)
        self.state = QLabel()
        self.state.setObjectName("navStatusState")
        self.state.setTextFormat(Qt.RichText)
        self.state.setWordWrap(True)
        self.job = QLabel()
        self.job.setObjectName("navStatusJob")
        lay.addWidget(self.state)
        lay.addWidget(self.job)
        self.temp_head, self.temp_value = self._reading(lay)
        self.pres_head, self.pres_value = self._reading(lay)
        self.chamber = None

    def _reading(self, lay):
        line = QFrame()
        line.setObjectName("navStatusLine")
        line.setFixedHeight(1)
        lay.addSpacing(3)
        lay.addWidget(line)
        lay.addSpacing(3)
        head = QLabel()
        head.setObjectName("navStatusUnit")
        value = QLabel("—")
        value.setObjectName("navStatusValue")
        lay.addWidget(head)
        lay.addWidget(value)
        return head, value

    def show_chamber(self, name, snapshot, pres_unit="Torr"):
        """Shows a chamber's status."""
        self.chamber = name
        if snapshot is None:
            state, color, job = "No chamber", HMI["no_connection"], ""
            values = {}
        else:
            values = snapshot.values or {}
            fresh = snapshot.connected and values and (time.time() - (snapshot.values_at or 0) < 15)
            if not fresh:
                state, color = "Connecting", HMI["connecting"]
            else:
                state, color = STATES.get(
                    snapshot.status, (str(snapshot.status).capitalize(), HMI["idle"])
                )
                if snapshot.alarm_code:
                    state, color = "Alarm", HMI["alarm"]
            job = ""
            if fresh and snapshot.status == "work":
                job = "(program)" if values.get("interval") else "(setpoint)"
        self.state.setText(f"<span style='color:{color}'>●</span>&nbsp;<b>{self.tr(state)}</b>")
        self.job.setText(self.tr(job) if job else (name or ""))
        self.setToolTip(name or "")
        num = lambda key: values.get(key) if isinstance(values.get(key), (int, float)) else None  # noqa: E731
        self.temp_head.setText(f"°C → {_temperature(num('temp_ref'))}")
        self.temp_value.setText(_temperature(num("temp")))
        from app.services.pressure import to_torr

        self.pres_head.setText(f"Torr → {_pressure(to_torr(num('pres_ref'), pres_unit))}")
        self.pres_value.setText(_pressure(to_torr(num("pres"), pres_unit)))
