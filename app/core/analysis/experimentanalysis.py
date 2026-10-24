"""Everything known about one recorded experiment (port of core/analysis/experimentanalysis.cpp).

`ExperimentAnalysis.to_json()` is what both apps store in the run database's
summary_json, tagged with ANALYSIS_VERSION.
"""

from dataclasses import dataclass, field

from app.core.analysis import anomalies as anomaly_mod
from app.core.analysis.anomalies import Anomaly, AnomalyOptions, LimitRule
from app.core.analysis.common import NaN, json_number, valid
from app.core.analysis.controlanalysis import (
    PidContribution,
    SaturationOptions,
    SaturationStats,
    analyse_pid_contribution,
    analyse_saturation,
)
from app.core.analysis.kpis import KpiOptions
from app.core.analysis.runanalysis import RunAnalysis, analyse_run
from app.core.analysis.segmentation import HOLD, RAMP, SETPOINT, SegmentationOptions
from app.core.analysis.seriesstats import compute_stats, percentile
from app.core.analysis.steadystate import (
    SteadyInterval,
    SteadyStateOptions,
    detect_steady_states,
    first_steady_time,
    steady_fraction,
)

# Bump when to_json() gains content: stored analyses older than this are recomputed.
# Same number as EXPERIMENT_ANALYSIS_VERSION in control2-client.
ANALYSIS_VERSION = 2


@dataclass
class ExperimentInput:
    """One recorded experiment, channels by state name (temp, temp_ref, temp_u,
    temp_u_p/i/d, temp_kp/ki/kd, interval, cycle_cur, ...). Missing channels
    just skip the analyses needing them."""

    times: list[float]  # elapsed s, ascending
    channels: dict[str, list[float]]
    rules: list[LimitRule] = field(default_factory=list)  # variable rules of the run


@dataclass
class ExperimentOptions:
    kpi: KpiOptions = field(default_factory=KpiOptions)
    segmentation: SegmentationOptions = field(default_factory=SegmentationOptions)
    steady: SteadyStateOptions = field(default_factory=SteadyStateOptions)
    saturation: SaturationOptions = field(default_factory=SaturationOptions)
    anomaly: AnomalyOptions = field(default_factory=AnomalyOptions)


@dataclass
class StageDetail:
    time_to_steady_s: float = NaN  # from the stage start until a steady state begins
    steady_fraction: float = NaN  # share of the stage spent steady
    reference_rate_per_min: float = NaN  # least-squares trend of the reference (ramps)
    value_rate_per_min: float = NaN
    tracking_lag_s: float = NaN  # ramps: mean error / reference rate
    saturation: SaturationStats = field(default_factory=SaturationStats)
    pid: PidContribution = field(default_factory=PidContribution)
    kp: float = NaN  # gains in use (median over the stage)
    ki: float = NaN
    kd: float = NaN


@dataclass
class RunConfig:
    """Configuration of the run, as far as the recorded signals tell it."""

    samples: int = 0
    duration_s: float = NaN
    sample_period_s: float = NaN
    kp: float = NaN  # most used gains
    ki: float = NaN
    kd: float = NaN
    gain_sets: int = 0  # distinct (kp, ki, kd) combinations used
    setpoints: list[float] = field(default_factory=list)  # levels the reference held, in order
    min_reference: float = NaN
    max_reference: float = NaN
    max_ramp_rate_per_min: float = NaN
    stages: int = 0
    steps: int = 0  # setpoint stages
    ramps: int = 0
    holds: int = 0
    program_intervals: int = 0  # highest interval number
    cycles: int = 0  # highest cycle number
    channels: list[str] = field(default_factory=list)


