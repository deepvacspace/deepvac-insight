"""Control-loop KPIs of a value following a reference (port of core/analysis/kpis.cpp).

Error convention: error = reference - value (positive when the value is below
the reference). Direction: +1 when the reference is above the initial value
(heating), else -1 (cooling). Every KPI is NaN when it cannot be computed.
"""

import math
from dataclasses import dataclass

from app.core.analysis.common import NaN, valid


@dataclass
class KpiOptions:
    settle_band: float = 0.5  # |error| band for settling time / steady state
    near_band: float = 2.0  # |error| band for "time to near"
    tail_window_s: float = 300.0  # tail statistics: the last N seconds
    tail_end_s: float = NaN  # tail window ends here (elapsed s); NaN = last sample
    initial_value: float = NaN  # value the transition starts from; NaN = first valid sample
    start_time_s: float = NaN  # times are measured from here; NaN = first valid sample
    rise_low: float = 0.1  # rise / fall time from 10 % ...
    rise_high: float = 0.9  # ... to 90 % of the transition


@dataclass
class ControlKpis:
    samples: int = 0  # samples with both value and reference
    mae: float = NaN
    rmse: float = NaN
    bias: float = NaN  # mean error
    max_abs_error: float = NaN
    overshoot: float = NaN  # max excursion beyond the reference in the approach direction
    overshoot_rms: float = NaN
    undershoot: float = NaN  # max excursion back after first reaching the reference
    rise_time_s: float = NaN  # 10 -> 90 % of the transition when heating
    fall_time_s: float = NaN  # ... when cooling
    time_to_near_s: float = NaN
    settling_time_s: float = NaN  # until |error| enters settle_band and stays there
    steady_state_error: float = NaN  # mean error after settling
    tail_mae: float = NaN
    tail_bias: float = NaN
    tail_std: float = NaN  # population std of the value over the tail window
    near_std: float = NaN  # std of the value while |error| <= near_band
    final_error: float = NaN
    min_value: float = NaN
    max_value: float = NaN
    end_value: float = NaN
    direction: int = 0


@dataclass
class CostWeights:
    tail_mae: float = 1.0
    overshoot: float = 10.0
    tail_std: float = 0.5
    final_error: float = 0.5
    invalid: float = 1_000_000.0


def _crossing_time(t, v, idx, level, direction):
    """Time `v` first crosses `level` going in `direction`, interpolated between samples."""
    for k, i in enumerate(idx):
        d = (v[i] - level) * direction
        if d >= 0.0:
            if k == 0:
                return t[i]
            j = idx[k - 1]
            prev = (v[j] - level) * direction
            fraction = prev / (prev - d)  # prev < 0 <= d
            return t[j] + (t[i] - t[j]) * fraction
    return NaN


