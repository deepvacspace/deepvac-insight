#!/usr/bin/env python3
"""Fake chamber PLC speaking the PLC gate protocol."""

import argparse
import random
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import plc_gate as pg  # noqa: E402

STATE_NAMES = [
    "temp",
    "temp_raw",
    "temp_ref",
    "temp_u",
    "temp_u_p",
    "temp_u_i",
    "temp_u_d",
    "pres_23_high",
    "temp_kp",
    "temp_ki",
    "temp_kd",
    "temp_pid_idx",
    "status_condition",
    "work_type",
    "interval",
    "intervals_count",
    "cycle_cur",
    "cycle_total",
    "t_total",
    "t_interval",
    "inputs",
    "outputs",
    "alarm_in_status",
    "alarm_out_status",
]
SETTINGS = {
    "alarm_cfg_1": 1.0,
    "alarm_cfg_2": 0.0,
    "temp_comp_alw": 40.0,
    "t_gis_404": 180.0,
    "t_delay_23": 30.0,
    "temp_cool_duty": 0.3,
    "teh_duty": 1.0,
    "p1p0": 5.0,
    "p1i0": 600.0,
    "p1d0": 6.0,
}
CONTROL_PARAMS = [
    {"name": "duration", "type": 256},
    {"name": "temp", "type": 0},
    {"name": "flags", "type": 257},
]
IDLE, WORK, FINISH, ALARM = 1, 2, 3, 4


class Chamber:
    def __init__(self, start_temp: float):
        self.lock = threading.Lock()
        self.temp = start_temp
        self.ref = start_temp
        self.status = IDLE
        self.work_type = 0
        self.intervals: list[dict] = []
        self.interval = 0
        self.cycle = {"count": 0, "start": 0, "end": 0}
        self.cycle_cur = 0
        self.interval_started = 0.0
        self.job_started = 0.0
        self.i_term = 0.0
        self.finished_pending = False

    def start_job(self, prog_type: int, intervals: list[dict], cycle: dict):
        with self.lock:
            self.work_type = prog_type
            self.intervals = intervals
            self.cycle = cycle
            self.cycle_cur = 1 if cycle.get("count") else 0
            self.interval = 0
            self.status = WORK
            self.job_started = self.interval_started = time.time()
            self._enter_interval()

    def _enter_interval(self):
        self.interval_start_temp = self.temp
        self.interval_started = time.time()

    def stop(self):
        with self.lock:
            self.status = IDLE
            self.intervals = []

    def next_interval(self):
        with self.lock:
            self._advance()

    def _advance(self):
        self.interval += 1
        if (
            self.cycle.get("count")
            and self.interval > self.cycle["end"] - 1 >= 0
            and self.cycle_cur < self.cycle["count"]
        ):
            self.interval = max(0, self.cycle["start"] - 1)
            self.cycle_cur += 1
        if self.interval >= len(self.intervals):
            self.status = FINISH
            self.finished_pending = True
            self.intervals = []
            return
        self._enter_interval()

    def step(self, dt: float) -> list[float]:
        with self.lock:
            if self.status == WORK and self.intervals:
                current = self.intervals[self.interval]
                elapsed = time.time() - self.interval_started
                target = current["temp"]
                if current["flags"] & (1 << pg.RAMP_BIT) and current["duration"] > 0:
                    fraction = min(1.0, elapsed / current["duration"])
                    self.ref = (
                        self.interval_start_temp + (target - self.interval_start_temp) * fraction
                    )
                else:
                    self.ref = target
                timed = current["flags"] & ((1 << pg.RAMP_BIT) | (1 << pg.AWAIT_BIT))
                if timed and elapsed >= current["duration"]:
                    self._advance()
            error = self.ref - self.temp
            self.i_term = max(-50.0, min(50.0, self.i_term + error * dt / 60.0))
            u_p = 5.0 * error
            u_i = self.i_term
            u = max(-100.0, min(100.0, u_p + u_i)) if self.status == WORK else 0.0
            self.temp += (u / 100.0) * dt * 0.08 + random.uniform(-0.02, 0.02)
            t_interval = time.time() - self.interval_started if self.status == WORK else 0.0
            values = {
                "temp": self.temp,
                "temp_raw": self.temp - 0.3,
                "temp_ref": self.ref,
                "temp_u": u,
                "temp_u_p": u_p,
                "temp_u_i": u_i,
                "temp_u_d": 0.0,
                "pres_23_high": 12.0,
                "temp_kp": 5.0,
                "temp_ki": 600.0,
                "temp_kd": 6.0,
                "temp_pid_idx": 1.0,
                "status_condition": float(self.status),
                "work_type": float(self.work_type),
                "interval": float(self.interval + 1 if self.status == WORK else 0),
                "intervals_count": float(len(self.intervals)),
                "cycle_cur": float(self.cycle_cur),
                "cycle_total": float(self.cycle.get("count", 0)),
                "t_total": time.time() - self.job_started if self.status == WORK else 0.0,
                "t_interval": t_interval,
                "inputs": 0.0,
                "outputs": 7.0 if self.status == WORK else 0.0,
                "alarm_in_status": 0.0,
                "alarm_out_status": 0.0,
            }
            return [values[name] for name in STATE_NAMES]


