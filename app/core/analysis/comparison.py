"""Comparing runs (port of core/analysis/comparison.cpp)."""

import math
from dataclasses import dataclass, field

from app.core.analysis.anomalies import WARNING, number
from app.core.analysis.common import NaN, valid
from app.core.analysis.experimentanalysis import ExperimentAnalysis, RunConfig
from app.core.analysis.segmentation import RAMP, SETPOINT
from app.core.analysis.steadystate import steady_fraction

# what the runs of a comparison are lined up on
ALIGN_START = "start"  # run start
ALIGN_SETPOINT_CHANGE = "setpoint_change"  # n-th change of the reference (step or ramp start)
ALIGN_TARGET_CROSSING = (
    "target_crossing"  # first time the value reaches the reference after the n-th change
)
ALIGN_STAGE = "stage"  # start of stage n

LOWER = "lower"
HIGHER = "higher"
NONE = "none"


@dataclass
class KpiComparisonRow:
    key: str  # stable id
    label: str  # English
    unit: str  # "", "s", "%", "°C"
    better: str = LOWER
    values: list[float] = field(default_factory=list)  # one per run, NaN when not available


@dataclass
class ConfigComparisonRow:
    label: str
    values: list[str] = field(default_factory=list)  # one per run
    differs: bool = False


@dataclass
class SignalComparison:
    samples: int = 0
    overlap_s: float = NaN
    mean_diff: float = NaN  # mean(B - A)
    rms_diff: float = NaN
    max_abs_diff: float = NaN
    correlation: float = NaN  # Pearson


def _setpoint_changes(analysis: ExperimentAnalysis, tolerance: float) -> list[int]:
    """Starts of the stages where the reference moves to a new level."""
    stages = analysis.run.stages
    changes = []
    for k in range(1, len(stages)):
        previous, stage = stages[k - 1].segment, stages[k].segment
        moves = (stage.kind == RAMP and previous.kind != RAMP) or (
            stage.kind == SETPOINT
            and abs(stage.reference_start - previous.reference_end) >= tolerance
        )
        if moves:
            changes.append(k)
    return changes


def _text(value: float, precision: int = 4) -> str:
    return number(value, precision) if valid(value) else "—"


def alignment_anchor(
    analysis: ExperimentAnalysis, times, value, reference, mode: str, n: int
) -> float:
    """Elapsed time of the anchor in one run (n is 1-based); NaN when the run has no such anchor."""
    if not times:
        return NaN
    stages = analysis.run.stages
    if mode == ALIGN_START:
        return times[0]
    if mode == ALIGN_STAGE:
        return stages[n - 1].segment.start if 1 <= n <= len(stages) else NaN
    if mode in (ALIGN_SETPOINT_CHANGE, ALIGN_TARGET_CROSSING):
        changes = _setpoint_changes(analysis, 0.05)
        if n < 1 or n > len(changes):
            return NaN
        stage = stages[changes[n - 1]].segment
        if mode == ALIGN_SETPOINT_CHANGE:
            return stage.start
        # first sample from the change on where the error changes sign or reaches zero
        count = min(len(times), len(value), len(reference))
        side = 0
        for i in range(stage.first, count):
            error = reference[i] - value[i]
            if not valid(error):
                continue
            now = 1 if error > 0.0 else (-1 if error < 0.0 else 0)
            if side == 0:
                side = now
                if now == 0:
                    return times[i]
            elif now != side:
                return times[i]
    return NaN


def _steady_percent(a: ExperimentAnalysis) -> float:
    if not a.config.duration_s > 0.0:
        return NaN
    start = a.run.stages[0].segment.start if a.run.stages else 0.0
    end = a.run.stages[-1].segment.end if a.run.stages else a.config.duration_s
    return 100.0 * steady_fraction(a.steady, start, end)


KPI_SPECS = [
    ("mae", "MAE", "°C", LOWER, lambda a: a.run.overall.mae),
    ("rmse", "RMSE", "°C", LOWER, lambda a: a.run.overall.rmse),
    ("max_abs_error", "Max |error|", "°C", LOWER, lambda a: a.run.overall.max_abs_error),
    ("overshoot", "Overshoot (worst step)", "°C", LOWER, lambda a: a.run.overshoot),
    ("undershoot", "Undershoot (worst step)", "°C", LOWER, lambda a: a.run.undershoot),
    ("settling", "Settling (last step)", "s", LOWER, lambda a: a.run.settling_time_s),
    (
        "steady_state_error",
        "Steady-state error",
        "°C",
        NONE,
        lambda a: a.run.overall.steady_state_error,
    ),
    ("tail_mae", "Tail MAE", "°C", LOWER, lambda a: a.run.overall.tail_mae),
    ("tail_std", "Tail std", "°C", LOWER, lambda a: a.run.overall.tail_std),
    ("cost", "Control cost", "", LOWER, lambda a: a.run.cost),
    ("steady_fraction", "Time steady", "%", HIGHER, _steady_percent),
    # full heating / cooling is normal, neither better nor worse
    ("saturation", "Output saturated", "%", NONE, lambda a: 100.0 * a.saturation.fraction),
    ("windup", "Integrator windup", "s", LOWER, lambda a: a.saturation.windup_s),
    ("share_p", "P share", "%", NONE, lambda a: 100.0 * a.pid.share_p),
    ("share_i", "I share", "%", NONE, lambda a: 100.0 * a.pid.share_i),
    ("share_d", "D share", "%", NONE, lambda a: 100.0 * a.pid.share_d),
    ("anomalies", "Anomalies (warning+)", "", LOWER, lambda a: float(a.anomaly_count(WARNING))),
]