def compute_kpis(times, value, reference, options: KpiOptions | None = None) -> ControlKpis:
    """times: elapsed s (ascending); value / reference: same length, NaN = missing.

    `reference` may also be a single number (constant setpoint).
    """
    options = options or KpiOptions()
    if isinstance(reference, (int, float)):
        reference = [float(reference)] * len(times)
    kpis = ControlKpis()
    n = min(len(times), len(value), len(reference))
    idx = [i for i in range(n) if valid(times[i]) and valid(value[i]) and valid(reference[i])]
    kpis.samples = len(idx)
    if not idx:
        return kpis

    t0 = options.start_time_s if valid(options.start_time_s) else times[idx[0]]
    t_end = options.tail_end_s if valid(options.tail_end_s) else times[idx[-1]]
    tail_start = max(t0, t_end - options.tail_window_s)
    y0 = options.initial_value if valid(options.initial_value) else value[idx[0]]
    any_tail = any(times[i] >= tail_start for i in idx)

    sum_abs = sum_sq = sum_err = max_abs = 0.0
    overshoot = sum_overshoot_sq = undershoot = 0.0
    reached = False
    tail_sum_abs = tail_sum_err = tail_sum = tail_sum_sq = 0.0
    tail_count = 0
    near_sum = near_sum_sq = 0.0
    near_count = 0
    min_value = max_value = value[idx[0]]
    last_outside_settle = -1  # position in idx of the last sample outside the settle band

    for k, i in enumerate(idx):
        y = value[i]
        r = reference[i]
        error = r - y
        abs_error = abs(error)
        direction = 1 if r > y0 else -1  # equal -> cooling, as in the C++ implementation
        sum_abs += abs_error
        sum_sq += error * error
        sum_err += error
        max_abs = max(max_abs, abs_error)
        min_value = min(min_value, y)
        max_value = max(max_value, y)
        beyond = max(y - r if direction > 0 else r - y, 0.0)
        overshoot = max(overshoot, beyond)
        sum_overshoot_sq += beyond * beyond
        if reached:
            undershoot = max(undershoot, max(r - y if direction > 0 else y - r, 0.0))
        elif (y >= r) if direction > 0 else (y <= r):
            reached = True
        if not any_tail or times[i] >= tail_start:
            tail_sum_abs += abs_error
            tail_sum_err += error
            tail_sum += y
            tail_sum_sq += y * y
            tail_count += 1
        if abs_error <= options.near_band:
            if not valid(kpis.time_to_near_s):
                kpis.time_to_near_s = times[i] - t0
            near_sum += y
            near_sum_sq += y * y
            near_count += 1
        if abs_error > options.settle_band:
            last_outside_settle = k

    count = float(len(idx))
    kpis.mae = sum_abs / count
    kpis.rmse = math.sqrt(sum_sq / count)
    kpis.bias = sum_err / count
    kpis.max_abs_error = max_abs
    kpis.overshoot = overshoot
    kpis.overshoot_rms = math.sqrt(sum_overshoot_sq / count)
    kpis.undershoot = undershoot if reached else NaN
    kpis.min_value = min_value
    kpis.max_value = max_value
    kpis.end_value = value[idx[-1]]
    kpis.final_error = reference[idx[-1]] - value[idx[-1]]
    if tail_count > 0:
        mean = tail_sum / tail_count
        kpis.tail_mae = tail_sum_abs / tail_count
        kpis.tail_bias = tail_sum_err / tail_count
        kpis.tail_std = math.sqrt(max(0.0, tail_sum_sq / tail_count - mean * mean))
    if near_count > 0:
        mean = near_sum / near_count
        kpis.near_std = math.sqrt(max(0.0, near_sum_sq / near_count - mean * mean))

    # settling: enter the band and stay in it until the end
    if last_outside_settle < len(idx) - 1:
        settled_at = idx[last_outside_settle + 1]
        kpis.settling_time_s = times[settled_at] - t0
        settled = [reference[i] - value[i] for i in idx[last_outside_settle + 1 :]]
        kpis.steady_state_error = sum(settled) / len(settled)

    # rise / fall time: 10 -> 90 % of the move from the initial value to the final reference
    target = reference[idx[-1]]
    span = target - y0
    kpis.direction = 1 if span > 0.0 else (-1 if span < 0.0 else 0)
    if kpis.direction != 0:
        low = _crossing_time(times, value, idx, y0 + options.rise_low * span, kpis.direction)
        high = _crossing_time(times, value, idx, y0 + options.rise_high * span, kpis.direction)
        transition = high - low if valid(low) and valid(high) else NaN
        if kpis.direction > 0:
            kpis.rise_time_s = transition
        else:
            kpis.fall_time_s = transition
    return kpis


def control_cost(
    kpis: ControlKpis, invalid: bool = False, weights: CostWeights | None = None
) -> float:
    """Weighted cost used to rank controller settings (lower is better)."""
    weights = weights or CostWeights()

    def or_zero(v):
        return v if valid(v) else 0.0

    cost = (
        weights.tail_mae * or_zero(kpis.tail_mae)
        + weights.overshoot * or_zero(kpis.overshoot)
        + weights.tail_std * or_zero(kpis.tail_std)
        + weights.final_error * abs(or_zero(kpis.final_error))
    )
    if invalid:
        cost += weights.invalid
    return cost