@dataclass
class ExperimentAnalysis:
    has_control: bool = False  # temp and temp_ref present with samples
    run: RunAnalysis = field(default_factory=RunAnalysis)
    stage_details: list[StageDetail] = field(default_factory=list)  # parallel to run.stages
    steady: list[SteadyInterval] = field(default_factory=list)
    saturation: SaturationStats = field(default_factory=SaturationStats)
    pid: PidContribution = field(default_factory=PidContribution)
    anomalies: list[Anomaly] = field(default_factory=list)  # sorted by start
    config: RunConfig = field(default_factory=RunConfig)

    def anomaly_count(self, at_least: str) -> int:
        rank = anomaly_mod.SEVERITY_RANK[at_least]
        return sum(1 for a in self.anomalies if anomaly_mod.SEVERITY_RANK[a.severity] >= rank)

    def to_json(self) -> dict:
        data = self.run.to_json()
        data["analysis_version"] = ANALYSIS_VERSION
        for item, d in zip(data["stages"], self.stage_details, strict=True):
            item.update(
                {
                    "time_to_steady_s": json_number(d.time_to_steady_s),
                    "steady_fraction": json_number(d.steady_fraction),
                    "reference_rate_per_min": json_number(d.reference_rate_per_min),
                    "value_rate_per_min": json_number(d.value_rate_per_min),
                    "tracking_lag_s": json_number(d.tracking_lag_s),
                    "saturation": _saturation_json(d.saturation),
                    "pid": _pid_json(d.pid),
                    "kp": json_number(d.kp),
                    "ki": json_number(d.ki),
                    "kd": json_number(d.kd),
                }
            )
        data["steady_states"] = [
            {
                "start_s": s.start,
                "end_s": s.end,
                "mean": json_number(s.mean),
                "std": json_number(s.std),
                "mean_error": json_number(s.mean_error),
            }
            for s in self.steady
        ]
        data["saturation"] = _saturation_json(self.saturation)
        data["pid"] = _pid_json(self.pid)
        data["anomalies"] = [a.to_json() for a in self.anomalies]
        c = self.config
        data["config"] = {
            "samples": c.samples,
            "duration_s": json_number(c.duration_s),
            "sample_period_s": json_number(c.sample_period_s),
            "kp": json_number(c.kp),
            "ki": json_number(c.ki),
            "kd": json_number(c.kd),
            "gain_sets": c.gain_sets,
            "setpoints": list(c.setpoints),
            "min_reference": json_number(c.min_reference),
            "max_reference": json_number(c.max_reference),
            "max_ramp_rate_per_min": json_number(c.max_ramp_rate_per_min),
            "stages": c.stages,
            "steps": c.steps,
            "ramps": c.ramps,
            "holds": c.holds,
            "program_intervals": c.program_intervals,
            "cycles": c.cycles,
        }
        return data


def _saturation_json(s: SaturationStats) -> dict:
    return {
        "fraction": json_number(s.fraction),
        "fraction_high": json_number(s.fraction_high),
        "fraction_low": json_number(s.fraction_low),
        "longest_s": json_number(s.longest_s),
        "episodes": s.episodes,
        "windup_s": json_number(s.windup_s),
        "u_mean": json_number(s.u_mean),
        "u_min": json_number(s.u_min),
        "u_max": json_number(s.u_max),
    }


def _pid_json(p: PidContribution) -> dict:
    return {
        "share_p": json_number(p.share_p),
        "share_i": json_number(p.share_i),
        "share_d": json_number(p.share_d),
        "mean_abs_p": json_number(p.mean_abs_p),
        "mean_abs_i": json_number(p.mean_abs_i),
        "mean_abs_d": json_number(p.mean_abs_d),
        "dominant": p.dominant(),
    }


def _median(values, first: int, last: int) -> float:
    finite = [
        values[i] for i in range(max(first, 0), min(last + 1, len(values))) if valid(values[i])
    ]
    return percentile(finite, 0.5) if finite else NaN


def _highest(values) -> float:
    finite = [v for v in values if valid(v)]
    return max(finite) if finite else NaN


def is_measured(name: str) -> bool:
    """Channels whose values are measurements (spikes / flatlines make sense there)."""
    return name in ("temp", "temp_raw") or name.startswith("pres_")