def decode_job(data: bytes) -> tuple[int, list[dict], dict]:
    prog_type, _, count, start, end, n = struct.unpack_from("<BHHHHH", data, 0)
    offset, intervals = 11, []
    for _ in range(n):
        interval = {}
        for param in CONTROL_PARAMS:
            fmt = "<I" if param["type"] in (pg.PARAM_TYPE_DURATION, pg.PARAM_TYPE_FLAGS) else "<f"
            (interval[param["name"]],) = struct.unpack_from(fmt, data, offset)
            offset += 4
        intervals.append(interval)
    return prog_type, intervals, {"count": count, "start": start, "end": end}


def serve_client(conn: socket.socket, chamber: Chamber, interval: float):
    send_lock = threading.Lock()
    alive = threading.Event()
    alive.set()

    def send(body: bytes):
        with send_lock:
            conn.sendall(pg.make_package(body))

    def names_body(code, names):
        body = bytearray([code, len(names)])
        for name in names:
            body += pg.pascal_string(name)
        return bytes(body)

    def stream():
        last = time.time()
        while alive.is_set():
            now = time.time()
            values = chamber.step(now - last)
            last = now
            try:
                send(
                    bytes([pg.IN_STATE_VALUES, len(values)])
                    + struct.pack(f"<{len(values)}f", *values)
                )
                if chamber.finished_pending:
                    chamber.finished_pending = False
                    send(bytes([pg.IN_JOB_FINISHED]))
            except OSError:
                break
            time.sleep(interval)

    threading.Thread(target=stream, daemon=True).start()
    reader = pg.PacketReader()
    try:
        send(names_body(pg.IN_COMPONENTS, ["sensor", "heater", "fridge"]))
        while True:
            data = conn.recv(65536)
            if not data:
                break
            for body in reader.feed(data):
                code, payload = body[0], body[1:]
                if code == pg.OUT_TEXT_CMD:
                    command, _ = pg.read_pascal_string(payload, 0)
                    if command != "ka":
                        print(f"[fake-plc] command: {command}")
                    if command == "get_states":
                        send(names_body(pg.IN_STATE_NAMES, STATE_NAMES))
                    elif command == "get_settings":
                        body = bytearray([pg.IN_SETTINGS_MAP, len(SETTINGS)])
                        for name, value in SETTINGS.items():
                            body += pg.pascal_string(name) + struct.pack("<f", value)
                        send(bytes(body))
                    elif command == "stop":
                        chamber.stop()
                    elif command == "next":
                        chamber.next_interval()
                    elif command == "confrim_finish":
                        with chamber.lock:
                            chamber.status = IDLE
                elif code == pg.OUT_JOB:
                    prog_type, intervals, cycle = decode_job(payload)
                    print(f"[fake-plc] job type {prog_type}: {intervals} cycle {cycle}")
                    chamber.start_job(prog_type, intervals, cycle)
                elif code == pg.OUT_SETTINGS_MAP:
                    _, settings = pg.decode_message(bytes([pg.IN_SETTINGS_MAP]) + payload)
                    print(f"[fake-plc] settings: {settings}")
                    SETTINGS.update(settings)
    except OSError:
        pass
    finally:
        alive.clear()
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Fake chamber PLC (PLC gate protocol).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4321)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between state values")
    ap.add_argument("--start-temp", type=float, default=24.0)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    chamber = Chamber(args.start_temp)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(5)
    print(f"[fake-plc] listening on {args.host}:{args.port}", flush=True)
    try:
        while True:
            conn, addr = server.accept()
            print(f"[fake-plc] client {addr}", flush=True)
            threading.Thread(
                target=serve_client, args=(conn, chamber, args.interval), daemon=True
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    main()
