"""Run catalogue for the agent tools: numbering, references and cached analyses.

Runs are numbered #1, #2, ... by start time, oldest first, like the
control2-client assistant, and can also be referred to by database key,
title, "latest" or "previous".
"""

import json
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.analysis import ExperimentAnalysis, analyse_experiment
from app.core.analysis.anomalies import LimitRule
from app.core.analysis.common import NaN
from app.core.analysis.experimentanalysis import ExperimentInput
from app.core.analysis.runsamples import columns_from_rows
from app.services import annotations_service, data_service, run_export

CATALOGUE_COLUMNS = (
    "key, id, group_name, source, chamber, test_profile, start_time, end_time, samples, duration_s, "
    "mae, cost, tail_mae, overshoot, settle_time_s, start_epoch"
)


class RunReferenceError(ValueError):
    pass


def _start_epoch(row) -> float:
    if row["start_epoch"] is not None:
        return float(row["start_epoch"])
    text = (row["start_time"] or "").replace(" UTC", "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return -math.inf  # unknown start: numbered first


_RUN_TOKEN = re.compile(r"(\d{10}_[0-9a-f]{8})")


def _token(row) -> str | None:
    match = _RUN_TOKEN.search(row["key"]) or _RUN_TOKEN.search(row["id"] or "")
    return match.group(1) if match else None


def _without_duplicates(rows):
    """Drops folder-synced runs that are also in the database as imported runs.

    control2-client imported Insight's run history, so after its database is
    merged here the same experiment exists twice: keep the imported copy,
    which has the manifest (channel units, events). The folder row stays in
    the database; sync_cache() would recreate it anyway.
    """
    imported = {(_token(r), r["start_time"]) for r in rows if r["source"] != "folder" and _token(r)}
    return [
        r
        for r in rows
        if not (r["source"] == "folder" and (_token(r), r["start_time"]) in imported)
    ]


@dataclass
class CatalogueEntry:
    number: int
    key: str
    name: str
    group: str
    source: str
    chamber: str | None
    test_profile: str | None
    start_time: str
    end_time: str
    start_epoch: float
    samples: int
    duration_s: float | None
    mae: float | None
    cost: float | None
    tail_mae: float | None
    overshoot: float | None
    settle_time_s: float | None

    @property
    def ref(self) -> str:
        return f"#{self.number}"


class RunCatalogue:
    """Numbered list of every run in the run database, rebuilt when it changes."""

    def __init__(self, cache_size: int = 16):
        self._lock = threading.Lock()
        self._entries: list[CatalogueEntry] = []
        self._signature = None
        self._analyses: OrderedDict[str, tuple[ExperimentInput, ExperimentAnalysis]] = OrderedDict()
        self._cache_size = cache_size

    def entries(self) -> list[CatalogueEntry]:
        conn = data_service.connect_cache()
        try:
            signature = conn.execute("SELECT COUNT(*), MAX(cached_at) FROM runs").fetchone()
            signature = tuple(signature) + (str(data_service.cache_path()),)
            with self._lock:
                if signature == self._signature:
                    return self._entries
            rows = conn.execute(f"SELECT {CATALOGUE_COLUMNS} FROM runs").fetchall()
        finally:
            conn.close()
        rows = sorted(_without_duplicates(rows), key=lambda r: (_start_epoch(r), r["key"]))
        entries = [_entry(i + 1, r) for i, r in enumerate(rows)]
        with self._lock:
            self._entries, self._signature = entries, signature
            self._analyses.clear()
        return entries

    def entry_for_key(self, key: str) -> CatalogueEntry:
        """The run with this database key; a folder copy hidden from the numbering gets number 0."""
        for entry in self.entries():
            if entry.key == key:
                return entry
        conn = data_service.connect_cache()
        try:
            row = conn.execute(
                f"SELECT {CATALOGUE_COLUMNS} FROM runs WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RunReferenceError(f"Run {key} is not in the database.")
        return _entry(0, row)

    def resolve(self, reference: str) -> CatalogueEntry:
        entries = self.entries()
        if not entries:
            raise RunReferenceError("There are no recorded runs.")
        text = str(reference).strip()
        lowered = text.lower()
        if lowered in ("latest", "last", "newest"):
            return entries[-1]
        if lowered == "previous":
            if len(entries) < 2:
                raise RunReferenceError("There is no run before the latest one.")
            return entries[-2]
        number = lowered.lstrip("#")
        if number.isdigit():
            n = int(number)
            if 1 <= n <= len(entries):
                return entries[n - 1]
            raise RunReferenceError(
                f"There is no run #{n}; runs are numbered #1 to #{len(entries)}."
            )
        for entry in entries:
            if entry.key == text:
                return entry
        matches = [e for e in entries if e.name.lower() == lowered]
        if not matches:
            matches = [e for e in entries if lowered in e.name.lower() or lowered in e.key.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RunReferenceError(f"No run matches '{text}'. Use list_runs to find run numbers.")
        listed = ", ".join(f"{m.ref} {m.name}" for m in matches[:8])
        raise RunReferenceError(
            f"'{text}' matches {len(matches)} runs ({listed}{', ...' if len(matches) > 8 else ''})."
        )

    def load(self, entry: CatalogueEntry) -> tuple[ExperimentInput, ExperimentAnalysis]:
        """Samples and analysis of a run (cached; analysed with the run's variable rules)."""
        with self._lock:
            if entry.key in self._analyses:
                self._analyses.move_to_end(entry.key)
                return self._analyses[entry.key]
        conn = data_service.connect_cache()
        try:
            row = conn.execute(
                "SELECT samples_json FROM runs WHERE key = ?", (entry.key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RunReferenceError(f"Run {entry.ref} is no longer in the database.")
        times, channels = columns_from_rows(json.loads(row["samples_json"]))
        start = times[0] if times else 0.0
        rules = [
            LimitRule(
                r["channel"],
                NaN if r["lo"] is None else r["lo"],
                NaN if r["hi"] is None else r["hi"],
                r["name"],
            )
            for r in annotations_service.list_rules(entry.key)
        ]
        data = ExperimentInput([t - start for t in times], channels, rules)
        result = (data, analyse_experiment(data))
        with self._lock:
            self._analyses[entry.key] = result
            while len(self._analyses) > self._cache_size:
                self._analyses.popitem(last=False)
        return result

    def manifest(self, entry: CatalogueEntry) -> dict | None:
        conn = data_service.connect_cache()
        try:
            row = conn.execute(
                "SELECT manifest_json FROM runs WHERE key = ?", (entry.key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["manifest_json"]:
            return None
        try:
            return json.loads(row["manifest_json"])
        except ValueError:
            return None

    def export_run(
        self, entry: CatalogueEntry, with_samples: bool = False, title: str | None = None
    ) -> run_export.RunExport:
        """Everything run_export needs for this run (with_samples: the stored rows, for a bundle)."""
        from app.api import control_settings

        data, a = self.load(entry)
        manifest = self.manifest(entry)
        states = control_settings.state_meta(control_settings.load())
        described = {c["name"]: c for c in (manifest or {}).get("channels", []) if "name" in c}
        labels, units = {}, {}
        for name in data.channels:
            meta = described.get(name) or {}
            fallback = states.get(name, {})
            labels[name] = meta.get("label") or fallback.get("label") or name
            units[name] = meta.get("unit") or fallback.get("unit") or ""
        summary = samples = None
        if with_samples:
            conn = data_service.connect_cache()
            try:
                row = conn.execute(
                    "SELECT summary_json, samples_json FROM runs WHERE key = ?", (entry.key,)
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                summary = json.loads(row["summary_json"] or "{}")
                samples = json.loads(row["samples_json"] or "[]")
        return run_export.RunExport(
            title=title or (f"{entry.ref} {entry.name}" if entry.number else entry.name),
            start_epoch=entry.start_epoch,
            times=data.times,
            channels=data.channels,
            analysis=a,
            manifest=manifest,
            labels=labels,
            units=units,
            chamber=entry.chamber or "",
            annotations=annotations_service.list_annotations(entry.key),
            rules=annotations_service.list_rules(entry.key),
            summary=summary,
            samples=samples,
        )


def _entry(number: int, r) -> CatalogueEntry:
    return CatalogueEntry(
        number=number,
        key=r["key"],
        name=r["id"],
        group=r["group_name"],
        source=r["source"],
        chamber=r["chamber"],
        test_profile=r["test_profile"],
        start_time=r["start_time"] or "",
        end_time=r["end_time"] or "",
        start_epoch=_start_epoch(r),
        samples=r["samples"],
        duration_s=r["duration_s"],
        mae=r["mae"],
        cost=r["cost"],
        tail_mae=r["tail_mae"],
        overshoot=r["overshoot"],
        settle_time_s=r["settle_time_s"],
    )
