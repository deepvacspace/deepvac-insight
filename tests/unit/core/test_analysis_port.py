"""Tests of the Analysis Core against the C++ results."""

import gzip
import json
import math
from pathlib import Path

import pytest

from app.core.analysis import ANALYSIS_VERSION, analyse_experiment
from app.core.analysis.anomalies import number
from app.core.analysis.kpis import compute_kpis
from app.core.analysis.runsamples import experiment_input
from app.core.analysis.segmentation import HOLD, RAMP, SETPOINT, segment_experiment
from app.core.analysis.seriesstats import percentile

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "analysis" / "cpp_oracle_cases.json.gz"
CASES = json.loads(gzip.decompress(FIXTURE.read_bytes()))["cases"]


def _differences(expected, actual, path=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                yield f"{path}.{key}: present on one side only"
            else:
                yield from _differences(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            yield f"{path}: {len(expected)} items != {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual, strict=False)):
            yield from _differences(e, a, f"{path}[{i}]")
    elif (
        isinstance(expected, (int, float))
        and isinstance(actual, (int, float))
        and not isinstance(expected, bool)
    ):
        if not math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-9):
            yield f"{path}: {expected!r} != {actual!r}"
    elif expected != actual:
        yield f"{path}: {expected!r} != {actual!r}"


@pytest.mark.parametrize("case", CASES, ids=[c["key"].split("/")[-1][:40] for c in CASES])
def test_matches_cpp_analysis_core(case):
    actual = json.loads(json.dumps(analyse_experiment(experiment_input(case["rows"])).to_json()))
    diffs = list(_differences(case["expected"], actual))
    assert not diffs, "\n".join(diffs[:20])


def test_fixture_covers_the_analyses():
    kinds = {a["kind"] for c in CASES for a in c["expected"]["anomalies"]}
    assert {"spike", "gap", "oscillation", "saturation", "not_reached"} <= kinds
    assert any(c["expected"]["config"]["ramps"] for c in CASES)
    assert any(c["expected"]["config"]["program_intervals"] > 1 for c in CASES)
    assert all(c["expected"]["analysis_version"] == ANALYSIS_VERSION for c in CASES)


def test_percentile_interpolates_like_numpy():
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.5
    assert percentile([1.0, float("nan"), 3.0], 0.95) == pytest.approx(2.9)
    assert math.isnan(percentile([], 0.5))


def test_number_rounds_ties_like_qt():
    assert number(439.25, 4) == "439.3"
    assert number(-439.25, 4) == "-439.3"
    assert number(508.0097482204437, 4) == "508"


def _step_response():
    """Builds a step response sample set."""
    times = [float(t) for t in range(0, 1200, 2)]
    value = []
    for t in times:
        if t < 300:
            value.append(20.0 + 30.0 * t / 300.0)
        elif t < 400:
            value.append(50.0 + 1.0 * (1 - abs(t - 350) / 50.0))
        else:
            value.append(50.0)
    return times, value, [50.0] * len(times)


def test_kpis_of_a_step_response():
    times, value, reference = _step_response()
    k = compute_kpis(times, value, reference)
    assert k.direction == 1
    assert k.overshoot == pytest.approx(1.0, abs=0.05)
    assert k.final_error == 0.0
    assert k.settling_time_s == 376.0
    assert k.rise_time_s == pytest.approx(0.8 * 300, abs=4)


def test_segments_a_step_then_hold_and_a_ramp():
    times, value, reference = _step_response()
    kinds = [s.kind for s in segment_experiment(times, value, reference)]
    assert kinds[0] == SETPOINT and HOLD in kinds

    ramp_times = [float(t) for t in range(0, 1800, 2)]
    ramp_ref = [20.0 + min(t, 900.0) / 60.0 for t in ramp_times]
    kinds = [s.kind for s in segment_experiment(ramp_times, ramp_ref, ramp_ref)]
    assert RAMP in kinds and kinds[-1] == HOLD
