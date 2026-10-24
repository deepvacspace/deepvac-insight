"""Descriptive statistics of one channel (port of core/analysis/seriesstats.cpp)."""

import math
from dataclasses import dataclass

from app.core.analysis.common import NaN, valid


@dataclass
class SeriesStats:
    n: int = 0  # valid samples in the interval
    missing: int = 0  # samples in the interval without a value
    min: float = NaN
    max: float = NaN
    mean: float = NaN
    std: float = NaN  # sample standard deviation (n - 1)
    rms: float = NaN
    slope_per_s: float = NaN  # least-squares trend, value units per second
    p95: float = NaN  # 95th percentile, linear interpolation


def percentile(values, q: float) -> float:
    """q in [0, 1], linear interpolation between order statistics (numpy's default)."""
    finite = sorted(v for v in values if valid(v))
    if not finite:
        return NaN
    position = min(max(q, 0.0), 1.0) * (len(finite) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(finite) - 1)
    return finite[lower] + (finite[upper] - finite[lower]) * (position - lower)


def compute_stats(times, values, start: float = NaN, end: float = NaN) -> SeriesStats:
    """times ascending (s); [start, end] inclusive, NaN bounds = unbounded."""
    stats = SeriesStats()
    in_range: list[float] = []
    total = total_sq = sum_t = sum_tt = sum_ty = 0.0
    t_ref = NaN  # centring the times keeps the slope numerically stable
    for t, y in zip(times, values, strict=False):  # the shorter series, like the C++ min(size)
        if (valid(start) and t < start) or (valid(end) and t > end):
            continue
        if not valid(y):
            stats.missing += 1
            continue
        if not valid(t_ref):
            t_ref = t
        dt = t - t_ref
        in_range.append(y)
        total += y
        total_sq += y * y
        sum_t += dt
        sum_tt += dt * dt
        sum_ty += dt * y
    stats.n = len(in_range)
    if stats.n == 0:
        return stats
    count = float(stats.n)
    stats.min = min(in_range)
    stats.max = max(in_range)
    stats.mean = total / count
    stats.rms = math.sqrt(total_sq / count)
    if stats.n > 1:
        variance = sum((y - stats.mean) ** 2 for y in in_range)
        stats.std = math.sqrt(variance / (count - 1.0))
        denominator = count * sum_tt - sum_t * sum_t
        if denominator > 0.0:
            stats.slope_per_s = (count * sum_ty - sum_t * total) / denominator
    stats.p95 = percentile(in_range, 0.95)
    return stats
