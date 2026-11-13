"""The Brain's experiments database."""

import json
import math
import os
import sqlite3
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

import app.common as common

MAX_ROWS = 200
MAX_CELL = 300
TIME_LIMIT_S = 5.0

SCHEMA = {
    "experiments": (
        "One row per recorded experiment (run), numbered like the analyst tools (#N = number, oldest first).",
        {
            "number": "experiment number: #12 is number 12",
            "key": "run database key",
            "name": "title",
            "group_name": "folder / series it belongs to (e.g. 0degrees, neg70degrees, cooldown_run_history)",
            "source": "import (Deepvac Control), folder (logger files), monitoring (recorded by Insight)",
            "chamber": "chamber name, when recorded",
            "test_profile": "test profile it ran, when recorded",
            "start_time": "local start, YYYY-MM-DD HH:MM:SS",
            "end_time": "local end",
            "start_epoch": "start, Unix seconds",
            "duration_s": "length in seconds",
            "samples": "number of samples",
            "has_control": "1 when temperature and setpoint were recorded (KPIs exist)",
            "setpoints": "setpoint levels in order, e.g. '24 → 0'",
            "final_setpoint_c": "last setpoint level, °C",
            "min_reference_c": "lowest setpoint, °C",
            "max_reference_c": "highest setpoint, °C",
            "max_ramp_rate_c_per_min": "fastest setpoint ramp, °C/min",
            "kp": "PID Kp most used during the run",
            "ki": "PID Ki most used",
            "kd": "PID Kd most used",
            "gain_sets": "how many different (Kp, Ki, Kd) were used",
            "stages": "number of stages (steps + ramps + holds)",
            "steps": "setpoint step stages",
            "ramps": "ramp stages",
            "holds": "hold stages",
            "program_intervals": "highest PLC program interval reached",
            "cycles": "highest PLC program cycle reached",
            "mae": "mean |setpoint - temperature| over the run, °C",
            "rmse": "root mean square error, °C",
            "tail_mae": "MAE over the last 300 s, °C (the headline when there is no ramp)",
            "tail_std": "temperature standard deviation over the last 300 s, °C",
            "overshoot": "worst overshoot past a setpoint, °C",
            "undershoot": "worst undershoot after reaching a setpoint, °C",
            "settling_time_s": "time until the last setpoint is held within ±0.5 °C, s",
            "steady_state_error": "mean error after settling, °C",
            "final_error": "error at the end, °C",
            "cost": "weighted score used to rank PID settings (lower is better)",
            "saturated_fraction": "share of time the control output sat at ±100 %",
            "pid_share_p": "share of the control output from the P term",
            "pid_share_i": "share from the I term",
            "pid_share_d": "share from the D term",
            "anomalies_critical": "critical anomalies detected",
            "anomalies_warning": "warning anomalies",
            "anomalies_info": "information anomalies",
            "dut": "device under test, when recorded",
            "operator": "operator, when recorded",
            "purpose": "purpose, when recorded",
        },
    ),
    "stages": (
        "The stages of each experiment (setpoint step, ramp, hold) with their KPIs.",
        {
            "experiment": "experiments.number",
            "stage": "1, 2, ... in order",
            "kind": "setpoint (moving to a new constant setpoint), ramp, hold (at the setpoint), unknown",
            "start_s": "start, seconds from the experiment start",
            "end_s": "end, seconds from the start",
            "duration_s": "length, s",
            "reference_start_c": "setpoint at the start, °C",
            "reference_end_c": "setpoint at the end, °C",
            "start_temp_c": "temperature at the start, °C",
            "end_temp_c": "temperature at the end, °C",
            "mae": "MAE of the stage, °C",
            "tail_mae": "MAE over its last 300 s, °C",
            "overshoot": "overshoot of a step, °C",
            "settling_time_s": "settling time of a step, s",
            "cost": "cost of a step response (as experiments.cost), lower is better",
            "time_to_steady_s": "time until a steady state began, s",
            "steady_fraction": "share of the stage spent steady",
            "saturated_fraction": "share of time the output sat at ±100 %",
            "ramp_rate_c_per_min": "setpoint rate of a ramp, °C/min",
            "tracking_lag_s": "how far the temperature lags a ramp, s",
            "kp": "Kp in use (median)",
            "ki": "Ki in use",
            "kd": "Kd in use",
            "program_interval": "PLC program interval, when recorded",
        },
    ),
    "anomalies": (
        "Anomalies detected in each experiment.",
        {
            "experiment": "experiments.number",
            "kind": "limit, spike, flatline, gap, oscillation, not_reached, saturation",
            "severity": "info, warning, critical",
            "channel": "channel it concerns",
            "start_s": "start, seconds from the experiment start",
            "end_s": "end",
            "detail": "description",
        },
    ),
    "events": (
        "What happened when: events recorded with experiments (job start/end, setpoint and PID changes, alarms) "
        "and Deepvac Control's event log (application, operator and PLC messages).",
        {
            "time": "local time, YYYY-MM-DD HH:MM:SS",
            "epoch": "Unix seconds",
            "source": "'experiment' (recorded with a run) or 'control log' (Deepvac Control's log)",
            "experiment": "experiments.number for events of a run, else NULL",
            "type": "event type (job_start, setpoint_change, alarm_raised, ... ; control log: app, plc, error)",
            "message": "text",
        },
    ),
    "test_profiles": (
        "Test profiles (programs) that can be run.",
        {
            "name": "profile name",
            "description": "description",
            "intervals": "number of PLC intervals",
            "setpoints": "setpoint levels, °C",
            "duration_h": "programmed length, hours",
        },
    ),
    "recordings": (
        "Days of full-day recording (every sample, experiment or not) per chamber; read them with get_recording.",
        {
            "chamber": "chamber",
            "day": "YYYY-MM-DD",
            "samples": "samples that day",
            "mb": "file size, MB",
        },
    ),
}


