"""Test profiles as PLC programs, shared by the Controller page and the agent API.

A PLC program is a list of intervals plus a cycle, as control2-client's
program control sends it (plc_gate.encode_job). Each interval either ramps
to its temperature over its duration (RAMP_BIT) or goes to it at full speed
and holds it for its duration (AWAIT_BIT).

Profiles imported from control2-client carry their PLC program
(program_json) and run as they are. Profiles made in Insight are a list of
steps (setpoint, duration); each becomes a "go to and hold" interval.
"""

from app.services import plc_gate

RAMP = "ramp"
HOLD = "hold"


class ProgramError(ValueError):
    pass


def program_for_profile(profile, enable_flags):
    """(intervals, cycle) of a test profile; enable_flags: channel bits of an interval."""
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
    """Programmed length; a cycle repeats intervals start..end (1-based) count times in all."""
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
