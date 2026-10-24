"""PLC gate: the chamber PLC's binary TCP protocol (port of control2-client's GateTcp).

Packet:  AA 55 BB 77 | body size (u16 LE) | body | CRC-16 (big endian)
         The CRC (poly 0x1021, init 0xFFFF) covers the size and the body; a
         receiver recomputes it over size + body + CRC and expects 0.
Body:    one code byte, then the payload.

Outgoing (app -> PLC)                    Incoming (PLC -> app)
  0x01 text command (Pascal string)        0x01 state values: u8 count, count x f32 LE
  0x02 settings map: u8 count,             0x02 state names: u8 count, Pascal strings
       count x (Pascal string, f32 LE)     0x03 settings map (same shape as outgoing)
  0x03 job (see encode_job)                0x04 configuration
  0x04 force outputs                       0x05 alarm: u32 LE
  0x05 pair: Pascal string, f32 LE         0x06 job finished
                                           0x07 user message (Pascal string)
                                           0x08 fitted components: u8 count, Pascal strings

Text commands: "ka" (keep-alive, every 5 s), "get_states", "get_settings",
"stop", "next", "confirm_alarm", "confirm_finish", "shutdown". The PLC program
spells the two confirm commands "confrim_..."; send_text_cmd() fixes that.

The protocol layer (functions) is pure; PlcGateClient runs the connection in a
background thread, so it works headless (the agent API) and inside the app.
"""

import contextlib
import logging
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PACKAGE_HEADER = bytes([0xAA, 0x55, 0xBB, 0x77])

# outgoing message codes
OUT_TEXT_CMD = 0x01
OUT_SETTINGS_MAP = 0x02
OUT_JOB = 0x03
OUT_FORCE_OUT = 0x04
OUT_PAIR = 0x05

# incoming message codes
IN_STATE_VALUES = 0x01
IN_STATE_NAMES = 0x02
IN_SETTINGS_MAP = 0x03
IN_CONFIGURATION = 0x04
IN_ALARM = 0x05
IN_JOB_FINISHED = 0x06
IN_USER_MSG = 0x07
IN_COMPONENTS = 0x08

# job
PROG_TYPE_SETPOINT = 1  # manual setpoint
PROG_TYPE_PROG = 2  # program (test profile)
AWAIT_BIT = 8  # interval flag: hold the setpoint for its duration
RAMP_BIT = 9  # interval flag: ramp to the setpoint over the duration
PARAM_TYPE_DURATION = 256  # Qt::UserRole in control_params "type"
PARAM_TYPE_FLAGS = 257  # Qt::UserRole + 1
LONG_TIME_S = 100 * 3600 - 1  # "until stopped" duration of a manual setpoint

STATUS_NAMES = {1: "idle", 2: "work", 3: "finish", 4: "alarm"}  # status_condition values
STATUS_WORK = 2

_PLC_SPELLING = {"confirm_alarm": "confrim_alarm", "confirm_finish": "confrim_finish"}


# ---------------------------------------------------------------- protocol


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF (utils::crc16)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def make_package(body: bytes) -> bytes:
    sized = struct.pack("<H", len(body)) + body
    return PACKAGE_HEADER + sized + struct.pack(">H", crc16(sized))


class PacketReader:
    """Collects received bytes and returns complete, CRC-checked bodies (StreamBuffer)."""

    def __init__(self):
        self._buffer = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        bodies = []
        while True:
            start = self._buffer.find(PACKAGE_HEADER)
            if start < 0:
                del self._buffer[: max(0, len(self._buffer) - 3)]  # keep a possible partial header
                return bodies
            if start > 0:
                del self._buffer[:start]
            if len(self._buffer) <= 8:
                return bodies
            (size,) = struct.unpack_from("<H", self._buffer, 4)
            if len(self._buffer) < 8 + size:
                return bodies
            if crc16(bytes(self._buffer[4 : 8 + size])) == 0:
                bodies.append(bytes(self._buffer[6 : 6 + size]))
                del self._buffer[: 8 + size]
            else:
                self.crc_errors += 1
                del self._buffer[: len(PACKAGE_HEADER)]  # resynchronise on the next header