def brain_db_path() -> Path:
    return common.DATA_DIR / "deepvac_brain.sqlite3"


def control_log_path() -> Path:
    return common.DATA_DIR / "deepvac_control_log.sqlite3"


def read_actions_dat(path: Path) -> list[dict]:
    """Reads Deepvac Control's event log file."""
    raw = path.read_bytes()
    pos = 0

    def take(fmt):
        nonlocal pos
        value = struct.unpack_from(fmt, raw, pos)
        pos += struct.calcsize(fmt)
        return value[0]

    def qstring():
        size = take(">I")
        if size == 0xFFFFFFFF:
            return ""
        nonlocal pos
        text = raw[pos : pos + size].decode("utf-16-be")
        pos += size
        return text

    def variant():
        kind = take(">I")
        take(">B")
        if kind == 1:
            return bool(take(">B"))
        if kind in (2, 3):
            return take(">i") if kind == 2 else take(">I")
        if kind in (4, 5):
            return take(">q") if kind == 4 else take(">Q")
        if kind == 6:
            return take(">d")
        if kind == 10:
            return qstring()
        if kind == 8:
            return {qstring(): variant() for _ in range(take(">I"))}
        if kind == 9:
            return [variant() for _ in range(take(">I"))]
        if kind == 16:
            day, msecs, spec = take(">q"), take(">I"), take(">B")
            offset = take(">i") if spec == 2 else 0
            stamp = (day - 2440588) * 86400 + msecs / 1000.0 - offset
            if spec == 0:
                stamp = (datetime(1970, 1, 1) + timedelta(seconds=stamp)).timestamp()
            return stamp
        raise ValueError(f"QVariant type {kind} at byte {pos}")

    events = []
    for item in [variant() for _ in range(take(">I"))]:
        if isinstance(item, dict) and item.get("date") is not None:
            events.append(
                {
                    "epoch": float(item["date"]),
                    "type": int(item.get("type", 0)),
                    "message": item.get("msg", ""),
                }
            )
    return events


CONTROL_LOG_TYPES = {0: "app", 1: "plc", 2: "error"}


def import_control_log(actions_dat: Path) -> int:
    """Imports Deepvac Control's event log."""
    events = read_actions_dat(actions_dat)
    conn = sqlite3.connect(control_log_path())
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events (epoch REAL NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL, "
            "UNIQUE (epoch, type, message))"
        )
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?)",
            [
                (e["epoch"], CONTROL_LOG_TYPES.get(e["type"], str(e["type"])), e["message"])
                for e in events
            ],
        )
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before
    finally:
        conn.close()


def _num(value):
    if value is None or isinstance(value, str):
        return value
    value = float(value)
    return value if math.isfinite(value) else None


def _local(epoch):
    return (
        datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        if epoch and math.isfinite(epoch)
        else None
    )


