"""Test profiles as PLC programs, shared by the Controller page and the agent API."""

from app.services import plc_gate

RAMP = "ramp"
HOLD = "hold"


class ProgramError(ValueError):
    pass


def program_for_profile(profile, enable_flags):
    """Builds the PLC program of a test profile."""
    program = profile.get("program")
    if program and program.get("intervals"):
        return list(program["intervals"]), dict(program.get("cycle") or {})
    intervals = []
    for i, step in enumerate(profile.get("steps") or [], start=1):
        if step.get("setpoint_temp") is None:
            raise ProgramError(
                f"Step {i} of '{profile['name']}' has no temperature: the chamber PLC runs "
                "temperature programs only."
            )
        intervals.append(
            {
                "duration": int(round(float(step["duration_s"]))),
                "temp": float(step["setpoint_temp"]),
                "flags": (enable_flags & 0x07) | (1 << plc_gate.AWAIT_BIT),
            }
        )
    if not intervals:
        raise ProgramError(f"'{profile['name']}' has no steps.")
    return intervals, {}


def interval_mode(interval):
    """RAMP, HOLD, or "" for an interval with neither flag."""
    flags = int(interval.get("flags", 0))
    if flags & (1 << plc_gate.RAMP_BIT):
        return RAMP
    if flags & (1 << plc_gate.AWAIT_BIT):
        return HOLD
    return ""


def total_duration_s(intervals, cycle=None):
    """Returns the programmed length."""
    durations = [float(i.get("duration", 0)) for i in intervals]
    total = sum(durations)
    cycle = cycle or {}
    count, start, end = (
        int(cycle.get("count", 0)),
        int(cycle.get("start", 0)),
        int(cycle.get("end", 0)),
    )
    if count > 1 and 1 <= start <= end <= len(durations):
        total += (count - 1) * sum(durations[start - 1 : end])
    return total


def check_limits(intervals, low, high):
    for i, interval in enumerate(intervals, start=1):
        temp = interval.get("temp")
        if temp is not None and not low <= float(temp) <= high:
            raise ProgramError(
                f"Interval {i} ({temp:g} °C) is outside the chamber's range {low:g} … {high:g} °C."
            )