def pascal_string(text: str) -> bytes:
    data = text.encode("latin-1", errors="replace")[:255]
    return bytes([len(data)]) + data


def read_pascal_string(data: bytes, offset: int) -> tuple[str, int]:
    """(text, next offset); raises ValueError on a truncated string."""
    if offset >= len(data):
        raise ValueError("missing Pascal string")
    length = data[offset]
    end = offset + 1 + length
    if end > len(data):
        raise ValueError("Pascal string longer than the message")
    return data[offset + 1 : end].decode("latin-1"), end


def encode_text_cmd(command: str) -> bytes:
    command = _PLC_SPELLING.get(command.strip(), command)
    return bytes([OUT_TEXT_CMD]) + pascal_string(command)


def encode_pair(name: str, value: float) -> bytes:
    return bytes([OUT_PAIR]) + pascal_string(name) + struct.pack("<f", value)


def encode_settings_map(settings: dict[str, float]) -> bytes:
    if len(settings) > 255:
        raise ValueError("at most 255 settings per message")
    body = bytearray([OUT_SETTINGS_MAP, len(settings)])
    for name in sorted(settings):  # QMap iterates in key order
        body += pascal_string(name) + struct.pack("<f", float(settings[name]))
    return bytes(body)


def encode_job(
    prog_type: int, intervals: list[dict], params: list[dict], cycle: dict | None = None
) -> bytes:
    """JOB message (ProgramModel::makeSendData).

    type u8 | 0 u16 | cycle count u16 | cycle start u16 | cycle end u16 |
    interval count u16 | per interval, per parameter in `params` order:
    duration / flags as u32, anything else as f32 (all little endian).

    params: settings.json "control_params" ([{name, type}, ...]);
    intervals: [{param name: value}]; cycle: {count, start, end}.
    """
    cycle = cycle or {}
    body = bytearray([OUT_JOB])
    body += struct.pack(
        "<BHHHHH",
        prog_type,
        0,
        int(cycle.get("count", 0)),
        int(cycle.get("start", 0)),
        int(cycle.get("end", 0)),
        len(intervals),
    )
    for interval in intervals:
        for param in params:
            value = interval.get(param["name"], 0)
            if int(param.get("type", 0)) in (PARAM_TYPE_DURATION, PARAM_TYPE_FLAGS):
                body += struct.pack("<I", int(value) & 0xFFFFFFFF)
            else:
                body += struct.pack("<f", float(value))
    return bytes(body)


def manual_setpoint_intervals(
    setpoint: float,
    current_temp: float | None,
    enable_flags: int,
    ramp_rate_per_min: float | None = None,
    hold_s: float | None = None,
) -> list[dict]:
    """Intervals of a manual temperature setpoint, as Manual::sendProgram builds them.

    enable_flags: the channel-enable bits of an interval (bits 0-2, from the
    chamber's default program flags). With a ramp rate, a first interval ramps
    from the current temperature; the setpoint is then held for hold_s, or
    until stopped.
    """
    intervals = []
    if ramp_rate_per_min:
        if current_temp is None:
            raise ValueError("a ramp needs the chamber's current temperature")
        duration = abs((current_temp - setpoint) / ramp_rate_per_min * 60.0)
        intervals.append(
            {
                "duration": int(duration),
                "temp": setpoint,
                "flags": (enable_flags & 0x07) | (1 << RAMP_BIT),
            }
        )
    hold_flags = enable_flags & 0x07
    if hold_s:
        hold_flags |= 1 << AWAIT_BIT
    intervals.append(
        {"duration": int(hold_s) if hold_s else LONG_TIME_S, "temp": setpoint, "flags": hold_flags}
    )
    return intervals