def compare_kpis(runs: list[ExperimentAnalysis | None]) -> list[KpiComparisonRow]:
    """The headline KPIs of every run side by side (first run = reference)."""
    rows = []
    for key, label, unit, better, get in KPI_SPECS:
        rows.append(
            KpiComparisonRow(
                key, label, unit, better, [get(run) if run is not None else NaN for run in runs]
            )
        )
    return rows


def _setpoints_text(c: RunConfig) -> str:
    return " → ".join(_text(s) for s in c.setpoints) if c.setpoints else "—"


CONFIG_SPECS = [
    ("Duration, s", lambda c: _text(c.duration_s, 6)),
    ("Sample period, s", lambda c: _text(c.sample_period_s, 3)),
    ("Kp", lambda c: _text(c.kp)),
    ("Ki", lambda c: _text(c.ki)),
    ("Kd", lambda c: _text(c.kd)),
    ("PID gain sets used", lambda c: str(c.gain_sets)),
    ("Setpoints, °C", _setpoints_text),
    ("Reference range, °C", lambda c: f"{_text(c.min_reference)} … {_text(c.max_reference)}"),
    ("Max ramp rate, °C/min", lambda c: _text(c.max_ramp_rate_per_min, 3)),
    ("Stages (step / ramp / hold)", lambda c: f"{c.stages} ({c.steps} / {c.ramps} / {c.holds})"),
    ("Program intervals", lambda c: str(c.program_intervals) if c.program_intervals > 0 else "—"),
    ("Cycles", lambda c: str(c.cycles) if c.cycles > 0 else "—"),
]


def compare_configs(configs: list[RunConfig | None]) -> list[ConfigComparisonRow]:
    rows = []
    for label, get in CONFIG_SPECS:
        values = [get(c) if c is not None else "—" for c in configs]
        rows.append(ConfigComparisonRow(label, values, any(v != values[0] for v in values)))
    return rows


def match_steps(runs: list[ExperimentAnalysis | None]) -> list[list[int]]:
    """Step responses of every run paired up: k-th step of each run (stage index, -1 when missing)."""
    steps = [
        [k for k, s in enumerate(run.run.stages) if s.segment.kind == SETPOINT] if run else []
        for run in runs
    ]
    most = max((len(s) for s in steps), default=0)
    return [[s[k] if k < len(s) else -1 for s in steps] for k in range(most)]


def compare_signals(
    times_a, values_a, offset_a: float, times_b, values_b, offset_b: float
) -> SignalComparison:
    """Signal B against A after shifting both by their alignment offsets (B interpolated at A's samples)."""
    result = SignalComparison()
    n_a = min(len(times_a), len(values_a))
    n_b = min(len(times_b), len(values_b))
    if n_a == 0 or n_b < 2:
        return result
    sum_a = sum_b = sum_aa = sum_bb = sum_ab = sum_diff = sum_diff2 = max_diff = 0.0
    first = last = NaN
    j = 0
    for i in range(n_a):
        t = times_a[i] - offset_a + offset_b  # A's sample on B's time axis
        if not valid(values_a[i]) or t < times_b[0] or t > times_b[n_b - 1]:
            continue
        while j + 1 < n_b and times_b[j + 1] < t:
            j += 1
        k = min(j + 1, n_b - 1)
        span = times_b[k] - times_b[j]
        w = (t - times_b[j]) / span if span > 0.0 else 0.0
        b = values_b[j] + w * (values_b[k] - values_b[j])
        if not valid(b):
            continue
        a = values_a[i]
        diff = b - a
        result.samples += 1
        sum_a += a
        sum_b += b
        sum_aa += a * a
        sum_bb += b * b
        sum_ab += a * b
        sum_diff += diff
        sum_diff2 += diff * diff
        max_diff = max(max_diff, abs(diff))
        first = first if valid(first) else times_a[i]
        last = times_a[i]
    if result.samples == 0:
        return result
    n = float(result.samples)
    result.overlap_s = last - first
    result.mean_diff = sum_diff / n
    result.rms_diff = math.sqrt(sum_diff2 / n)
    result.max_abs_diff = max_diff
    var_a = sum_aa - sum_a * sum_a / n
    var_b = sum_bb - sum_b * sum_b / n
    if var_a > 0.0 and var_b > 0.0:
        result.correlation = (sum_ab - sum_a * sum_b / n) / math.sqrt(var_a * var_b)
    return result
