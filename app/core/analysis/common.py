"""Helpers shared by the analysis modules.

The analysis package is a port of control2-client's Analysis Core
(src/core/analysis/*.cpp). Keep the two in step: the stored summaries
(summary_json, "analysis_version") are compared across both apps.
"""

import math

NaN = float("nan")


def valid(value) -> bool:
    return value is not None and math.isfinite(value)


def lround(value: float) -> int:
    """C's lround: halves away from zero (Python's round() rounds halves to even)."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def as_floats(values) -> list[float]:
    """Any sequence of numbers / None -> list of floats, None -> NaN."""
    if values is None:
        return []
    return [NaN if v is None else float(v) for v in values]


def json_number(value):
    """NaN / inf -> None, so results serialise as JSON null like the C++ toJson()."""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