def analyse_experiment(
    data: ExperimentInput, options: ExperimentOptions | None = None
) -> ExperimentAnalysis:
    options = options or ExperimentOptions()
    result = ExperimentAnalysis()
    times = data.times
    n = len(times)

    def channel(name: str) -> list[float]:
        values = data.channels.get(name, [])
        return values if len(values) >= n else []

    value = channel("temp")
    reference = channel("temp_ref")
    u = channel("temp_u")
    u_p, u_i, u_d = channel("temp_u_p"), channel("temp_u_i"), channel("temp_u_d")
    kp, ki, kd = channel("temp_kp"), channel("temp_ki"), channel("temp_kd")
    interval = channel("interval")
    cycle = channel("cycle_cur")

    # ---- control KPIs, stages, steady state
    if value and reference:
        result.run = analyse_run(
            times, value, reference, interval, cycle, options.kpi, options.segmentation
        )
        result.has_control = result.run.overall.samples > 0
    if value:
        result.steady = detect_steady_states(times, value, reference, options.steady)

    # ---- whole-run control behaviour
    episodes = []
    if n > 0:
        if u:
            result.saturation = analyse_saturation(
                times, u, u_i, 0, n - 1, options.saturation, episodes
            )
        if u_p and u_i and u_d:
            result.pid = analyse_pid_contribution(u_p, u_i, u_d, 0, n - 1)

    # ---- per stage
    for stage in result.run.stages:
        s = stage.segment
        detail = StageDetail()
        steady_at = first_steady_time(result.steady, s.start, s.end)
        detail.time_to_steady_s = steady_at - s.start if valid(steady_at) else NaN
        detail.steady_fraction = steady_fraction(result.steady, s.start, s.end)
        if s.kind == RAMP:
            reference_trend = compute_stats(times, reference, s.start, s.end)
            value_trend = compute_stats(times, value, s.start, s.end)
            detail.reference_rate_per_min = reference_trend.slope_per_s * 60.0
            detail.value_rate_per_min = value_trend.slope_per_s * 60.0
            if abs(reference_trend.slope_per_s) > 1e-9:
                detail.tracking_lag_s = stage.kpis.bias / reference_trend.slope_per_s
        if u:
            detail.saturation = analyse_saturation(
                times, u, u_i, s.first, s.last, options.saturation
            )
        if u_p and u_i and u_d:
            detail.pid = analyse_pid_contribution(u_p, u_i, u_d, s.first, s.last)
        detail.kp = _median(kp, s.first, s.last) if kp else NaN
        detail.ki = _median(ki, s.first, s.last) if ki else NaN
        detail.kd = _median(kd, s.first, s.last) if kd else NaN
        result.stage_details.append(detail)

    # ---- configuration
    config = result.config
    config.samples = n
    if n > 1:
        config.duration_s = times[-1] - times[0]
        config.sample_period_s = percentile([times[i] - times[i - 1] for i in range(1, n)], 0.5)
    channels = set(data.channels)
    for alias in ("kp", "ki", "kd"):  # Insight-compatible copies of temp_kp / temp_ki / temp_kd
        if "temp_" + alias in channels:
            channels.discard(alias)
    config.channels = sorted(channels)
    if kp and ki and kd:
        used: dict[tuple[float, float, float], int] = {}
        for i in range(n):
            if valid(kp[i]) and valid(ki[i]) and valid(kd[i]):
                key = (kp[i], ki[i], kd[i])
                used[key] = used.get(key, 0) + 1
        config.gain_sets = len(used)
        best = 0
        for key in sorted(used):  # QMap order: ties go to the smallest gains
            if used[key] > best:
                best = used[key]
                config.kp, config.ki, config.kd = key
    if reference:
        reference_stats = compute_stats(times, reference)
        config.min_reference = reference_stats.min
        config.max_reference = reference_stats.max
    for k, stage in enumerate(result.run.stages):
        s = stage.segment
        config.stages += 1
        if s.kind == SETPOINT:
            config.steps += 1
        elif s.kind == RAMP:
            config.ramps += 1
            rate = abs(result.stage_details[k].reference_rate_per_min)
            if valid(rate):
                config.max_ramp_rate_per_min = (
                    max(config.max_ramp_rate_per_min, rate)
                    if valid(config.max_ramp_rate_per_min)
                    else rate
                )
        elif s.kind == HOLD:
            config.holds += 1
        # a level the reference sat at (a setpoint followed by its hold counts once)
        if (
            s.kind != RAMP
            and valid(s.reference_end)
            and (
                not config.setpoints
                or abs(config.setpoints[-1] - s.reference_end)
                >= options.segmentation.reference_tolerance
            )
        ):
            config.setpoints.append(s.reference_end)
    config.program_intervals = (
        max(0, int(_highest(interval))) if interval and valid(_highest(interval)) else 0
    )
    config.cycles = max(0, int(_highest(cycle))) if cycle and valid(_highest(cycle)) else 0

    # ---- anomalies
    found: list[Anomaly] = []
    for rule in data.rules:
        values = data.channels.get(rule.channel, [])
        if len(values) >= n:
            found += anomaly_mod.detect_limit_violations(times, values, rule)
    for name, values in data.channels.items():
        if is_measured(name) and len(values) >= n:
            found += anomaly_mod.detect_spikes(times, values, name, options.anomaly)
            found += anomaly_mod.detect_flatlines(times, values, name, options.anomaly)
    found += anomaly_mod.detect_gaps(times, value, "temp", options.anomaly)
    if result.has_control:
        found += anomaly_mod.detect_oscillation(times, value, reference, options.anomaly)
        found += anomaly_mod.detect_unreached_setpoints(result.run, options.anomaly)
    found += anomaly_mod.detect_long_saturation(episodes, options.anomaly)
    found.sort(key=lambda a: a.start)  # stable, like std::stable_sort
    result.anomalies = found
    return result
