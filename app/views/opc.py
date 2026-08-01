"""OpcMixin — builds the OPC UA server setup page.

Backed by a real asyncua OPC UA server (app/services/opc_broadcast_server.py)
that any standard OPC UA client can browse and subscribe to. It can only be
started once at least one chamber connection (Live Monitoring) is
established, since it broadcasts the samples those connections receive --
one server, but every currently-connected chamber gets its own node under
Objects/ChamberVariables/<chamber name>. It keeps running as more chambers
connect or disconnect, and only stops automatically once the last one
disconnects.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_OPC_RATE_MS = {"100 ms": 100, "250 ms": 250, "500 ms": 500, "1 s": 1000}


class OpcMixin:
    def _opc_view(self):
        container = QWidget()
        container.setObjectName("workspaceBody")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(14)

        hdr = QLabel(self.tr("OPC Server"))
        hdr.setObjectName("pageTitle")
        outer.addWidget(hdr)
        sub = QLabel(
            self.tr(
                "Broadcast every connected chamber's live data over a real OPC UA "
                "server. Requires at least one active chamber connection (Live "
                "Monitoring); connect any OPC UA client to browse and subscribe to "
                "the published variables."
            )
        )
        sub.setObjectName("sectionLabel")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        cfg_card = QFrame()
        cfg_card.setObjectName("card")
        cfg_card.setFixedWidth(300)
        cl = QVBoxLayout(cfg_card)
        cl.setContentsMargins(14, 14, 14, 14)
        cl.setSpacing(10)

        lbl = QLabel(self.tr("SERVER CONFIGURATION"))
        lbl.setObjectName("sectionLabel")
        cl.addWidget(lbl)

        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(1, 1)

        self._opc_port = QSpinBox()
        self._opc_port.setRange(1, 65535)
        self._opc_port.setValue(4840)

        self._opc_namespace = QLineEdit("urn:deepvac:opc:server")

        self._opc_security = QComboBox()
        self._opc_security.addItems(["None", "Basic128Rsa15", "Basic256Sha256"])
        self._opc_security.setToolTip(
            self.tr(
                'Only "None" (anonymous, unencrypted) is currently enforced by '
                "the server; the signed/encrypted policies need a certificate "
                "this app doesn't provision yet."
            )
        )

        self._opc_auth = QComboBox()
        self._opc_auth.addItem(self.tr("Anonymous"), "Anonymous")
        self._opc_auth.addItem(self.tr("Username / Password"), "Username / Password")
        self._opc_auth.setToolTip(
            self.tr(
                "Only anonymous access is currently enforced by the server; "
                "username/password auth needs a credential store this app "
                "doesn't have yet."
            )
        )

        self._opc_update_rate = QComboBox()
        self._opc_update_rate.addItems(["100 ms", "250 ms", "500 ms", "1 s"])
        self._opc_update_rate.setCurrentIndex(2)
        self._opc_update_rate.setToolTip(
            self.tr(
                "Caps how often the latest chamber sample is pushed to the OPC UA variable nodes."
            )
        )
        self._opc_update_rate.currentTextChanged.connect(self._on_opc_rate_changed)

        for row_idx, (cap, w) in enumerate(
            [
                (self.tr("Port"), self._opc_port),
                (self.tr("Namespace"), self._opc_namespace),
                (self.tr("Security"), self._opc_security),
                (self.tr("Auth"), self._opc_auth),
                (self.tr("Update rate"), self._opc_update_rate),
            ]
        ):
            lbl = QLabel(cap)
            lbl.setObjectName("sectionLabel")
            grid.addWidget(lbl, row_idx, 0)
            grid.addWidget(w, row_idx, 1)
        cl.addLayout(grid)

        self._opc_start_btn = QPushButton(self.tr("Start Server"))
        self._opc_start_btn.setObjectName("primaryButton")
        self._opc_start_btn.setEnabled(False)
        self._opc_start_btn.setToolTip(self.tr("Connect a chamber in Live Monitoring first"))
        self._opc_start_btn.clicked.connect(self._on_opc_toggle)
        cl.addWidget(self._opc_start_btn)

        status_row = QHBoxLayout()
        self._opc_dot = QLabel("●")
        self._opc_dot.setObjectName("chamberIconOff")
        self._opc_status_lbl = QLabel(self.tr("Server stopped"))
        self._opc_status_lbl.setObjectName("statusText")
        status_row.addWidget(self._opc_dot)
        status_row.addWidget(self._opc_status_lbl, 1)
        cl.addLayout(status_row)
        cl.addStretch(1)
        top_row.addWidget(cfg_card)

        info_card = QFrame()
        info_card.setObjectName("card")
        il = QVBoxLayout(info_card)
        il.setContentsMargins(14, 14, 14, 14)
        il.setSpacing(8)
        info_lbl = QLabel(self.tr("ENDPOINT INFO"))
        info_lbl.setObjectName("sectionLabel")
        il.addWidget(info_lbl)
        self._opc_info_lbl = QLabel()
        self._opc_info_lbl.setAlignment(Qt.AlignCenter)
        self._opc_info_lbl.setObjectName("monitorPlaceholder")
        self._opc_info_lbl.setMinimumHeight(240)
        self._opc_info_lbl.setWordWrap(True)
        self._opc_reset_endpoint_info()
        il.addWidget(self._opc_info_lbl, 1)
        top_row.addWidget(info_card, 1)
        outer.addLayout(top_row)
        outer.addStretch(1)

        self.opc_server.started.connect(self._on_opc_started)
        self.opc_server.stopped.connect(self._on_opc_stopped)
        self.opc_server.client_count_changed.connect(self._on_opc_clients_changed)
        self.opc_server.server_error.connect(self._on_opc_error)

        return container

    # ── Server control ───────────────────────────────────────────────────────

    def _on_opc_toggle(self):
        if self.opc_server.is_running():
            self.opc_server.stop()
        else:
            port = self._opc_port.value()
            namespace = self._opc_namespace.text().strip()
            self.opc_server.set_update_rate(
                _OPC_RATE_MS.get(self._opc_update_rate.currentText(), 500)
            )
            self.opc_server.start(port, namespace=namespace)

    def _on_opc_rate_changed(self, text):
        self.opc_server.set_update_rate(_OPC_RATE_MS.get(text, 500))

    def _opc_set_chamber_connected(self, any_connected):
        """Called from main_window._recompute_chamber_aggregate_status()
        with whether *any* chamber session is currently connected -- the
        server only auto-stops once the last one drops, not on every
        individual chamber's disconnect."""
        if not any_connected and self.opc_server.is_running():
            self.opc_server.stop()
        self._opc_start_btn.setEnabled(any_connected)
        self._opc_start_btn.setToolTip(
            "" if any_connected else self.tr("Connect a chamber in Live Monitoring first")
        )

    def _on_opc_started(self, port):
        self._opc_start_btn.setText(self.tr("Stop Server"))
        self._opc_dot.setObjectName("chamberIconOn")
        self._opc_dot.style().unpolish(self._opc_dot)
        self._opc_dot.style().polish(self._opc_dot)
        self._opc_status_lbl.setText(self.tr("Broadcasting on port {0} — 0 clients").format(port))
        self._opc_update_endpoint_info(port)

    def _on_opc_stopped(self):
        self._opc_start_btn.setText(self.tr("Start Server"))
        self._opc_dot.setObjectName("chamberIconOff")
        self._opc_dot.style().unpolish(self._opc_dot)
        self._opc_dot.style().polish(self._opc_dot)
        self._opc_status_lbl.setText(self.tr("Server stopped"))
        self._opc_reset_endpoint_info()

    def _on_opc_clients_changed(self, count):
        if self.opc_server.is_running():
            from PySide6.QtCore import QCoreApplication

            port = self._opc_port.value()
            # self.tr()'s %n/plural overload doesn't reliably resolve context
            # in this PySide6 version -- call QCoreApplication.translate()
            # directly with the exact context pyside6-lupdate recorded.
            text = QCoreApplication.translate(
                "OpcMixin", "Broadcasting on port {0} — %n client(s)", "", count
            )
            self._opc_status_lbl.setText(text.format(port))

    def _on_opc_error(self, msg):
        self._opc_status_lbl.setText(self.tr("Server error: {0}").format(msg))

    def _opc_update_endpoint_info(self, port):
        import socket

        try:
            host_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            host_ip = "127.0.0.1"
        namespace = self._opc_namespace.text().strip() or self.tr("(none)")
        self._opc_info_lbl.setText(
            self.tr("Broadcasting")
            + "\n\n"
            + self.tr("Endpoint: opc.tcp://{0}:{1}/deepvac/insight/").format(host_ip, port)
            + "\n"
            + self.tr("Namespace: {0}").format(namespace)
            + "\n\n"
            + self.tr(
                "Connect any OPC UA client (e.g. UAExpert) to this endpoint and browse "
                "to Objects → ChamberVariables → <chamber name> for each connected "
                "chamber's live values."
            )
        )

    def _opc_reset_endpoint_info(self):
        self._opc_info_lbl.setText(
            self.tr(
                "Server not running\n\n"
                "Connect a chamber in Live Monitoring, then click\n"
                "Start Server to begin broadcasting its data."
            )
        )
