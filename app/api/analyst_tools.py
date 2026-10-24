"""Analyst tools: read-only questions about recorded runs.

All numbers come from the run database and app.core.analysis (the port of
control2-client's Analysis Core), never from the model. Results are kept
small: an LLM reads them. Nothing here reaches a chamber.

The export_* tools write files into Insight's reports folder, with DeepVac
Control's export options (app/services/run_export.py): chart images, data,
comparison CSV, experiment reports (PDF / HTML) and bundles. They return the
file's path and a file:// link the chat can open.
"""

import math
from datetime import datetime

import app.common as common
from app.api import control_settings
from app.api.runs import CatalogueEntry, RunCatalogue
from app.core.analysis import anomalies as anomaly_mod
from app.core.analysis.comparison import compare_configs, compare_kpis, match_steps
from app.core.analysis.kpis import ControlKpis, KpiOptions, compute_kpis
from app.core.analysis.runanalysis import kpis_to_json, tracks_setpoint
from app.core.analysis.seriesstats import compute_stats
from app.services import data_service, day_recorder, run_export

MAX_LIST = 50
MAX_STAGES = 30
MAX_ANOMALIES = 25

RANK_METRICS = {
    "mae": ("mae", True),
    "tail_mae": ("tail_mae", True),
    "overshoot": ("overshoot", True),
    "settling_time": ("settle_time_s", True),
    "cost": ("cost", True),
    "duration": ("duration_s", False),
}


