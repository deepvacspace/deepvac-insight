"""PLC gate: the chamber PLC's binary TCP protocol."""

import bisect
import contextlib
import dataclasses
import itertools
import logging
import math
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PACKAGE_HEADER = bytes([0xAA, 0x55, 0xBB, 0x77])

OUT_TEXT_CMD = 0x01
OUT_SETTINGS_MAP = 0x02
OUT_JOB = 0x03
OUT_FORCE_OUT = 0x04
OUT_PAIR = 0x05

IN_STATE_VALUES = 0x01
IN_STATE_NAMES = 0x02
IN_SETTINGS_MAP = 0x03
IN_CONFIGURATION = 0x04
IN_ALARM = 0x05
IN_JOB_FINISHED = 0x06
IN_USER_MSG = 0x07
IN_COMPONENTS = 0x08

PROG_TYPE_SETPOINT = 1
PROG_TYPE_PROG = 2
AWAIT_BIT = 8
RAMP_BIT = 9
PARAM_TYPE_DURATION = 256
PARAM_TYPE_FLAGS = 257
LONG_TIME_S = 100 * 3600 - 1

STATUS_NAMES = {1: "idle", 2: "work", 3: "finish", 4: "alarm"}
STATUS_WORK = 2

_PLC_SPELLING = {"confirm_alarm": "confrim_alarm", "confirm_finish": "confrim_finish"}


def crc16(data: bytes) -> int:
    """Computes CRC-16/CCITT-FALSE."""
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
    """Splits received bytes into CRC-checked packets."""

    def __init__(self):
        self._buffer = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        bodies = []
        while True:
            start = self._buffer.find(PACKAGE_HEADER)
            if start < 0:
                del self._buffer[: max(0, len(self._buffer) - 3)]
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
                del self._buffer[: len(PACKAGE_HEADER)]


def pascal_string(text: str) -> bytes:
    data = text.encode("latin-1", errors="replace")[:255]
    return bytes([len(data)]) + data


def read_pascal_string(data: bytes, offset: int) -> tuple[str, int]:
    """Reads a Pascal string and returns it with the next offset."""
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
    for name in sorted(settings):
        body += pascal_string(name) + struct.pack("<f", float(settings[name]))
    return bytes(body)


def encode_job(
    prog_type: int, intervals: list[dict], params: list[dict], cycle: dict | None = None
) -> bytes:
    """Encodes a JOB message."""
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
    """Intervals of a manual temperature setpoint, as Manual::sendProgram builds them."""
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
    """Decodes an incoming message."""
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


@dataclass
class PlcSnapshot:
    connected: bool = False
    address: str = ""
    state_names: list[str] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)
    values_at: float | None = None
    settings: dict[str, float] = field(default_factory=dict)
    alarm_code: int = 0
    alarm_at: float | None = None
    components: list[str] = field(default_factory=list)
    job_finished_at: float | None = None
    user_messages: list[tuple[float, str]] = field(default_factory=list)
    last_error: str = ""
    job_started_at: float | None = None
    job_ended_at: float | None = None
    settings_at: float | None = None
    plc_time: float | None = None
    plc_rate: float = 1.0

    @property
    def status(self) -> str:
        value = self.values.get("status_condition")
        return STATUS_NAMES.get(int(value), "none") if value is not None else "unknown"


def plc_clock_now(plc_time, at, rate, wall, max_gap_s=0.25):
    """Extrapolates the PLC clock to a wall-clock time."""
    gap = max(0.0, wall - at)
    if rate > 1.5:
        gap = min(gap, max_gap_s)
    return plc_time + gap * rate