def build(progress=None) -> dict:
    """Rebuilds the experiments database."""
    from app.api.runs import RunCatalogue
    from app.core.analysis import anomalies as anomaly_mod
    from app.core.analysis.kpis import control_cost
    from app.services import day_recorder, plc_programs, test_profiles_service

    target = brain_db_path()
    tmp = target.with_name(target.name + ".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    for table, (_, columns) in SCHEMA.items():
        conn.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
    conn.execute("CREATE TABLE meta (key, value)")

    catalogue = RunCatalogue(cache_size=2)
    entries = catalogue.entries()
    for done, entry in enumerate(entries, start=1):
        if progress:
            progress(done, len(entries))
        try:
            data, a = catalogue.load(entry)
        except Exception:
            continue
        manifest = catalogue.manifest(entry) or {}
        details = manifest.get("metadata") or {}
        c, k = a.config, a.run.overall
        count = a.anomaly_count
        row = {
            "number": entry.number,
            "key": entry.key,
            "name": entry.name,
            "group_name": entry.group,
            "source": entry.source,
            "chamber": entry.chamber,
            "test_profile": entry.test_profile,
            "start_time": _local(entry.start_epoch),
            "end_time": _local(entry.start_epoch + (data.times[-1] if data.times else 0)),
            "start_epoch": _num(entry.start_epoch),
            "duration_s": _num(data.times[-1] - data.times[0]) if len(data.times) > 1 else 0,
            "samples": len(data.times),
            "has_control": int(a.has_control),
            "setpoints": " → ".join(f"{s:g}" for s in c.setpoints) or None,
            "final_setpoint_c": _num(c.setpoints[-1]) if c.setpoints else None,
            "min_reference_c": _num(c.min_reference),
            "max_reference_c": _num(c.max_reference),
            "max_ramp_rate_c_per_min": _num(c.max_ramp_rate_per_min),
            "kp": _num(c.kp),
            "ki": _num(c.ki),
            "kd": _num(c.kd),
            "gain_sets": c.gain_sets,
            "stages": c.stages,
            "steps": c.steps,
            "ramps": c.ramps,
            "holds": c.holds,
            "program_intervals": c.program_intervals or None,
            "cycles": c.cycles or None,
            "mae": _num(k.mae) if a.has_control else None,
            "rmse": _num(k.rmse) if a.has_control else None,
            "tail_mae": _num(k.tail_mae) if a.has_control else None,
            "tail_std": _num(k.tail_std) if a.has_control else None,
            "overshoot": _num(a.run.overshoot) if a.has_control else None,
            "undershoot": _num(a.run.undershoot) if a.has_control else None,
            "settling_time_s": _num(a.run.settling_time_s) if a.has_control else None,
            "steady_state_error": _num(k.steady_state_error) if a.has_control else None,
            "final_error": _num(k.final_error) if a.has_control else None,
            "cost": _num(a.run.cost) if a.has_control else None,
            "saturated_fraction": _num(a.saturation.fraction),
            "pid_share_p": _num(a.pid.share_p),
            "pid_share_i": _num(a.pid.share_i),
            "pid_share_d": _num(a.pid.share_d),
            "anomalies_critical": count(anomaly_mod.CRITICAL),
            "anomalies_warning": count(anomaly_mod.WARNING) - count(anomaly_mod.CRITICAL),
            "anomalies_info": len(a.anomalies) - count(anomaly_mod.WARNING),
            "dut": details.get("dut") or None,
            "operator": details.get("operator") or None,
            "purpose": details.get("purpose") or None,
        }
        _insert(conn, "experiments", row)
        temps = data.channels.get("temp") or []
        for i, (stage, d) in enumerate(zip(a.run.stages, a.stage_details, strict=False), start=1):
            s = stage.segment
            step = s.kind == "setpoint"
            _insert(
                conn,
                "stages",
                {
                    "experiment": entry.number,
                    "stage": i,
                    "kind": s.kind,
                    "start_s": _num(s.start),
                    "end_s": _num(s.end),
                    "duration_s": _num(s.end - s.start),
                    "reference_start_c": _num(s.reference_start),
                    "reference_end_c": _num(s.reference_end),
                    "start_temp_c": _num(temps[s.first]) if s.first < len(temps) else None,
                    "end_temp_c": _num(temps[s.last]) if s.last < len(temps) else None,
                    "mae": _num(stage.kpis.mae),
                    "tail_mae": _num(stage.kpis.tail_mae),
                    "overshoot": _num(stage.kpis.overshoot) if step else None,
                    "settling_time_s": _num(stage.kpis.settling_time_s) if step else None,
                    "cost": _num(control_cost(stage.kpis)) if step else None,
                    "time_to_steady_s": _num(d.time_to_steady_s),
                    "steady_fraction": _num(d.steady_fraction),
                    "saturated_fraction": _num(d.saturation.fraction),
                    "ramp_rate_c_per_min": _num(d.reference_rate_per_min)
                    if s.kind == "ramp"
                    else None,
                    "tracking_lag_s": _num(d.tracking_lag_s) if s.kind == "ramp" else None,
                    "kp": _num(d.kp),
                    "ki": _num(d.ki),
                    "kd": _num(d.kd),
                    "program_interval": s.interval if s.interval >= 0 else None,
                },
            )
        for x in a.anomalies:
            _insert(
                conn,
                "anomalies",
                {
                    "experiment": entry.number,
                    "kind": x.kind,
                    "severity": x.severity,
                    "channel": x.channel,
                    "start_s": _num(x.start),
                    "end_s": _num(x.end),
                    "detail": x.detail,
                },
            )
        for e in manifest.get("events") or []:
            t = e.get("t")
            if t is None:
                continue
            _insert(
                conn,
                "events",
                {
                    "time": _local(t),
                    "epoch": t,
                    "source": "experiment",
                    "experiment": entry.number,
                    "type": e.get("type"),
                    "message": e.get("message"),
                },
            )

    if control_log_path().exists():
        log = sqlite3.connect(control_log_path())
        try:
            for epoch, kind, message in log.execute(
                "SELECT epoch, type, message FROM events ORDER BY epoch"
            ):
                _insert(
                    conn,
                    "events",
                    {
                        "time": _local(epoch),
                        "epoch": epoch,
                        "source": "control log",
                        "experiment": None,
                        "type": kind,
                        "message": message,
                    },
                )
        finally:
            log.close()

    for profile in test_profiles_service.list_profiles():
        try:
            intervals, cycle = plc_programs.program_for_profile(profile, 0b111)
            setpoints = sorted({i.get("temp") for i in intervals if i.get("temp") is not None})
            hours = plc_programs.total_duration_s(intervals, cycle) / 3600
        except plc_programs.ProgramError:
            intervals, setpoints, hours = [], [], None
        _insert(
            conn,
            "test_profiles",
            {
                "name": profile["name"],
                "description": profile.get("description"),
                "intervals": len(intervals),
                "setpoints": ", ".join(f"{s:g}" for s in setpoints) or None,
                "duration_h": hours,
            },
        )

    import pyarrow.parquet as pq

    for day in day_recorder.list_days():
        try:
            rows = pq.ParquetFile(day["path"]).metadata.num_rows
        except Exception:
            rows = None
        _insert(
            conn,
            "recordings",
            {
                "chamber": day["chamber"],
                "day": day["day"],
                "samples": rows,
                "mb": round(day["bytes"] / 1e6, 3),
            },
        )

    conn.execute("CREATE INDEX idx_stages ON stages (experiment)")
    conn.execute("CREATE INDEX idx_anomalies ON anomalies (experiment)")
    conn.execute("CREATE INDEX idx_events ON events (epoch)")
    conn.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [
            ("built_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("schema", json.dumps(list(SCHEMA))),
        ],
    )
    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in SCHEMA}
    conn.close()
    os.replace(tmp, target)
    return counts


