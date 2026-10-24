"""Full-day recordings: every sample of every chamber, one Apache Parquet file per chamber and day.

    data/recordings/<chamber>/YYYY-MM-DD.parquet

The format is DeepVac Control's (control2-client src/storage/recordingfile.h),
"deepvac.recording/1", so either application reads the other's files:

    timestamp      TIMESTAMP(MICROS, UTC), not null
    <channel>      DOUBLE, not null, one column per value (a gap is NaN)
    footer         deepvac.format, deepvac.day, deepvac.chamber, deepvac.channels (JSON:
                   name, label, unit), deepvac.source

written with GZIP, PLAIN encoding (no dictionary) and data pages v1: the subset DeepVac
Control's own Parquet reader takes. pandas, pyarrow and DuckDB read them as they are
(`duckdb.sql("SELECT avg(temp) FROM 'data/recordings/*/2026-10-24.parquet'")`).

DayRecorder keeps each chamber's day in memory; flush() rewrites the day's file atomically
(Parquet files can't be appended to) and a restart picks the day up from its file. add() never
writes, so samples can come from the GUI thread. RecordingFeed runs in a thread: it reads the PLC
connections (ChamberLinks) and flushes every FLUSH_S seconds; the desktop app also gives the
recorder the samples of connected sample-feed chambers.
"""

import json
import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import app.common as common

log = logging.getLogger(__name__)

FORMAT = "deepvac.recording/1"
TIME_COLUMN = "timestamp"
FLUSH_S = 60.0
# what DeepVac Control's reader takes (src/storage/parquet.h); level 6: a fifth smaller than 3, 9 gains nothing
WRITE_OPTIONS = {
    "compression": "gzip",
    "compression_level": 6,
    "use_dictionary": False,
    "data_page_version": "1.0",
}


def recordings_dir() -> Path:
    return common.DATA_DIR / "recordings"


def safe_name(chamber: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", chamber).strip(" .") or "chamber"


def day_path(chamber: str, day: date, root: Path | None = None) -> Path:
    return (root or recordings_dir()) / safe_name(chamber) / f"{day:%Y-%m-%d}.parquet"


def day_of(epoch_s: float) -> date:
    """The local day of a timestamp, as DeepVac Control names its day files."""
    return datetime.fromtimestamp(epoch_s).date()


# ---------------------------------------------------------------- files


def write_day(
    path: Path,
    times: list[float],
    channels: dict[str, list[float]],
    chamber: str = "",
    labels: dict[str, dict] | None = None,
) -> Path:
    """Writes (replaces) a day file atomically. labels: {channel: {"label", "unit"}}."""
    labels = labels or {}
    micros = np.round(np.asarray(times, dtype=np.float64) * 1e6).astype(np.int64)
    fields = [pa.field(TIME_COLUMN, pa.timestamp("us", tz="UTC"), nullable=False)]
    arrays = [pa.array(micros, type=pa.timestamp("us", tz="UTC"))]
    for name, values in channels.items():
        fields.append(pa.field(name, pa.float64(), nullable=False))
        arrays.append(pa.array(np.asarray(values, dtype=np.float64), type=pa.float64()))
    described = [
        {
            "name": name,
            "label": labels.get(name, {}).get("label", ""),
            "unit": labels.get(name, {}).get("unit", ""),
        }
        for name in channels
    ]
    metadata = {
        "deepvac.format": FORMAT,
        "deepvac.day": f"{day_of(times[0]):%Y-%m-%d}" if len(times) else "",
        "deepvac.chamber": chamber,
        "deepvac.channels": json.dumps(described, ensure_ascii=False),
        "deepvac.source": "DeepVac Insight",
    }
    table = pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=metadata))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, **WRITE_OPTIONS)
    os.replace(tmp, path)
    return path


def read_day(
    path: Path, channels: list[str] | None = None
) -> tuple[np.ndarray, dict[str, np.ndarray], dict]:
    """(epoch seconds, {channel: values}, footer metadata) of a day file, written by either application."""
    table = pq.read_table(path)
    meta = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    time_name = next(
        (f.name for f in table.schema if pa.types.is_timestamp(f.type)),
        TIME_COLUMN if TIME_COLUMN in table.column_names else None,
    )
    if time_name is None:
        raise ValueError(f"{path.name} has no timestamp column")
    column = table.column(time_name)
    if pa.types.is_timestamp(column.type):
        micros = column.cast(pa.timestamp("us", tz="UTC")).cast(pa.int64())
    else:
        micros = column.cast(pa.int64())
    times = micros.to_numpy().astype(np.float64) / 1e6
    names = [n for n in table.column_names if n != time_name]
    if channels:
        names = [n for n in channels if n in table.column_names]
    values = {}
    for name in names:
        col = table.column(name)
        if pa.types.is_floating(col.type) or pa.types.is_integer(col.type):
            values[name] = col.cast(pa.float64()).to_numpy(zero_copy_only=False)
    return times, values, meta


def list_days(chamber: str | None = None, root: Path | None = None) -> list[dict]:
    """Recorded days: [{chamber, day, path, bytes}], oldest first."""
    base = root or recordings_dir()
    if not base.exists():
        return []
    folders = (
        [base / safe_name(chamber)] if chamber else sorted(p for p in base.iterdir() if p.is_dir())
    )
    days = []
    for folder in folders:
        for path in sorted(folder.glob("????-??-??.parquet")):
            days.append(
                {
                    "chamber": folder.name,
                    "day": path.stem,
                    "path": path,
                    "bytes": path.stat().st_size,
                }
            )
    return sorted(days, key=lambda d: (d["day"], d["chamber"]))