class PlcClock:
    """The PLC's clock, from its job runtime."""

    SEGMENT_MIN_S = 5.0
    MIN_RATE, MAX_RATE = 0.1, 10000.0

    def __init__(self):
        self.time: float | None = None
        self.rate = 1.0
        self._wall: float | None = None
        self._t_total: float | None = None
        self._segment: tuple[float, float] | None = None

    def advance(self, wall: float, values: dict) -> float:
        t_total = values.get("t_total")
        working = values.get("status_condition") == STATUS_WORK
        if not (isinstance(t_total, (int, float)) and math.isfinite(t_total)) or not working:
            t_total = None
        if self.time is None:
            self.time = wall
        else:
            elapsed = max(0.0, wall - self._wall)
            if t_total is not None and self._t_total is not None and t_total >= self._t_total:
                self.time += t_total - self._t_total
                if self._segment is None:
                    self._segment = (self._wall, self._t_total)
                span = wall - self._segment[0]
                if span >= self.SEGMENT_MIN_S and t_total > self._segment[1]:
                    rate = (t_total - self._segment[1]) / span
                    self.rate = min(max(rate, self.MIN_RATE), self.MAX_RATE)
            else:
                self._segment = None
                self.time += elapsed * self.rate
        self._wall, self._t_total = wall, t_total
        return self.time


class PlcGateClient:
    """One chamber PLC connection, run by a background thread."""

    KEEPALIVE_S = 5.0
    CONNECT_TIMEOUT_S = 10.0
    REQUEST_PERIOD_S = 5.0
    MAX_MESSAGES = 50
    HISTORY_SAMPLES = 48 * 3600

    def __init__(self, host: str, port: int, on_sample=None):
        self.host = host
        self.port = int(port)
        self._on_sample = on_sample
        self._lock = threading.Lock()
        self._snapshot = PlcSnapshot(address=f"{host}:{port}")
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._history: deque[dict] = deque(maxlen=self.HISTORY_SAMPLES)
        self._plc_times: deque[float] = deque(maxlen=self.HISTORY_SAMPLES)
        self._clock = PlcClock()

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

    def snapshot(self) -> PlcSnapshot:
        with self._lock:
            s = self._snapshot
            return dataclasses.replace(
                s,
                state_names=list(s.state_names),
                values=dict(s.values),
                settings=dict(s.settings),
                components=list(s.components),
                user_messages=list(s.user_messages),
            )

    def history(self, since: float | None = None) -> list[dict]:
        """Returns the received samples newer than a time."""
        with self._lock:
            if since is None:
                return list(self._history)
            return [s for s in self._history if s["timestamp"] > since]

    def plc_samples(self, since: float, max_points: int = 500, keys=None) -> list[dict]:
        """Returns evenly spread samples newer than a PLC time."""
        with self._lock:
            times = list(self._plc_times)
            start = bisect.bisect_right(times, since)
            samples = list(itertools.islice(self._history, start, None))
        times = times[start:]
        step = max(1, len(samples) // max(1, max_points))
        out = []
        for t, sample in zip(times[::step], samples[::step], strict=True):
            row = {"plc_time": t}
            for key in keys or sample.keys():
                if key in sample:
                    row[key] = sample[key]
            out.append(row)
        return out

    def plc_history(self, since: float | None = None) -> list[dict]:
        """Returns the samples newer than a PLC time."""
        with self._lock:
            return [
                {**s, "plc_time": t}
                for s, t in zip(self._history, self._plc_times, strict=True)
                if since is None or t > since
            ]

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
                    s.plc_time = self._clock.advance(now, s.values)
                    s.plc_rate = self._clock.rate
                    sample = {"timestamp": now, **s.values}
                    self._history.append(sample)
                    self._plc_times.append(s.plc_time)
                    working = s.values.get("status_condition") == STATUS_WORK
                    if working and not was_working:
                        s.job_started_at, s.job_ended_at = now, None
                    elif was_working and not working:
                        s.job_ended_at = now
                elif s.state_names:
                    s.state_names = []
            elif code == IN_SETTINGS_MAP:
                s.settings = payload
                s.settings_at = now
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
            except Exception:
                log.exception("PLC sample listener failed")