def _insert(conn, table, row):
    columns = list(SCHEMA[table][1])
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [row.get(c) for c in columns],
    )


class QueryError(ValueError):
    pass


def schema() -> dict:
    """Tables, columns with their meaning, and how many rows each holds."""
    counts, built = {}, None
    if brain_db_path().exists():
        conn = _read_only()
        try:
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in SCHEMA}
            row = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
            built = row[0] if row else None
        finally:
            conn.close()
    return {
        "built_at": built,
        "tables": {
            t: {"holds": what, "rows": counts.get(t), "columns": columns}
            for t, (what, columns) in SCHEMA.items()
        },
    }


def _read_only():
    path = brain_db_path()
    if not path.exists():
        raise QueryError("The experiments database is not built yet (scripts/build_brain.py).")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def query(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Runs one read-only query on the experiments database."""
    text = sql.strip().rstrip(";").strip()
    if not text.lower().startswith(("select", "with")):
        raise QueryError("Only SELECT (or WITH ... SELECT) statements.")
    if ";" in text:
        raise QueryError("One statement at a time.")
    conn = _read_only()
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

    def authorizer(action, *_):
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    deadline = time.monotonic() + TIME_LIMIT_S
    conn.set_authorizer(authorizer)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = conn.execute(text)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(max(1, min(int(max_rows), MAX_ROWS)) + 1)
    except sqlite3.DatabaseError as exc:
        message = "took longer than 5 s" if "interrupted" in str(exc) else str(exc)
        raise QueryError(f"Query failed: {message}") from exc
    finally:
        conn.close()
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]

    def cell(v):
        if isinstance(v, float):
            return float(f"{v:.6g}")
        if isinstance(v, str) and len(v) > MAX_CELL:
            return v[: MAX_CELL - 1] + "…"
        return v

    result = {
        "columns": columns,
        "rows": [[cell(v) for v in r] for r in rows],
        "row_count": len(rows),
    }
    if truncated:
        result["note"] = (
            f"Only the first {max_rows} rows; narrow the query (WHERE, LIMIT) or aggregate."
        )
    return result
