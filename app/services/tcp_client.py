"""TCP client for a live chamber connection.

Inbound wire protocol: newline-delimited JSON. Each line the server sends
is one JSON object of variable readings, the same shape as a
run_samples.csv row (temp, temp_ref, kp, ki, kd, temp_u, temp_u_p,
temp_u_i, temp_u_d, ...).

Outbound wire protocol (send_command(), used by Test Profiles --
views/monitoring.py's step-sequencer): also newline-delimited JSON, one
object per command, e.g. {"cmd": "set_point", "temperature": 60.0,
"pressure": null, "step_index": 0, "step_label": "Ramp to 60C",
"profile_name": "Soak Test A"}.

"""

import json

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket


class ChamberConnection(QObject):
    connected = Signal()
    disconnected = Signal()
    connection_error = Signal(str)
    sample_received = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._socket = QTcpSocket(self)
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.errorOccurred.connect(self._on_error)
        self._buffer = b""

    def connect_to_host(self, host, port):
        if self.is_connected():
            return
        self._buffer = b""
        self._socket.connectToHost(host, int(port))

    def disconnect_from_host(self):
        self._socket.disconnectFromHost()

    def is_connected(self):
        return self._socket.state() == QAbstractSocket.ConnectedState

    def send_command(self, payload):
        """Writes one JSON object + newline to the socket -- see this
        module's docstring for the outbound protocol shape. Raises
        RuntimeError rather than silently dropping the command if there's
        no live connection, so a caller (the test-profile step-sequencer)
        can't mistake a no-op for a sent command."""
        if not self.is_connected():
            raise RuntimeError("Cannot send a command: chamber is not connected.")
        line = json.dumps(payload) + "\n"
        self._socket.write(line.encode("utf-8"))

    def _on_connected(self):
        self.connected.emit()

    def _on_disconnected(self):
        self._buffer = b""
        self.disconnected.emit()

    def _on_error(self, _err):
        self.connection_error.emit(self._socket.errorString())

    def _on_ready_read(self):
        self._buffer += bytes(self._socket.readAll())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(sample, dict):
                self.sample_received.emit(sample)
