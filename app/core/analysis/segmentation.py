"""Splits an experiment into stages (port of core/analysis/segmentation.cpp).

- ramp: the reference moves gradually;
- setpoint: the reference is constant and the value is still moving towards it;
- hold: the reference is constant and the value has reached it;
- unknown: no reference recorded.
"""

import math
from dataclasses import dataclass, replace

from app.core.analysis.common import NaN, lround, valid

RAMP = "ramp"
SETPOINT = "setpoint"
HOLD = "hold"
UNKNOWN = "unknown"

RAMP_WINDOW_S = 120.0  # reference drift is measured over this centred window
RAMP_NEIGHBOURS = 3  # samples on each side searched for similar reference changes
RAMP_INCREMENT_RATIO = 3.0  # "similar": within this factor of each other


@dataclass
class Segment:
    kind: str = UNKNOWN
    first: int = 0  # sample indexes, inclusive
    last: int = 0
    start: float = 0.0  # times of first / last
    end: float = 0.0
    interval: int = -1  # PLC program interval (1-based), -1 when not recorded
    cycle: int = -1  # PLC program cycle, -1 when not recorded
    reference_start: float = NaN
    reference_end: float = NaN

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class SegmentationOptions:
    reference_tolerance: float = 0.05  # reference changes smaller than this are noise
    settle_band: float = 0.5  # setpoint ends / hold starts when |reference - value| gets this small
    min_duration_s: float = 30.0  # shorter stages are merged into the previous one


def _to_int(values, i: int) -> int:
    return lround(values[i]) if i < len(values) and valid(values[i]) else -1


def filled_reference(reference, n: int) -> list[float]:
    """NaN gaps of the reference take the last known value (the first known one before it)."""
    filled = [NaN] * n
    last = NaN
    for i in range(n):
        if i < len(reference) and valid(reference[i]):
            last = reference[i]
        filled[i] = last
    first = NaN
    for i in range(n - 1, -1, -1):
        if valid(filled[i]):
            first = filled[i]
        else:
            filled[i] = first
    return filled


def segment_experiment(
    times, value, reference, interval=(), cycle=(), options: SegmentationOptions | None = None
) -> list[Segment]:
    """Boundaries come from the recorded PLC program position (interval / cycle
    changes) when available, and from the reference itself (steps, ramp start /
    end). Any of interval, cycle, reference, value may be empty."""
    options = options or SegmentationOptions()
    n = len(times)
    if n == 0:
        return []
    ref = filled_reference(reference, n)
    have_reference = valid(ref[0])

    # reference drift without its steps, to tell ramps from steps
    delta = [0.0] * n
    for i in range(1, n):
        delta[i] = ref[i] - ref[i - 1] if have_reference else 0.0

    # a change is a step unless it is part of a ramp: a fast ramp moves more than the tolerance
    # per sample, but then nearby samples change by a similar amount in the same direction
    def ramp_increment(i: int) -> bool:
        size = abs(delta[i])
        for k in range(max(1, i - RAMP_NEIGHBOURS), min(i + RAMP_NEIGHBOURS, n - 1) + 1):
            other = abs(delta[k])
            if (
                k != i
                and delta[k] * delta[i] > 0.0
                and other * RAMP_INCREMENT_RATIO >= size
                and other <= size * RAMP_INCREMENT_RATIO
            ):
                return True
        return False

    jump = [False] * n
    drift = [0.0] * n
    for i in range(1, n):
        jump[i] = abs(delta[i]) >= options.reference_tolerance and not ramp_increment(i)
        drift[i] = drift[i - 1] + (0.0 if jump[i] else delta[i])

    ramping = [False] * n
    if have_reference:
        lo = hi = 0
        for i in range(n):
            while times[lo] < times[i] - RAMP_WINDOW_S / 2.0:
                lo += 1
            while hi + 1 < n and times[hi + 1] <= times[i] + RAMP_WINDOW_S / 2.0:
                hi += 1
            ramping[i] = abs(drift[hi] - drift[lo]) > options.reference_tolerance
        # the centred window flags half a window before / after a ramp: keep only where it moves
        a = 0
        while a < n:
            if not ramping[a]:
                a += 1
                continue
            b = a
            while b + 1 < n and ramping[b + 1]:
                b += 1
            first = last = -1
            for i in range(max(a, 1), b + 1):
                if drift[i] != drift[i - 1]:
                    first = i - 1 if first < 0 else first
                    last = i
            for i in range(a, b + 1):
                ramping[i] = first >= 0 and first <= i <= last
            a = b + 1

    def kind_at(i: int) -> str:
        if not have_reference:
            return UNKNOWN
        return RAMP if ramping[i] else HOLD

    # raw stages: a new one at every program position change, step, or ramp start / end
    stages: list[Segment] = []
    for i in range(n):
        kind = kind_at(i)
        start_new = not stages
        if not start_new:
            current = stages[-1]
            program_interval = _to_int(interval, i)
            program_cycle = _to_int(cycle, i)
            start_new = (
                kind != current.kind
                or (program_interval >= 0 and program_interval != current.interval)
                or (program_cycle >= 0 and program_cycle != current.cycle)
                or (jump[i] and kind == HOLD)
            )
        if start_new:
            stages.append(
                Segment(
                    kind=kind,
                    first=i,
                    last=i,
                    interval=_to_int(interval, i),
                    cycle=_to_int(cycle, i),
                )
            )
        else:
            stages[-1].last = i

    # constant reference: SETPOINT while the value approaches, HOLD once it got there
    split: list[Segment] = []
    for stage in stages:
        if stage.kind != HOLD:
            split.append(stage)
            continue
        reached_at = -1
        for i in range(stage.first, stage.last + 1):
            if i < len(value) and valid(value[i]) and abs(ref[i] - value[i]) <= options.settle_band:
                reached_at = i
                break
        if reached_at == stage.first:
            split.append(stage)
        elif reached_at < 0:
            split.append(replace(stage, kind=HOLD if len(value) == 0 else SETPOINT))
        else:
            split.append(replace(stage, kind=SETPOINT, last=reached_at - 1))
            split.append(replace(stage, first=reached_at))

    # merge stages too short to mean anything into their neighbour (same program position)
    merged: list[Segment] = []
    for stage in split:
        duration = times[stage.last] - times[stage.first]
        same_position = (
            bool(merged)
            and merged[-1].interval == stage.interval
            and merged[-1].cycle == stage.cycle
        )
        mergeable = (
            same_position and not jump[stage.first]
        )  # a reference step always starts a new stage
        if mergeable and (duration < options.min_duration_s or merged[-1].kind == stage.kind):
            merged[-1].last = stage.last
        else:
            merged.append(replace(stage))

    # a short stage at the start of a program position (or of the run) joins the next stage there
    i = 0
    while i + 1 < len(merged):
        stage, nxt = merged[i], merged[i + 1]
        short_stage = times[stage.last] - times[stage.first] < options.min_duration_s
        if (
            short_stage
            and stage.interval == nxt.interval
            and stage.cycle == nxt.cycle
            and not jump[nxt.first]
        ):
            nxt.first = stage.first
            del merged[i]
        else:
            i += 1

    for segment in merged:
        segment.start = times[segment.first]
        segment.end = times[segment.last]
        segment.reference_start = ref[segment.first]
        segment.reference_end = ref[segment.last]
    return merged


def is_nan(value: float) -> bool:
    return isinstance(value, float) and math.isnan(value)