def decode_message(body: bytes) -> tuple[int, object]:
    """(code, payload) of an incoming body; raises ValueError when malformed."""
    if not body:
        raise ValueError("empty message")
    code, data = body[0], body[1:]
    if code == IN_STATE_VALUES:
        if not data:
            raise ValueError("state values without a count")
        count = data[0]
        if len(data) < 1 + 4 * count:
            raise ValueError("state values shorter than their count")
        return code, list(struct.unpack_from(f"<{count}f", data, 1))
    if code in (IN_STATE_NAMES, IN_COMPONENTS):
        if not data:
            raise ValueError("name list without a count")
        names, offset = [], 1
        while offset < len(data):
            name, offset = read_pascal_string(data, offset)
            if name:
                names.append(name)
        return code, names
    if code == IN_SETTINGS_MAP:
        if not data:
            raise ValueError("settings map without a count")
        count, offset, settings = data[0], 1, {}
        for _ in range(count):
            name, offset = read_pascal_string(data, offset)
            if offset + 4 > len(data):
                raise ValueError("setting without a value")
            (settings[name],) = struct.unpack_from("<f", data, offset)
            offset += 4
        return code, settings
    if code == IN_ALARM:
        if len(data) != 4:
            raise ValueError("alarm code must be 4 bytes")
        return code, struct.unpack("<I", data)[0]
    if code == IN_USER_MSG:
        return code, read_pascal_string(data, 0)[0] if data else ""
    if code == IN_JOB_FINISHED:
        return code, None
    return code, data


# ---------------------------------------------------------------- client


@dataclass
class PlcSnapshot:
    connected: bool = False
    address: str = ""
    state_names: list[str] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)  # latest value of every state
    values_at: float | None = None  # epoch s of the latest state values
    settings: dict[str, float] = field(default_factory=dict)
    alarm_code: int = 0
    alarm_at: float | None = None
    components: list[str] = field(default_factory=list)
    job_finished_at: float | None = None
    user_messages: list[tuple[float, str]] = field(default_factory=list)
    last_error: str = ""
    job_started_at: float | None = None  # status_condition went to "work" (seen by this connection)
    job_ended_at: float | None = None  # ... and left it again; None while the job runs

    @property
    def status(self) -> str:
        value = self.values.get("status_condition")
        return STATUS_NAMES.get(int(value), "none") if value is not None else "unknown"


