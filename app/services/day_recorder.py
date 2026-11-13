"""Full-day recordings of every chamber."""

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
    """The local day of a timestamp, as Deepvac Control names its day files."""
    return datetime.fromtimestamp(epoch_s).date()


def write_day(
    path: Path,
    times: list[float],
    channels: dict[str, list[float]],
    chamber: str = "",
    labels: dict[str, dict] | None = None,
) -> Path:
    """Writes a day file atomically."""
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
        "deepvac.source": "Deepvac Insight",
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
    """Reads a day file."""
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
    """Lists the recorded days."""
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
    memory: tuple | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Samples of a chamber between two epoch times, across day files."""
    times_parts, parts = [], {}
    day = day_of(start)
    while day <= day_of(end):
        path = day_path(chamber, day, root)
        loaded = None
        if memory is not None and memory[0] == day:
            loaded = memory[1], memory[2]
        elif path.exists():
            try:
                times, values, _ = read_day(path, channels)
                loaded = times, values
            except Exception as exc:
                log.warning("day recording %s not readable: %s", path, exc)
        if loaded is not None:
            times, values = loaded
            keep = (times >= start) & (times <= end)
            times_parts.append(times[keep])
            for name, column in values.items():
                parts.setdefault(name, []).append((len(times_parts) - 1, column[keep]))
        day += timedelta(days=1)
    if not times_parts:
        return np.array([]), {}
    times = np.concatenate(times_parts)
    result = {}
    for name, pieces in parts.items():
        filled = [np.full(len(t), np.nan) for t in times_parts]
        for index, column in pieces:
            filled[index] = column
        result[name] = np.concatenate(filled)
    return times, result


class _Day:
    def __init__(self, day: date):
        self.day = day
        self.times: list[float] = []
        self.channels: dict[str, list[float]] = {}
        self.dirty = False


class DayRecorder:
    """Collects samples per chamber into day files."""

    def __init__(self, root: Path | None = None, labels=None):
        """Day recorder; labels returns the channel descriptions for the files."""
        self._root = root
        self._labels = labels
        self._lock = threading.Lock()
        self._days: dict[str, _Day] = {}
        self._finished: list[tuple[str, _Day]] = []

    def add(self, chamber: str, sample: dict) -> bool:
        stamp = sample.get("timestamp")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            return False
        with self._lock:
            current = self._days.get(chamber)
            day = day_of(stamp)
            if current is None or current.day != day:
                if current is not None and current.dirty:
                    self._finished.append((chamber, current))
                current = self._open(chamber, day)
                self._days[chamber] = current
            if current.times and stamp <= current.times[-1]:
                return False
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
                    column = current.channels[name] = [math.nan] * n
                column.append(float(value))
            for column in current.channels.values():
                if len(column) < n + 1:
                    column.append(math.nan)
            current.dirty = True
        return True

    def flush(self) -> None:
        """Writes every day with new samples."""
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

    def day_status(self, chamber: str) -> dict | None:
        """Returns the status of the day being recorded, or None."""
        with self._lock:
            current = self._days.get(chamber)
            if current is None:
                return None
            return {
                "day": current.day,
                "samples": len(current.times),
                "path": day_path(chamber, current.day, self._root),
                "pending": current.dirty,
                "last": current.times[-1] if current.times else None,
            }

    def window(
        self, chamber: str, start: float, end: float, channels: list[str] | None = None
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Returns a chamber's samples between two times."""
        with self._lock:
            current = self._days.get(chamber)
            memory = None
            if current is not None and current.times:
                memory = (
                    current.day,
                    np.asarray(current.times, dtype=np.float64),
                    {
                        name: np.asarray(values, dtype=np.float64)
                        for name, values in current.channels.items()
                        if channels is None or name in channels
                    },
                )
        return read_range(chamber, start, end, channels, self._root, memory)

    def _open(self, chamber: str, day: date) -> _Day:
        """Loads today's recording of a chamber."""
        current = _Day(day)
        path = day_path(chamber, day, self._root)
        if path.exists():
            try:
                times, values, _ = read_day(path)
                current.times = times.tolist()
                current.channels = {name: column.tolist() for name, column in values.items()}
            except Exception as exc:
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
    """Thread that feeds the PLC chambers' samples to the recorder."""

    POLL_S = 2.0

    def __init__(self, links, recorder: DayRecorder, experiments=None):
        """Feeds samples to the day recorder and the experiment recorder."""
        self.links = links
        self.recorder = recorder
        self.experiments = experiments
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
        if self.experiments is not None:
            self.experiments.flush()

    def poll_once(self) -> int:
        added = 0
        for chamber in self.links.chambers():
            if chamber.get("protocol") != "plc":
                continue
            client = self.links.client(chamber, wait=False)
            since = self._since.get(chamber["id"])
            for sample in client.history(since):
                if self.recorder.add(chamber["name"], sample):
                    added += 1
                if self.experiments is not None:
                    working = sample.get("status_condition") == 2
                    self.experiments.add(chamber["name"], sample, working)
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