def n(value, digits: int = 5):
    """JSON-friendly number: NaN / inf -> None, floats to `digits` significant digits."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    value = float(value)
    if not math.isfinite(value):
        return None
    return float(f"{value:.{digits}g}")


def _local(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _time_range(start: str, end: str) -> tuple[float, float]:
    """Local times "YYYY-MM-DD[ HH:MM[:SS]]"; a bare day is the whole day. Empty: today so far."""

    def parse(text, end_of_day):
        text = text.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                value = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt == "%Y-%m-%d" and end_of_day:
                value = value.replace(hour=23, minute=59, second=59, microsecond=999999)
            return value.timestamp()
        raise ValueError(f"'{text}': use YYYY-MM-DD or YYYY-MM-DD HH:MM")

    if not start:
        now = datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), now.timestamp()
    t0 = parse(start, False)
    t1 = parse(end, True) if end else parse(start, True)
    if t1 < t0:
        raise ValueError("end is before start")
    return t0, t1


STATUS_NAMES = {1: "idle", 2: "work", 3: "finish", 4: "alarm"}


def _changes(times, column, is_status: bool, limit: int = 20) -> list[dict]:
    """When a slowly changing channel (status, setpoint) took a new value."""
    changes, last = [], None
    for t, v in zip(times, column, strict=True):
        if v != v:
            continue
        if last is None or abs(v - last) > 1e-6:
            if last is not None:
                to = STATUS_NAMES.get(int(v), v) if is_status else n(float(v), 4)
                changes.append({"at": _local(t), "to": to})
            last = v
    return changes[:limit] + (
        [{"note": f"{len(changes) - limit} more"}] if len(changes) > limit else []
    )


def _header(entry: CatalogueEntry) -> dict:
    return {
        "run": entry.ref,
        "name": entry.name,
        "group": entry.group,
        "source": entry.source,
        "chamber": entry.chamber,
        "test_profile": entry.test_profile,
        "start": entry.start_time,
        "end": entry.end_time,
        "duration_min": n(entry.duration_s / 60.0, 4) if entry.duration_s else None,
        "samples": entry.samples,
    }


def _kpis(k: ControlKpis) -> dict:
    data = kpis_to_json(k)
    keep = (
        "mae",
        "rmse",
        "bias",
        "max_abs_error",
        "overshoot",
        "undershoot",
        "rise_time_s",
        "fall_time_s",
        "time_to_near_s",
        "settling_time_s",
        "steady_state_error",
        "tail_mae",
        "tail_std",
        "final_error",
        "min",
        "max",
        "end",
    )
    return {key: n(data[key]) for key in keep if data.get(key) is not None}


class AnalystTools:
    def __init__(self, catalogue: RunCatalogue | None = None):
        self.catalogue = catalogue or RunCatalogue()

    # ------------------------------------------------------------ runs

    def list_runs(
        self, search: str = "", group: str = "", limit: int = 20, newest_first: bool = True
    ) -> dict:
        entries = self.catalogue.entries()
        selected = entries
        if group:
            selected = [e for e in selected if e.group.lower() == group.lower()]
        if search:
            s = search.lower()
            selected = [
                e
                for e in selected
                if s in e.name.lower()
                or s in e.key.lower()
                or s in (e.test_profile or "").lower()
                or s in (e.chamber or "").lower()
            ]
        ordered = list(reversed(selected)) if newest_first else selected
        limit = max(1, min(int(limit), MAX_LIST))
        groups: dict[str, int] = {}
        for e in entries:
            groups[e.group] = groups.get(e.group, 0) + 1
        return {
            "total_runs": len(entries),
            "matching": len(selected),
            "groups": groups,
            "runs": [
                {
                    "run": e.ref,
                    "name": e.name,
                    "group": e.group,
                    "start": e.start_time,
                    "duration_min": n(e.duration_s / 60.0, 4) if e.duration_s else None,
                    "mae": n(e.mae, 4),
                    "tail_mae": n(e.tail_mae, 4),
                    "overshoot": n(e.overshoot, 4),
                    "settle_time_s": n(e.settle_time_s, 4),
                }
                for e in ordered[:limit]
            ],
        }

    def rank_runs(
        self, metric: str = "tail_mae", group: str = "", limit: int = 10, best_first: bool = True
    ) -> dict:
        if metric not in RANK_METRICS:
            raise ValueError(f"metric must be one of {', '.join(RANK_METRICS)}")
        attribute, lower_is_better = RANK_METRICS[metric]
        entries = [e for e in self.catalogue.entries() if getattr(e, attribute) is not None]
        if group:
            entries = [e for e in entries if e.group.lower() == group.lower()]
        reverse = (not lower_is_better) == best_first
        ranked = sorted(entries, key=lambda e: getattr(e, attribute), reverse=reverse)
        limit = max(1, min(int(limit), MAX_LIST))
        return {
            "metric": metric,
            "order": "best first" if best_first else "worst first",
            "ranked": len(ranked),
            "runs": [
                {
                    "rank": i + 1,
                    "run": e.ref,
                    "name": e.name,
                    "group": e.group,
                    metric: n(getattr(e, attribute), 4),
                }
                for i, e in enumerate(ranked[:limit])
            ],
        }

    def get_run_summary(self, run: str) -> dict:
        entry = self.catalogue.resolve(run)
        data, a = self.catalogue.load(entry)
        result = {"header": _header(entry)}
        if not a.has_control:
            result["note"] = (
                "This run has no temperature and setpoint channels, so it has no control analysis."
            )
        c = a.config
        result["configuration"] = {
            "setpoints_c": [n(s, 4) for s in c.setpoints],
            "kp": n(c.kp),
            "ki": n(c.ki),
            "kd": n(c.kd),
            "gain_sets": c.gain_sets,
            "stages": c.stages,
            "steps": c.steps,
            "ramps": c.ramps,
            "holds": c.holds,
            "max_ramp_rate_c_per_min": n(c.max_ramp_rate_per_min, 4),
            "sample_period_s": n(c.sample_period_s, 4),
            "program_intervals": c.program_intervals,
            "cycles": c.cycles,
        }
        if a.has_control:
            # with a ramp the whole-run MAE measures tracking; without one it mostly measures the distance
            # to the first setpoint, so the tail MAE is the headline
            result["kpis"] = {
                "headline": "mae" if a.run.has_ramp() else "tail_mae",
                **_kpis(a.run.overall),
                "overshoot_worst_step": n(a.run.overshoot),
                "settling_time_last_step_s": n(a.run.settling_time_s),
                "cost": n(a.run.cost),
            }
        result["saturation"] = {
            "fraction": n(a.saturation.fraction, 3),
            "longest_s": n(a.saturation.longest_s, 4),
        }
        result["pid_shares"] = {
            "p": n(a.pid.share_p, 3),
            "i": n(a.pid.share_i, 3),
            "d": n(a.pid.share_d, 3),
            "dominant": a.pid.dominant() or None,
        }
        result["anomalies"] = {
            "critical": a.anomaly_count(anomaly_mod.CRITICAL),
            "warning_or_worse": a.anomaly_count(anomaly_mod.WARNING),
            "total": len(a.anomalies),
            "top": [x.detail for x in a.anomalies if x.severity != anomaly_mod.INFO][:5],
        }
        result["stages"] = [
            {
                "stage": i + 1,
                "kind": s.segment.kind,
                "start_s": n(s.segment.start, 5),
                "end_s": n(s.segment.end, 5),
                "reference_c": [n(s.segment.reference_start, 4), n(s.segment.reference_end, 4)],
            }
            for i, s in enumerate(a.run.stages[:MAX_STAGES])
        ]
        return result

    def list_channels(self, run: str) -> dict:
        entry = self.catalogue.resolve(run)
        data, _ = self.catalogue.load(entry)
        manifest = self.catalogue.manifest(entry) or {}
        described = {ch["name"]: ch for ch in manifest.get("channels", []) if "name" in ch}
        states = control_settings.state_meta(control_settings.load())
        channels = []
        for name in sorted(data.channels):
            meta = described.get(name) or {}
            fallback = states.get(name, {})
            channels.append(
                {
                    "name": name,
                    "label": meta.get("label") or fallback.get("label") or "",
                    "unit": meta.get("unit") or fallback.get("unit") or "",
                    "role": meta.get("role") or "",
                }
            )
        return {"run": entry.ref, "channels": channels}

    def get_statistics(
        self,
        run: str,
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
    ) -> dict:
        entry = self.catalogue.resolve(run)
        data, _ = self.catalogue.load(entry)
        names = channels or [c for c in ("temp", "temp_ref", "temp_u") if c in data.channels]
        start = math.nan if from_s is None else float(from_s)
        end = math.nan if to_s is None else float(to_s)
        result, missing = {}, []
        for name in names:
            if name not in data.channels:
                missing.append(name)
                continue
            s = compute_stats(data.times, data.channels[name], start, end)
            result[name] = {
                "n": s.n,
                "missing": s.missing,
                "min": n(s.min),
                "max": n(s.max),
                "mean": n(s.mean),
                "std": n(s.std),
                "rms": n(s.rms),
                "p95": n(s.p95),
                "slope_per_min": n(
                    s.slope_per_s * 60.0 if math.isfinite(s.slope_per_s) else math.nan
                ),
            }
        out = {"run": entry.ref, "from_s": from_s, "to_s": to_s, "statistics": result}
        if missing:
            out["unknown_channels"] = missing
        return out

    def get_kpis(self, run: str, from_s: float | None = None, to_s: float | None = None) -> dict:
        entry = self.catalogue.resolve(run)
        data, a = self.catalogue.load(entry)
        if not a.has_control:
            return {
                "run": entry.ref,
                "note": "No temperature and setpoint channels: no control KPIs.",
            }
        if from_s is None and to_s is None:
            return {
                "run": entry.ref,
                "range": "whole run",
                "kpis": _kpis(a.run.overall),
                "cost": n(a.run.cost),
            }
        lo = -math.inf if from_s is None else float(from_s)
        hi = math.inf if to_s is None else float(to_s)
        idx = [i for i, t in enumerate(data.times) if lo <= t <= hi]
        if not idx:
            return {"run": entry.ref, "note": "No samples in that time range."}
        sl = slice(idx[0], idx[-1] + 1)
        k = compute_kpis(
            data.times[sl], data.channels["temp"][sl], data.channels["temp_ref"][sl], KpiOptions()
        )
        return {"run": entry.ref, "range": {"from_s": from_s, "to_s": to_s}, "kpis": _kpis(k)}

    def get_stages(self, run: str) -> dict:
        entry = self.catalogue.resolve(run)
        _, a = self.catalogue.load(entry)
        stages = []
        for i, (s, d) in enumerate(zip(a.run.stages, a.stage_details, strict=True)):
            k = s.kpis
            item = {
                "stage": i + 1,
                "kind": s.segment.kind,
                "start_s": n(s.segment.start),
                "end_s": n(s.segment.end),
                "reference_start_c": n(s.segment.reference_start, 4),
                "reference_end_c": n(s.segment.reference_end, 4),
                "mae" if tracks_setpoint(s.segment.kind) else "tail_mae": n(
                    k.mae if tracks_setpoint(s.segment.kind) else k.tail_mae, 4
                ),
                "overshoot": n(k.overshoot, 4),
                "settling_time_s": n(k.settling_time_s),
                "time_to_steady_s": n(d.time_to_steady_s),
                "steady_fraction": n(d.steady_fraction, 3),
                "saturated_fraction": n(d.saturation.fraction, 3),
                "kp": n(d.kp),
                "ki": n(d.ki),
                "kd": n(d.kd),
            }
            if s.segment.kind == "ramp":
                item["reference_rate_c_per_min"] = n(d.reference_rate_per_min, 4)
                item["tracking_lag_s"] = n(d.tracking_lag_s, 4)
            if s.segment.interval >= 0:
                item["program_interval"] = s.segment.interval
            stages.append(item)
        return {"run": entry.ref, "stages": stages[:MAX_STAGES], "total_stages": len(stages)}

    def get_anomalies(self, run: str, min_severity: str = "info") -> dict:
        if min_severity not in anomaly_mod.SEVERITY_RANK:
            raise ValueError("min_severity must be info, warning or critical")
        entry = self.catalogue.resolve(run)
        _, a = self.catalogue.load(entry)
        rank = anomaly_mod.SEVERITY_RANK[min_severity]
        found = [x for x in a.anomalies if anomaly_mod.SEVERITY_RANK[x.severity] >= rank]
        return {
            "run": entry.ref,
            "count": len(found),
            "anomalies": [
                {
                    "kind": x.kind,
                    "severity": x.severity,
                    "channel": x.channel,
                    "start_s": n(x.start),
                    "end_s": n(x.end),
                    "detail": x.detail,
                }
                for x in found[:MAX_ANOMALIES]
            ],
        }

    def compare_runs(self, runs: list[str]) -> dict:
        if len(runs) < 2:
            raise ValueError("Give at least two runs; the first is the reference.")
        entries = [self.catalogue.resolve(r) for r in runs[:6]]
        analyses = [self.catalogue.load(e)[1] for e in entries]
        kpi_rows = []
        for row in compare_kpis(analyses):
            values = [n(v, 4) for v in row.values]
            if all(v is None for v in values):
                continue
            item = {"kpi": row.label, "unit": row.unit, "better": row.better, "values": values}
            ref = row.values[0]
            if math.isfinite(ref):
                item["vs_reference"] = [
                    None if not math.isfinite(v) else n(v - ref, 4) for v in row.values[1:]
                ]
            kpi_rows.append(item)
        config_rows = [
            {"setting": r.label, "values": r.values}
            for r in compare_configs([a.config for a in analyses])
            if r.differs
        ]
        steps = []
        for k, match in enumerate(match_steps(analyses)[:10]):
            steps.append(
                {
                    "step": k + 1,
                    "per_run": [
                        None
                        if idx < 0
                        else {
                            "to_c": n(a.run.stages[idx].segment.reference_end, 4),
                            "overshoot": n(a.run.stages[idx].kpis.overshoot, 4),
                            "settling_time_s": n(a.run.stages[idx].kpis.settling_time_s),
                        }
                        for a, idx in zip(analyses, match, strict=True)
                    ],
                }
            )
        return {
            "runs": [f"{e.ref} {e.name}" for e in entries],
            "reference": entries[0].ref,
            "kpis": kpi_rows,
            "differing_configuration": config_rows,
            "step_responses": steps,
        }

    def get_events(self, run: str, limit: int = 40) -> dict:
        entry = self.catalogue.resolve(run)
        manifest = self.catalogue.manifest(entry)
        if not manifest or not manifest.get("events"):
            return {
                "run": entry.ref,
                "note": "This run has no recorded events (only runs recorded by DeepVac Control carry them).",
            }
        start = entry.start_epoch if math.isfinite(entry.start_epoch) else None
        events = []
        for e in manifest["events"][: max(1, min(int(limit), 200))]:
            t = e.get("t")  # epoch s
            events.append(
                {
                    "at_s": n(t - start) if start is not None and t is not None else None,
                    "type": e.get("type"),
                    "message": e.get("message"),
                }
            )
        return {"run": entry.ref, "count": len(manifest["events"]), "events": events}

    # ------------------------------------------------------------ exports (files)

    def _export_run(
        self, entry: CatalogueEntry, with_samples: bool = False
    ) -> run_export.RunExport:
        return self.catalogue.export_run(entry, with_samples)

    @staticmethod
    def _export_path(entry: CatalogueEntry, what: str, extension: str):
        stem = f"run{entry.number}_{run_export.safe_name(entry.name)}_{what}_{datetime.now():%Y%m%d_%H%M%S}"
        path = common.REPORTS_DIR / f"{stem}.{extension}"
        copy = 2
        while path.exists():  # two exports in the same second
            path = common.REPORTS_DIR / f"{stem}_{copy}.{extension}"
            copy += 1
        return path

    @staticmethod
    def _file(path) -> dict:
        return {"file": str(path), "link": path.resolve().as_uri(), "bytes": path.stat().st_size}

    def export_data(
        self,
        run: str,
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
        format: str = "csv",
    ) -> dict:
        if format not in run_export.DATA_FORMATS:
            raise ValueError("format must be csv or txt (tab separated)")
        entry = self.catalogue.resolve(run)
        target = self._export_run(entry)
        names = [c for c in (channels or list(target.channels)) if c in target.channels]
        path = self._export_path(entry, "data", format)
        rows = run_export.write_data(target, path, names, from_s, to_s, format == "txt")
        return {"run": entry.ref, **self._file(path), "rows": rows, "channels": names}

    def export_chart(
        self,
        run: str,
        kind: str = "temperature",
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
        format: str = "png",
        compare_with: list[str] | None = None,
    ) -> dict:
        fmt = format.lower()
        if fmt not in run_export.IMAGE_FORMATS:
            raise ValueError("format must be png, bmp or jpg")
        entry = self.catalogue.resolve(run)
        target = self._export_run(entry)
        if compare_with:
            others = [self.catalogue.resolve(r) for r in compare_with[:7]]
            channel = (channels or ["temp"])[0]
            image, caption = run_export.comparison_chart(
                [target, *(self._export_run(e) for e in others)], channel
            )
            drawn, refs = [channel], [entry.ref, *(e.ref for e in others)]
        else:
            image, caption, drawn = run_export.run_chart(target, kind, channels, from_s, to_s)
            refs = [entry.ref]
        path = run_export.save_image(
            image, self._export_path(entry, "compare" if compare_with else "chart", fmt), fmt
        )
        return {
            "runs": refs,
            "caption": caption,
            "channels": drawn,
            **self._file(path),
            "size_px": [image.width(), image.height()],
        }

    def export_report(
        self, run: str, format: str = "pdf", compare_with: list[str] | None = None
    ) -> dict:
        fmt = format.lower()
        if fmt not in run_export.REPORT_FORMATS:
            raise ValueError("format must be pdf or html")
        entry = self.catalogue.resolve(run)
        others = [self.catalogue.resolve(r) for r in (compare_with or [])[:5]]
        path = run_export.write_report(
            self._export_run(entry),
            self._export_path(entry, "report", fmt),
            fmt,
            [self._export_run(e) for e in others],
        )
        return {
            "run": entry.ref,
            "compared_with": [e.ref for e in others],
            **self._file(path),
            "contents": "summary, experiment details, control KPIs, configuration, charts, stages, steady "
            "states, anomalies, events, notes" + (", comparison" if others else ""),
        }

    def export_comparison(self, runs: list[str], channel: str = "temp") -> dict:
        if len(runs) < 2:
            raise ValueError("Give at least two runs.")
        entries = [self.catalogue.resolve(r) for r in runs[:20]]
        targets = [self._export_run(e) for e in entries]
        path = self._export_path(entries[0], "comparison", "csv")
        run_export.write_comparison_csv(targets, path, channel)
        return {"runs": [e.ref for e in entries], "channel": channel, **self._file(path)}

    def export_bundle(self, run: str) -> dict:
        entry = self.catalogue.resolve(run)
        path = run_export.write_bundle(
            self._export_run(entry, with_samples=True), self._export_path(entry, "bundle", "json")
        )
        return {"run": entry.ref, **self._file(path)}

    # ------------------------------------------------------------ full-day recordings

    def list_recordings(self, chamber: str = "") -> dict:
        days = day_recorder.list_days(chamber or None)
        if chamber and not days:
            known = sorted({d["chamber"] for d in day_recorder.list_days()})
            raise ValueError(
                f"No recordings of '{chamber}'. Recorded chambers: {', '.join(known) or 'none'}."
            )
        import pyarrow.parquet as pq

        result = []
        for d in days[-60:]:
            try:
                rows = pq.ParquetFile(d["path"]).metadata.num_rows
            except Exception:
                rows = None
            result.append(
                {
                    "chamber": d["chamber"],
                    "day": d["day"],
                    "samples": rows,
                    "mb": n(d["bytes"] / 1e6, 3),
                }
            )
        return {"recordings": result, "total_days": len(days)}

    def get_recording(
        self, chamber: str = "", start: str = "", end: str = "", channels: list[str] | None = None
    ) -> dict:
        """What a chamber did between two local times (or a whole day), from its day recordings."""
        chambers = sorted({d["chamber"] for d in day_recorder.list_days()})
        if not chamber:
            if len(chambers) != 1:
                raise ValueError(
                    f"Name the chamber: recorded ones are {', '.join(chambers) or 'none'}."
                )
            chamber = chambers[0]
        t0, t1 = _time_range(start, end)
        times, values = day_recorder.read_range(chamber, t0, t1, None)
        if not len(times):
            raise ValueError(
                f"No samples of '{chamber}' between {_local(t0)} and {_local(t1)}; see list_recordings."
            )
        wanted = channels or [
            c for c in ("temp", "temp_ref", "temp_u", "status_condition") if c in values
        ]
        wanted = wanted or list(values)[:8]
        steps = [b - a for a, b in zip(times[:-1], times[1:], strict=True)]
        gaps = [(a, b) for a, b in zip(times[:-1], times[1:], strict=True) if b - a > 30]
        result = {
            "chamber": chamber,
            "from": _local(times[0]),
            "to": _local(times[-1]),
            "samples": len(times),
            "sample_period_s": n(float(sorted(steps)[len(steps) // 2]), 3) if steps else None,
            "gaps": [
                {"from": _local(a), "to": _local(b), "minutes": n((b - a) / 60, 3)}
                for a, b in gaps[:10]
            ],
            "channels": {},
        }
        missing = [c for c in wanted if c not in values]
        for name in wanted:
            if name not in values:
                continue
            column = values[name]
            finite = [v for v in column if v == v]
            result["channels"][name] = {
                "min": n(min(finite)) if finite else None,
                "max": n(max(finite)) if finite else None,
                "mean": n(sum(finite) / len(finite)) if finite else None,
                "first": n(finite[0]) if finite else None,
                "last": n(finite[-1]) if finite else None,
                "missing": len(column) - len(finite),
            }
        for name, label in (
            ("status_condition", "status_changes"),
            ("temp_ref", "setpoint_changes"),
        ):
            if name in values:
                result[label] = _changes(times, values[name], name == "status_condition")
        if missing:
            result["unknown_channels"] = missing
        return result

    def simulate_pid(
        self,
        kp: float,
        ki: float,
        kd: float,
        target_c: float,
        start_c: float = 27.0,
        duration_min: float = 20.0,
    ) -> dict:
        """Closed-loop GRU digital twin (Insight's simulator): KPIs of a PID setting, nothing sent to a chamber."""

        result = data_service.simulate_gru_run(
            {
                "kp": kp,
                "ki": ki,
                "kd": kd,
                "start_temp": start_c,
                "target_temp": target_c,
                "duration_s": float(duration_min) * 60.0,
            }
        )
        metrics = {
            k: n(v) for k, v in (result.get("metrics") or {}).items() if isinstance(v, (int, float))
        }
        points = result.get("points") or []
        final = points[-1]["values"] if points else {}
        return {
            "source": "GRU digital twin (simulation, not a measurement)",
            "inputs": {
                "kp": kp,
                "ki": ki,
                "kd": kd,
                "start_c": start_c,
                "target_c": target_c,
                "duration_min": duration_min,
            },
            "metrics": metrics,
            "final_temp_c": n(final.get("temp"), 4),
        }
