"""Anomaly detection."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.analysis.common import NaN, json_number, valid
from app.core.analysis.controlanalysis import SaturationEpisode
from app.core.analysis.runanalysis import RunAnalysis
from app.core.analysis.segmentation import SETPOINT
from app.core.analysis.seriesstats import percentile

LIMIT = "limit"
SPIKE = "spike"
FLATLINE = "flatline"
GAP = "gap"
OSCILLATION = "oscillation"
NOT_REACHED = "not_reached"
SATURATION = "saturation"

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"
SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}


@dataclass
class Anomaly:
    kind: str = LIMIT
    severity: str = WARNING
    channel: str = ""
    start: float = 0.0
    end: float = 0.0
    value: float = NaN
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "channel": self.channel,
            "start_s": self.start,
            "end_s": self.end,
            "value": json_number(self.value),
            "detail": self.detail,
        }


@dataclass
class LimitRule:
    """Acceptable band of a variable."""

    channel: str
    lo: float = NaN
    hi: float = NaN
    label: str = ""


@dataclass
class AnomalyOptions:
    spike_z: float = 8.0
    spike_revert_samples: int = 3
    spike_min_jump: float = 0.0
    spike_min_jump_of_span: float = 0.02
    flatline_s: float = 900.0
    gap_factor: float = 5.0
    min_gap_s: float = 10.0
    oscillation_window_s: float = 600.0
    oscillation_crossings: int = 6
    crossing_deadband: float = 0.2
    near_band: float = 2.0
    saturation_s: float = 300.0


def number(value: float, precision: int = 3) -> str:
    """Formats a number like QString::number."""
    if not valid(value) or value == 0.0:
        return f"{value:.{precision}g}"
    exact = Decimal(value)
    rounded = exact.quantize(
        Decimal(1).scaleb(exact.adjusted() - precision + 1), rounding=ROUND_HALF_UP
    )
    return f"{float(rounded):.{precision}g}"


def _typical_period(times) -> float:
    steps = [times[i] - times[i - 1] for i in range(1, len(times))]
    return percentile(steps, 0.5) if steps else NaN


def detect_limit_violations(times, values, rule: LimitRule) -> list[Anomaly]:
    found = []
    n = min(len(times), len(values))
    begin = -1
    worst = NaN

    def excursion(v):
        over = 0.0
        if valid(rule.hi) and v > rule.hi:
            over = v - rule.hi
        if valid(rule.lo) and v < rule.lo:
            over = max(over, rule.lo - v)
        return over

    for i in range(n + 1):
        outside = i < n and valid(values[i]) and excursion(values[i]) > 0.0
        if outside:
            if begin < 0:
                begin = i
                worst = values[i]
            elif excursion(values[i]) > excursion(worst):
                worst = values[i]
        elif begin >= 0:
            lo = number(rule.lo, 5) if valid(rule.lo) else "-inf"
            hi = number(rule.hi, 5) if valid(rule.hi) else "+inf"
            found.append(
                Anomaly(
                    LIMIT,
                    WARNING,
                    rule.channel,
                    times[begin],
                    times[i - 1],
                    worst,
                    f"{rule.label or rule.channel}: {number(worst, 5)} outside [{lo}, {hi}]",
                )
            )
            begin = -1
    return found


def detect_spikes(
    times, values, channel: str, options: AnomalyOptions | None = None
) -> list[Anomaly]:
    options = options or AnomalyOptions()
    n = min(len(times), len(values))
    deltas = [NaN] * n
    finite_deltas = []
    for i in range(1, n):
        if valid(values[i]) and valid(values[i - 1]):
            deltas[i] = values[i] - values[i - 1]
            finite_deltas.append(deltas[i])
    if len(finite_deltas) < 20:
        return []
    median = percentile(finite_deltas, 0.5)
    deviations = [abs(d - median) for d in finite_deltas]
    scale = 1.4826 * percentile(deviations, 0.5)
    if not scale > 0.0:
        scale = 1.2533 * sum(deviations) / len(deviations)
    if not scale > 0.0:
        return []
    levels = [v for v in values[:n] if valid(v)]
    min_jump = max(
        options.spike_min_jump,
        options.spike_min_jump_of_span * (percentile(levels, 0.99) - percentile(levels, 0.01)),
    )
    found: list[Anomaly] = []
    i = 1
    while i < n:
        if not valid(deltas[i]):
            i += 1
            continue
        z = (deltas[i] - median) / scale
        if abs(z) < options.spike_z or abs(deltas[i]) < min_jump:
            i += 1
            continue
        revert = -1
        j = i + 1
        while j < n and j <= i + options.spike_revert_samples:
            if (
                valid(deltas[j])
                and deltas[j] * deltas[i] < 0.0
                and abs(values[j] - values[i - 1]) <= 0.5 * abs(deltas[i])
            ):
                revert = j
                break
            j += 1
        if revert < 0:
            i += 1
            continue
        if found and times[i] - found[-1].end <= 0.0:
            i += 1
            continue
        found.append(
            Anomaly(
                SPIKE,
                WARNING,
                channel,
                times[i],
                times[revert - 1],
                values[i],
                f"{channel} jumped by {number(deltas[i])} (robust z {number(z, 2)}) and came back",
            )
        )
        i = revert + 1
    return found


def detect_flatlines(
    times, values, channel: str, options: AnomalyOptions | None = None
) -> list[Anomaly]:
    options = options or AnomalyOptions()
    n = min(len(times), len(values))
    found = []
    begin = -1
    for i in range(n + 1):
        same = i < n and begin >= 0 and valid(values[i]) and values[i] == values[begin]
        if same:
            continue
        if begin >= 0 and times[i - 1] - times[begin] >= options.flatline_s:
            start, end = times[begin], times[i - 1]
            found.append(
                Anomaly(
                    FLATLINE,
                    INFO,
                    channel,
                    start,
                    end,
                    values[begin],
                    f"{channel} stuck at {number(values[begin], 5)} for {number(end - start, 4)} s",
                )
            )
        begin = i if i < n and valid(values[i]) else -1
    return found


def detect_gaps(
    times, values, channel: str, options: AnomalyOptions | None = None
) -> list[Anomaly]:
    """Time gaps between samples and stretches without a value of `channel`."""
    options = options or AnomalyOptions()
    found = []
    period = _typical_period(times)
    if not valid(period):
        return found
    threshold = max(options.min_gap_s, options.gap_factor * period)

    def add(start, end, detail):
        found.append(Anomaly(GAP, INFO, channel, start, end, end - start, detail))

    for i in range(1, len(times)):
        if times[i] - times[i - 1] >= threshold:
            add(times[i - 1], times[i], f"no samples for {number(times[i] - times[i - 1], 4)} s")
    n = min(len(times), len(values))
    begin = -1
    for i in range(n + 1):
        missing = i < n and not valid(values[i])
        if missing and begin < 0:
            begin = i
        elif not missing and begin >= 0:
            start = times[begin]
            end = times[i] if i < n else times[n - 1]
            if end - start >= threshold:
                add(start, end, f"{channel} missing for {number(end - start, 4)} s")
            begin = -1
    return found


def detect_oscillation(
    times, value, reference, options: AnomalyOptions | None = None
) -> list[Anomaly]:
    options = options or AnomalyOptions()
    n = min(len(times), len(value), len(reference))
    crossings = []
    side = 0
    for i in range(n):
        error = reference[i] - value[i]
        if not valid(error) or abs(error) <= options.crossing_deadband:
            continue
        now = 1 if error > 0.0 else -1
        if side != 0 and now != side:
            crossings.append(times[i])
        side = now
    found: list[Anomaly] = []
    lo = 0
    for i in range(len(crossings)):
        while crossings[lo] < crossings[i] - options.oscillation_window_s:
            lo += 1
        if i - lo + 1 < options.oscillation_crossings:
            continue
        start = crossings[lo]
        if found and start <= found[-1].end:
            found[-1].end = crossings[i]
            found[-1].value = max(found[-1].value, float(i - lo + 1))
        else:
            found.append(
                Anomaly(OSCILLATION, WARNING, "temp", start, crossings[i], float(i - lo + 1))
            )
    for anomaly in found:
        anomaly.detail = f"crossed the setpoint {number(anomaly.value)} times within {number(options.oscillation_window_s)} s"
    return found


def detect_unreached_setpoints(
    analysis: RunAnalysis, options: AnomalyOptions | None = None
) -> list[Anomaly]:
    """Detects step responses that never settled."""
    found = []
    for stage in analysis.stages:
        if stage.segment.kind != SETPOINT or valid(stage.kpis.settling_time_s):
            continue
        never_near = not valid(stage.kpis.time_to_near_s)
        ref_end = stage.segment.reference_end
        if never_near:
            detail = f"setpoint {number(ref_end, 5)} never approached (closest {number(abs(ref_end - stage.kpis.end_value), 3)})"
        else:
            detail = f"setpoint {number(ref_end, 5)} approached but never settled within the band"
        found.append(
            Anomaly(
                NOT_REACHED,
                CRITICAL if never_near else WARNING,
                "temp",
                stage.segment.start,
                stage.segment.end,
                ref_end,
                detail,
            )
        )
    return found


def detect_long_saturation(
    episodes: list[SaturationEpisode], options: AnomalyOptions | None = None
) -> list[Anomaly]:
    options = options or AnomalyOptions()
    found = []
    for episode in episodes:
        duration = episode.end - episode.start
        if duration < options.saturation_s:
            continue
        side = "upper" if episode.high else "lower"
        found.append(
            Anomaly(
                SATURATION,
                INFO,
                "temp_u",
                episode.start,
                episode.end,
                duration,
                f"control output at its {side} limit for {number(duration, 4)} s",
            )
        )
    return found
