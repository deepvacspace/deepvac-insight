"""Helpers shared by the analysis modules."""

import math

NaN = float("nan")


def valid(value) -> bool:
    return value is not None and math.isfinite(value)


def lround(value: float) -> int:
    """Rounds halves away from zero."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def as_floats(values) -> list[float]:
    """Any sequence of numbers / None -> list of floats, None -> NaN."""
    if values is None:
        return []
    return [NaN if v is None else float(v) for v in values]


def json_number(value):
    """Returns None for non-finite numbers."""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
