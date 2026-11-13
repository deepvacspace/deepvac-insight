"""Steady-state detection."""

import math
from dataclasses import dataclass

from app.core.analysis.common import NaN, valid


@dataclass
class SteadyStateOptions:
    window_s: float = 120.0
    max_std: float = 0.1
    max_slope_per_min: float = 0.05
    max_abs_error: float = 0.5
    min_duration_s: float = 60.0


@dataclass
class SteadyInterval:
    """An interval where the value is stable."""

    first: int = 0
    last: int = 0
    start: float = NaN
    end: float = NaN
    mean: float = NaN
    std: float = NaN
    mean_error: float = NaN

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_steady_states(
    times, value, reference=(), options: SteadyStateOptions | None = None
) -> list[SteadyInterval]:
    """Detects steady intervals."""
    options = options or SteadyStateOptions()
    n = min(len(times), len(value))
    if n < 3:
        return []
    with_reference = len(reference) >= n
    t0 = times[0]

    count = [0.0] * (n + 1)
    st = [0.0] * (n + 1)
    sv = [0.0] * (n + 1)
    stt = [0.0] * (n + 1)
    svv = [0.0] * (n + 1)
    stv = [0.0] * (n + 1)
    error_count = [0.0] * (n + 1)
    error_sum = [0.0] * (n + 1)
    for i in range(n):
        t = times[i] - t0
        v = value[i]
        ok = valid(v)
        count[i + 1] = count[i] + (1.0 if ok else 0.0)
        st[i + 1] = st[i] + (t if ok else 0.0)
        sv[i + 1] = sv[i] + (v if ok else 0.0)
        stt[i + 1] = stt[i] + (t * t if ok else 0.0)
        svv[i + 1] = svv[i] + (v * v if ok else 0.0)
        stv[i + 1] = stv[i] + (t * v if ok else 0.0)
        error_ok = with_reference and ok and valid(reference[i])
        error_count[i + 1] = error_count[i] + (1.0 if error_ok else 0.0)
        error_sum[i + 1] = error_sum[i] + (reference[i] - v if error_ok else 0.0)

    cover = [0] * (n + 1)
    lo = 0
    for i in range(n):
        while times[lo] < times[i] - options.window_s:
            lo += 1
        if times[i] - times[lo] < 0.5 * options.window_s:
            continue
        c = count[i + 1] - count[lo]
        if c < 5.0:
            continue
        s_t = st[i + 1] - st[lo]
        s_v = sv[i + 1] - sv[lo]
        variance = max(0.0, (svv[i + 1] - svv[lo] - s_v * s_v / c) / (c - 1.0))
        denominator = c * (stt[i + 1] - stt[lo]) - s_t * s_t
        slope = (c * (stv[i + 1] - stv[lo]) - s_t * s_v) / denominator if denominator > 0.0 else 0.0
        steady = (
            math.sqrt(variance) <= options.max_std
            and abs(slope) * 60.0 <= options.max_slope_per_min
        )
        if steady and with_reference:
            ec = error_count[i + 1] - error_count[lo]
            steady = (
                ec > 0.0 and abs((error_sum[i + 1] - error_sum[lo]) / ec) <= options.max_abs_error
            )
        if steady:
            cover[lo] += 1
            cover[i + 1] -= 1

    intervals: list[SteadyInterval] = []
    depth = 0
    begin = -1
    for i in range(n + 1):
        steady = False
        if i < n:
            depth += cover[i]
            steady = depth > 0
        if steady and begin < 0:
            begin = i
        elif not steady and begin >= 0:
            interval = SteadyInterval(first=begin, last=i - 1, start=times[begin], end=times[i - 1])
            if interval.duration >= options.min_duration_s:
                c = count[i] - count[begin]
                s_v = sv[i] - sv[begin]
                if c > 0.0:
                    interval.mean = s_v / c
                    interval.std = (
                        math.sqrt(max(0.0, (svv[i] - svv[begin] - s_v * s_v / c) / (c - 1.0)))
                        if c > 1.0
                        else 0.0
                    )
                ec = error_count[i] - error_count[begin]
                interval.mean_error = (error_sum[i] - error_sum[begin]) / ec if ec > 0.0 else NaN
                intervals.append(interval)
            begin = -1
    return intervals


def first_steady_time(steady: list[SteadyInterval], start: float, end: float) -> float:
    """Returns the first time a steady interval is in progress."""
    for interval in steady:
        if interval.end >= start and interval.start <= end:
            return max(interval.start, start)
    return NaN


def steady_fraction(steady: list[SteadyInterval], start: float, end: float) -> float:
    """Part of [start, end] covered by steady intervals, 0 ..."""
    if not end > start:
        return NaN
    covered = sum(max(0.0, min(iv.end, end) - max(iv.start, start)) for iv in steady)
    return min(max(covered / (end - start), 0.0), 1.0)
