"""A real OPC UA server broadcasting every currently-connected chamber's
live variables.

Backed by asyncua: a spec-compliant OPC UA server that any standard OPC UA
client (UAExpert, an asyncua/python-opcua client, etc.) can browse and
subscribe to -- not just an app-specific wire format.

asyncua is asyncio-native and this app runs on Qt's event loop, so the
server runs on its own thread with its own asyncio event loop. Samples
arrive on the Qt thread (one ChamberSession.sample_received per connected
chamber, see services/chamber_session.py -- wired to broadcast() in
main_window.py) and are handed to the asyncio thread with
asyncio.run_coroutine_threadsafe(); all node access happens on the server
thread, never on the Qt thread. Each connected chamber gets its own
sub-object node under <namespace>/Objects/ChamberVariables/<chamber name>,
created the first time a sample from that chamber is seen; each distinct
sample key becomes a variable node under that chamber's object, updated in
place after that. One server instance broadcasts every chamber currently
connected -- there is no per-chamber server.

Security: only SecurityPolicyType.NoSecurity / anonymous auth is offered.
Basic128Rsa15/Basic256Sha256 and username+password auth need a certificate
and a credential store this app doesn't have yet -- the OPC page's
Security/Auth fields (app/views/opc.py) are kept for a future
implementation and are not enforced here.
"""

import asyncio
import contextlib
import threading

from asyncua import Server, ua
from PySide6.QtCore import QObject, QTimer, Signal

_POLL_INTERVAL_S = 0.5  # how often the server thread checks for stop + client count


class OpcBroadcastServer(QObject):
    started = Signal(int)  # port
    stopped = Signal()
    client_count_changed = Signal(int)
    server_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.namespace = ""
        self._port = None
        self._loop = None
        self._thread = None
        self._stop_event = None
        self._start_error = None
        self._idx = None
        self._obj_node = None
        self._chamber_nodes = {}  # chamber_label -> object node under ChamberVariables
        self._var_nodes = {}  # (chamber_label, key) -> variable node
        self._running = threading.Event()
        self._last_client_count = 0

        self._pending_samples = {}  # chamber_label -> latest sample, flushed on the timer below
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(500)
        self._update_timer.timeout.connect(self._flush_pending_samples)

    def is_running(self):
        return self._running.is_set()

    def set_update_rate(self, interval_ms):
        """Caps how often a cached sample is pushed to the OPC UA nodes --
        safe to call while stopped (takes effect on the next start()) or
        while running (takes effect on the timer's next tick)."""
        self._update_timer.setInterval(max(1, int(interval_ms)))

    def start(self, port, namespace=""):
        if self.is_running():
            return True
        self.namespace = namespace or "urn:deepvac:opc:server"
        self._port = int(port)
        self._stop_event = threading.Event()
        self._chamber_nodes = {}
        self._var_nodes = {}
        self._pending_samples = {}
        self._last_client_count = 0

        ready = threading.Event()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._thread_main, args=(ready,), daemon=True, name="opc-ua-server"
        )
        self._thread.start()
        ready.wait(timeout=10)

        if self._start_error is not None:
            err, self._start_error = self._start_error, None
            self._thread = None
            self.server_error.emit(err)
            return False
        if not self.is_running():
            self._thread = None
            self.server_error.emit("Timed out starting the OPC UA server.")
            return False

        self._update_timer.start()
        self.started.emit(self._port)
        return True

    def stop(self):
        if not self.is_running():
            return
        self._update_timer.stop()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._chamber_nodes = {}
        self._var_nodes = {}
        self._pending_samples = {}
        self._last_client_count = 0
        self.stopped.emit()
        self.client_count_changed.emit(0)

    def broadcast(self, chamber_label: str, sample: dict):
        # Called on the Qt thread every time a sample arrives for a given
        # chamber; just cache it. _flush_pending_samples(), driven by the
        # QTimer above (i.e. the configured update rate), is what actually
        # pushes it to that chamber's nodes.
        self._pending_samples[chamber_label] = sample

    def _flush_pending_samples(self):
        pending, self._pending_samples = self._pending_samples, {}
        if not pending or not self.is_running() or self._loop is None:
            return
        for chamber_label, sample in pending.items():
            asyncio.run_coroutine_threadsafe(self._write_sample(chamber_label, sample), self._loop)

    # ── Server thread (asyncio) ─────────────────────────────────────────────

    def _thread_main(self, ready: threading.Event):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_main(ready))
        except Exception as exc:  # e.g. the port is already in use
            self._start_error = str(exc)
            ready.set()
        finally:
            self._running.clear()
            loop.close()

    async def _async_main(self, ready: threading.Event):
        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://0.0.0.0:{self._port}/deepvac/insight/")
        server.set_server_name("DeepVac Insight OPC UA Server")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self._idx = await server.register_namespace(self.namespace)
        self._obj_node = await server.nodes.objects.add_object(self._idx, "ChamberVariables")

        async with server:
            self._running.set()
            ready.set()
            while not self._stop_event.is_set():
                self._poll_client_count(server)
                await asyncio.sleep(_POLL_INTERVAL_S)

    def _poll_client_count(self, server):
        count = len(server.bserver.clients)
        if count != self._last_client_count:
            self._last_client_count = count
            self.client_count_changed.emit(count)

    async def _write_sample(self, chamber_label: str, sample: dict):
        chamber_node = self._chamber_nodes.get(chamber_label)
        if chamber_node is None:
            chamber_node = await self._obj_node.add_object(self._idx, chamber_label)
            self._chamber_nodes[chamber_label] = chamber_node

        for key, raw_value in sample.items():
            value = self._coerce(raw_value)
            if value is None:
                continue
            node_key = (chamber_label, key)
            node = self._var_nodes.get(node_key)
            if node is None:
                node = await chamber_node.add_variable(self._idx, key, value)
                await node.set_writable(False)
                self._var_nodes[node_key] = node
            else:
                with contextlib.suppress(ua.UaError):
                    await node.write_value(value)

    @staticmethod
    def _coerce(value):
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value
