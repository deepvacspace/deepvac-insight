"""Saves every chamber experiment as a run."""

import contextlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import app.common as common

log = logging.getLogger(__name__)

MIN_RUN_DURATION_S = 60.0
JOURNAL_PERIOD_S = 5.0
PENDING_JOB_MAX_AGE_S = 600.0
JOURNAL_VERSION = 1

STATUS_WORK, STATUS_FINISH, STATUS_ALARM = 2, 3, 4


def journal_dir() -> Path:
    return common.DATA_DIR / "journal"


_announced: dict[str, dict] = {}
_announced_lock = threading.Lock()


def announce_job(
    chamber: str,
    label: str,
    kind: str = "program",
    test_profile: str | None = None,
    details: dict | None = None,
) -> None:
    """Announces the job about to start on a chamber."""
    with _announced_lock:
        _announced[chamber] = {
            "label": label,
            "kind": kind,
            "test_profile": test_profile,
            "details": details or {},
            "at": time.time(),
        }


def _take_announced(chamber: str) -> dict | None:
    with _announced_lock:
        return _announced.pop(chamber, None)


def plc_working(sample: dict) -> bool:
    return sample.get("status_condition") == STATUS_WORK


def _outcome(end_status) -> str:
    if end_status == STATUS_ALARM:
        return "alarm"
    if end_status == STATUS_FINISH:
        return "finished"
    return "stopped"


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "chamber"


def _run_name(start: float, label: str, suffix: str = "") -> str:
    return f"{datetime.fromtimestamp(start):%Y-%m-%d %H:%M} {label}{suffix}"


@dataclass
class _Job:
    chamber: str
    kind: str
    label: str
    test_profile: str | None
    start: float
    journal: Path
    manifest: dict
    samples: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