def read_range(
    chamber: str,
    start: float,
    end: float,
    channels: list[str] | None = None,
    root: Path | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Samples of a chamber between two epoch times, across day files."""
    times_parts, parts = [], {}
    day = day_of(start)
    while day <= day_of(end):
        path = day_path(chamber, day, root)
        if path.exists():
            times, values, _ = read_day(path, channels)
            keep = (times >= start) & (times <= end)
            times_parts.append(times[keep])
            for name, column in values.items():
                parts.setdefault(name, []).append((len(times_parts) - 1, column[keep]))
        day += timedelta(days=1)
    if not times_parts:
        return np.array([]), {}
    times = np.concatenate(times_parts)
    result = {}
    for name, pieces in parts.items():  # a channel missing from some days: NaN there
        filled = [np.full(len(t), np.nan) for t in times_parts]
        for index, column in pieces:
            filled[index] = column
        result[name] = np.concatenate(filled)
    return times, result


# ---------------------------------------------------------------- recording


class _Day:
    def __init__(self, day: date):
        self.day = day
        self.times: list[float] = []
        self.channels: dict[str, list[float]] = {}
        self.dirty = False


class DayRecorder:
    """Collects samples ({"timestamp": epoch s, channel: value, ...}) per chamber into day files."""

    def __init__(self, root: Path | None = None, labels=None):
        """labels: callable(chamber) -> {channel: {"label", "unit"}} for the files' footer."""
        self._root = root
        self._labels = labels
        self._lock = threading.Lock()
        self._days: dict[str, _Day] = {}
        self._finished: list[tuple[str, _Day]] = []  # days that ended, still to write

    def add(self, chamber: str, sample: dict) -> bool:
        stamp = sample.get("timestamp")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            return False
        with self._lock:
            current = self._days.get(chamber)
            day = day_of(stamp)
            if current is None or current.day != day:
                if current is not None and current.dirty:
                    self._finished.append(
                        (chamber, current)
                    )  # yesterday: written at the next flush
                current = self._open(chamber, day)
                self._days[chamber] = current
            if current.times and stamp <= current.times[-1]:
                return False  # already have it (history overlap, a restart)
            n = len(current.times)
            current.times.append(float(stamp))
            for name, value in sample.items():
                if (
                    name == "timestamp"
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                ):
                    continue
                column = current.channels.get(name)
                if column is None:
                    column = current.channels[name] = [
                        math.nan
                    ] * n  # a new channel: NaN before it appeared
                column.append(float(value))
            for column in current.channels.values():
                if len(column) < n + 1:
                    column.append(math.nan)
            current.dirty = True
        return True

    def flush(self) -> None:
        """Writes every day with new samples (days that ended first)."""
        with self._lock:
            pending = self._finished + [
                (chamber, day) for chamber, day in self._days.items() if day.dirty
            ]
            self._finished = []
        for chamber, day in pending:
            self._write(chamber, day)

    def chambers(self) -> list[str]:
        with self._lock:
            return list(self._days)

    def _open(self, chamber: str, day: date) -> _Day:
        """The day so far: from its file when this chamber was recorded earlier today."""
        current = _Day(day)
        path = day_path(chamber, day, self._root)
        if path.exists():
            try:
                times, values, _ = read_day(path)
                current.times = times.tolist()
                current.channels = {name: column.tolist() for name, column in values.items()}
            except Exception as exc:  # a damaged file: kept aside, today starts again
                log.warning("day recording %s not readable (%s); kept as .bad", path, exc)
                path.replace(path.with_name(path.name + ".bad"))
        return current

    def _write(self, chamber: str, day: _Day) -> None:
        with self._lock:
            times = list(day.times)
            channels = {name: list(values) for name, values in day.channels.items()}
            day.dirty = False
        if not times:
            return
        try:
            labels = self._labels(chamber) if self._labels else None
            write_day(day_path(chamber, day.day, self._root), times, channels, chamber, labels)
        except Exception:
            log.exception("day recording of %s not written", chamber)
            day.dirty = True


class RecordingFeed:
    """The recorder's thread: reads every PLC chamber's connection (ChamberLinks) and flushes."""

    POLL_S = 2.0

    def __init__(self, links, recorder: DayRecorder):
        self.links = links
        self.recorder = recorder
        self._since: dict[int, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="day-recording", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.recorder.flush()

    def poll_once(self) -> int:
        added = 0
        for chamber in self.links.chambers():
            if chamber.get("protocol") != "plc":
                continue
            client = self.links.client(chamber, wait=False)  # connects when it isn't yet
            since = self._since.get(chamber["id"])
            for sample in client.history(since):
                if self.recorder.add(chamber["name"], sample):
                    added += 1
                self._since[chamber["id"]] = sample["timestamp"]
        return added

    def _run(self) -> None:
        last_flush = time.monotonic()
        while not self._stop.wait(self.POLL_S):
            try:
                self.poll_once()
                if time.monotonic() - last_flush >= FLUSH_S:
                    last_flush = time.monotonic()
                    self.recorder.flush()
            except Exception:
                log.exception("day recording")
