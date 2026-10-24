"""Analyst tools: read-only questions about recorded runs.

All numbers come from the run database and app.core.analysis (the port of
control2-client's Analysis Core), never from the model. Results are kept
small: an LLM reads them. Nothing here reaches a chamber.
"""

import csv
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

    def export_data(
        self,
        run: str,
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
    ) -> dict:
        entry = self.catalogue.resolve(run)
        data, _ = self.catalogue.load(entry)
        names = [c for c in (channels or sorted(data.channels)) if c in data.channels]
        lo = -math.inf if from_s is None else float(from_s)
        hi = math.inf if to_s is None else float(to_s)
        reports = common.REPORTS_DIR
        reports.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in entry.name)[:60]
        path = reports / f"run{entry.number}_{safe}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        rows = 0
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_s", *names])
            for i, t in enumerate(data.times):
                if lo <= t <= hi:
                    writer.writerow(
                        [
                            f"{t:.3f}",
                            *(
                                "" if math.isnan(data.channels[c][i]) else data.channels[c][i]
                                for c in names
                            ),
                        ]
                    )
                    rows += 1
        return {"run": entry.ref, "file": str(path), "rows": rows, "channels": names}

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
        from app.services import data_service

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