class ExperimentRecorder:
    def __init__(
        self,
        root: Path | None = None,
        save=None,
        configuration=None,
        channels=None,
        on_saved=None,
        on_failed=None,
    ):
        """Experiment recorder with its save, configuration and channel callbacks."""
        self._root = root
        self._save = save or _save_to_database
        self._configuration = configuration
        self._channels = channels
        self._on_saved = on_saved
        self._on_failed = on_failed
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._threads: list[threading.Thread] = []
        self._last_flush = time.monotonic()

    def set_next_job(
        self,
        chamber: str,
        label: str,
        kind: str = "program",
        test_profile: str | None = None,
        details: dict | None = None,
    ) -> None:
        """Sets the job about to start on a chamber."""
        announce_job(chamber, label, kind, test_profile, details)

    def add(self, chamber: str, sample: dict, working: bool) -> None:
        """Adds a sample of a chamber."""
        stamp = sample.get("timestamp")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            return
        finished = None
        with self._lock:
            job = self._jobs.get(chamber)
            if working:
                if job is None:
                    job = self._begin(chamber, float(stamp))
                if not job.samples or stamp > job.samples[-1]["timestamp"]:
                    job.samples.append(sample)
                    job.pending.append(sample)
            elif job is not None:
                finished = self._jobs.pop(chamber)
                end_status = sample.get("status_condition")
            if time.monotonic() - self._last_flush >= JOURNAL_PERIOD_S:
                self._flush_locked()
        if finished is not None:
            self._finish(finished, end_status, float(stamp))

    def set_working(self, chamber: str, working: bool, end_status=None) -> None:
        """Sets whether a job runs on a chamber."""
        if working:
            return
        with self._lock:
            job = self._jobs.pop(chamber, None)
        if job is not None:
            end = job.samples[-1]["timestamp"] if job.samples else time.time()
            self._finish(job, end_status, end)

    def running(self, chamber: str) -> dict | None:
        """Returns the job being recorded on a chamber."""
        with self._lock:
            job = self._jobs.get(chamber)
            if job is None:
                return None
            return {
                "label": job.label,
                "kind": job.kind,
                "start": job.start,
                "samples": len(job.samples),
            }

    def flush(self) -> None:
        """Journals the samples not yet written."""
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        """Journals the running jobs and waits for pending saves."""
        self.flush()
        self.wait_saved(10.0)

    def wait_saved(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))

    def recover_interrupted(self) -> int:
        """Saves the jobs left in journals by an application that stopped while they ran."""
        folder = self._journal_dir()
        if not folder.exists():
            return 0
        started = 0
        with self._lock:
            open_journals = {job.journal for job in self._jobs.values()}
        for path in sorted(folder.glob("*.jsonl")):
            if path in open_journals:
                continue
            try:
                header, samples = _read_journal(path)
            except Exception as exc:
                log.warning("experiment journal %s not readable (%s); kept as .bad", path, exc)
                with contextlib.suppress(OSError):
                    path.replace(path.with_suffix(".bad"))
                continue
            if len(samples) < 2 or (
                samples[-1]["timestamp"] - samples[0]["timestamp"] < MIN_RUN_DURATION_S
            ):
                path.unlink(missing_ok=True)
                continue
            name = _run_name(
                samples[0]["timestamp"], header.get("label", "Chamber job"), " (interrupted)"
            )
            manifest = dict(header.get("manifest") or {})
            manifest["name"] = name
            configuration = dict(manifest.get("configuration") or {})
            configuration["note"] = (
                "the application stopped while the job ran; saved from the journal at the next start"
            )
            manifest["configuration"] = configuration
            self._save_async(
                name, samples, header.get("chamber", ""), header.get("test_profile"), manifest, path
            )
            started += 1
        return started

    def _journal_dir(self) -> Path:
        return self._root or journal_dir()

    def _begin(self, chamber: str, start: float) -> _Job:
        announced = _take_announced(chamber)
        if announced and start - announced["at"] <= PENDING_JOB_MAX_AGE_S:
            kind, label = announced["kind"], announced["label"]
            test_profile, details = announced["test_profile"], announced["details"]
        else:
            kind, label, test_profile, details = "external", "Chamber job", None, {}
        configuration = {}
        if self._configuration:
            try:
                configuration = dict(self._configuration(chamber) or {})
            except Exception:
                log.exception("configuration snapshot of %s", chamber)
        job_info = {"kind": kind, "label": label, "test_profile": test_profile or ""}
        if details:
            job_info["details"] = details
        configuration.update(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "captured_at_execution": True,
                "job": job_info,
                "chamber": {"name": chamber},
            }
        )
        if kind == "external":
            configuration.setdefault("note", "job not started from this application")
        channels = []
        if self._channels:
            with contextlib.suppress(Exception):
                channels = list(self._channels(chamber) or [])
        manifest = {
            "chamber": chamber,
            "app_name": "Deepvac Insight",
            "configuration": configuration,
            "channels": channels,
        }
        folder = self._journal_dir()
        folder.mkdir(parents=True, exist_ok=True)
        journal = folder / f"{_safe(chamber)}-{int(start * 1000)}.jsonl"
        header = {
            "journal": JOURNAL_VERSION,
            "chamber": chamber,
            "kind": kind,
            "label": label,
            "test_profile": test_profile,
            "start": start,
            "manifest": manifest,
        }
        journal.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        job = _Job(chamber, kind, label, test_profile, start, journal, manifest)
        job.events.append(
            _event(start, "job_start", f"Job started: {label}", {"kind": kind, "label": label})
        )
        self._jobs[chamber] = job
        return job

    def _flush_locked(self) -> None:
        self._last_flush = time.monotonic()
        for job in self._jobs.values():
            _append_journal(job)

    def _finish(self, job: _Job, end_status, end: float) -> None:
        with self._lock:
            _append_journal(job)
        outcome = _outcome(end_status)
        job.events.append(_event(end, "job_end", f"Job {outcome}", {"outcome": outcome}))
        samples = job.samples
        if (
            len(samples) < 2
            or samples[-1]["timestamp"] - samples[0]["timestamp"] < MIN_RUN_DURATION_S
        ):
            job.journal.unlink(missing_ok=True)
            return
        name = _run_name(job.start, job.label, " (alarm)" if outcome == "alarm" else "")
        manifest = dict(job.manifest)
        manifest["name"] = name
        manifest["events"] = job.events
        with contextlib.suppress(Exception):
            from app.services import data_service, settings_service

            details = data_service.clean_metadata(settings_service.load_run_metadata())
            if any(details.values()):
                manifest["metadata"] = details
        self._save_async(name, samples, job.chamber, job.test_profile, manifest, job.journal)

    def _save_async(self, name, samples, chamber, test_profile, manifest, journal: Path) -> None:
        def run():
            try:
                key = self._save(name, samples, chamber, test_profile, manifest)
            except Exception as exc:
                log.exception("experiment %s not saved", name)
                if self._on_failed:
                    self._on_failed(f"{name}: {exc}")
                return
            journal.unlink(missing_ok=True)
            if self._on_saved:
                self._on_saved(key, name)

        thread = threading.Thread(target=run, name="experiment-save", daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()


def _event(t: float, etype: str, message: str, data: dict | None = None) -> dict:
    return {"t": t, "type": etype, "source": "app", "message": message, "data": data or {}}


def _append_journal(job: _Job) -> None:
    if not job.pending:
        return
    try:
        with job.journal.open("a", encoding="utf-8") as f:
            for sample in job.pending:
                f.write(json.dumps(sample, allow_nan=True) + "\n")
        job.pending = []
    except OSError:
        log.exception("experiment journal %s", job.journal)


def _read_journal(path: Path) -> tuple[dict, list[dict]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError("empty journal")
    header = json.loads(lines[0])
    samples = []
    for line in lines[1:]:
        try:
            samples.append(json.loads(line))
        except ValueError:
            break
    return header, samples


def _save_to_database(name, samples, chamber, test_profile, manifest) -> str:
    from app.services import data_service

    return data_service.save_monitoring_session(
        name, samples, chamber=chamber or None, test_profile=test_profile, manifest=manifest
    )["key"]