class PlcGateClient:
    """One chamber PLC connection, run by a background thread.

    Reconnects on its own, sends the keep-alive, asks for the state names
    and settings until they arrive, and keeps the latest of everything in a
    snapshot. Thread-safe.
    """

    KEEPALIVE_S = 5.0
    CONNECT_TIMEOUT_S = 10.0
    REQUEST_PERIOD_S = 5.0  # "base_params_request_period_sec"
    MAX_MESSAGES = 50
    HISTORY_SAMPLES = 48 * 3600  # two days at one sample per second

    def __init__(self, host: str, port: int, on_sample=None):
        self.host = host
        self.port = int(port)
        self._on_sample = on_sample  # called with {state: value, "timestamp": epoch s}
        self._lock = threading.Lock()
        self._snapshot = PlcSnapshot(address=f"{host}:{port}")
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._history: deque[dict] = deque(maxlen=self.HISTORY_SAMPLES)

    # -- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"plc-gate-{self.host}:{self.port}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock:
            with contextlib.suppress(OSError):
                sock.close()
        if self._thread:
            self._thread.join(timeout=3)

    def wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if (
                    self._snapshot.connected
                    and self._snapshot.state_names
                    and self._snapshot.values
                ):
                    return True
            time.sleep(0.05)
        return False

    # -- state

    def snapshot(self) -> PlcSnapshot:
        with self._lock:
            s = self._snapshot
            return PlcSnapshot(
                s.connected,
                s.address,
                list(s.state_names),
                dict(s.values),
                s.values_at,
                dict(s.settings),
                s.alarm_code,
                s.alarm_at,
                list(s.components),
                s.job_finished_at,
                list(s.user_messages),
                s.last_error,
                s.job_started_at,
                s.job_ended_at,
            )

    def history(self, since: float | None = None) -> list[dict]:
        """Samples received ({"timestamp": epoch s, state: value, ...}), oldest first; only newer than `since`."""
        with self._lock:
            if since is None:
                return list(self._history)
            return [s for s in self._history if s["timestamp"] > since]

    # -- commands

    def send_text_cmd(self, command: str) -> None:
        self._send(encode_text_cmd(command))

    def send_settings_map(self, settings: dict[str, float]) -> None:
        self._send(encode_settings_map(settings))

    def send_job(
        self, prog_type: int, intervals: list[dict], params: list[dict], cycle: dict | None = None
    ) -> None:
        self._send(encode_job(prog_type, intervals, params, cycle))

    def _send(self, body: bytes) -> None:
        sock = self._socket
        if sock is None or not self.snapshot().connected:
            raise RuntimeError(f"The chamber PLC at {self.host}:{self.port} is not connected.")
        with self._send_lock:
            sock.sendall(make_package(body))

    # -- connection thread

    def _set(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._snapshot, key, value)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1.0
            except OSError as exc:
                self._set(connected=False, last_error=str(exc))
                log.info("PLC %s:%s: %s", self.host, self.port, exc)
            self._socket = None
            self._set(connected=False)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 10.0)

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.CONNECT_TIMEOUT_S)
        sock.settimeout(0.5)
        self._socket = sock
        self._set(connected=True, last_error="")
        reader = PacketReader()
        last_keepalive = last_request = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_keepalive >= self.KEEPALIVE_S:
                    self.send_text_cmd("ka")
                    last_keepalive = now
                if now - last_request >= self.REQUEST_PERIOD_S:
                    snap = self.snapshot()
                    if not snap.state_names:
                        self.send_text_cmd("get_states")
                    elif not snap.settings:
                        self.send_text_cmd("get_settings")
                    last_request = now
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    raise ConnectionError("closed by the PLC")
                for body in reader.feed(data):
                    try:
                        self._handle(*decode_message(body))
                    except ValueError as exc:
                        log.warning("PLC %s:%s: bad message: %s", self.host, self.port, exc)
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    def _handle(self, code: int, payload) -> None:
        now = time.time()
        sample = None
        with self._lock:
            s = self._snapshot
            if code == IN_STATE_NAMES:
                s.state_names = payload
            elif code == IN_STATE_VALUES:
                if s.state_names and len(payload) == len(s.state_names):
                    was_working = s.values.get("status_condition") == STATUS_WORK
                    s.values = dict(zip(s.state_names, payload, strict=True))
                    s.values_at = now
                    sample = {"timestamp": now, **s.values}
                    self._history.append(sample)
                    working = s.values.get("status_condition") == STATUS_WORK
                    if working and not was_working:
                        s.job_started_at, s.job_ended_at = now, None
                    elif was_working and not working:
                        s.job_ended_at = now
                elif s.state_names:
                    s.state_names = []  # the PLC changed its state list: ask again
            elif code == IN_SETTINGS_MAP:
                s.settings = payload
            elif code == IN_ALARM:
                s.alarm_code, s.alarm_at = payload, now
            elif code == IN_COMPONENTS:
                s.components = payload
            elif code == IN_JOB_FINISHED:
                s.job_finished_at = now
            elif code == IN_USER_MSG:
                s.user_messages = (s.user_messages + [(now, payload)])[-self.MAX_MESSAGES :]
        if sample and self._on_sample:
            try:
                self._on_sample(sample)
            except Exception:  # a listener must not break the connection
                log.exception("PLC sample listener failed")
