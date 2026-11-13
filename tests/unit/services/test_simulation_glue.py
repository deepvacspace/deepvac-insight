"""Tests of the data service and simulation boundary."""

import numpy as np
import pytest

from app.services import data_service

pytestmark = pytest.mark.unit


def test_bounded_float_within_range_returns_value():
    assert data_service.bounded_float({"kp": "12.5"}, "kp", 7.0, 1.0, 50.0) == pytest.approx(12.5)


def test_bounded_float_uses_default_when_missing():
    assert data_service.bounded_float({}, "kp", 7.0, 1.0, 50.0) == pytest.approx(7.0)


def test_bounded_float_below_low_raises():
    with pytest.raises(ValueError):
        data_service.bounded_float({"kp": "0.5"}, "kp", 7.0, 1.0, 50.0)


def test_bounded_float_above_high_raises():
    with pytest.raises(ValueError):
        data_service.bounded_float({"kp": "999"}, "kp", 7.0, 1.0, 50.0)


def test_bounded_float_at_exact_boundary_is_accepted():
    assert data_service.bounded_float({"kp": "50.0"}, "kp", 7.0, 1.0, 50.0) == pytest.approx(50.0)
    assert data_service.bounded_float({"kp": "1.0"}, "kp", 7.0, 1.0, 50.0) == pytest.approx(1.0)


def test_bounded_float_error_message_names_the_field():
    with pytest.raises(ValueError, match="kp"):
        data_service.bounded_float({"kp": "0"}, "kp", 7.0, 1.0, 50.0)


def test_make_sim_args_uses_payload_values():
    args = data_service.make_sim_args(
        {"start_temp": 25.0, "target_temp": 55.0, "duration_s": 600.0, "dt_s": 1.0}
    )
    assert args.start_temp == 25.0
    assert args.target_temp == 55.0
    assert args.duration_s == 600.0
    assert args.dt_s == 1.0


def test_make_sim_args_defaults_when_payload_empty():
    args = data_service.make_sim_args({})
    assert args.start_temp == 27.0
    assert args.target_temp == 0.0
    assert args.duration_s == 1200.0
    assert args.dt_s == 2.0
    assert args.initial_i == 0.0


def test_make_sim_args_includes_fixed_controller_defaults_not_from_payload():
    args = data_service.make_sim_args({"u_min": -999.0})
    assert args.u_min == -1.0


@pytest.mark.integration
def test_simulate_twin_run_end_to_end():
    result = data_service.simulate_twin_run(
        {
            "kp": 7.0,
            "ki": 700.0,
            "kd": 10.0,
            "start_temp": 27.0,
            "target_temp": 60.0,
            "duration_s": 10.0,
            "dt_s": 2.0,
        }
    )
    assert "metrics" in result
    assert "points" in result
    assert result["metrics"]["kp"] == pytest.approx(7.0)
    assert result["metrics"]["ki"] == pytest.approx(700.0)
    assert result["metrics"]["kd"] == pytest.approx(10.0)
    assert len(result["points"]) == 5
    assert set(result["columns"]) == {
        "temp",
        "temp_ref",
        "error",
        "u",
        "u_p",
        "u_i",
        "u_d",
        "pred_delta",
    }
    for point in result["points"]:
        assert np.isfinite(point["values"]["temp"])


@pytest.mark.integration
def test_simulate_twin_run_invalid_kp_raises_not_silently_clamped():
    with pytest.raises(ValueError):
        data_service.simulate_twin_run({"kp": 99999.0})


@pytest.mark.integration
def test_simulate_twin_run_candidate_parameters_propagate_through_to_metrics():
    result_a = data_service.simulate_twin_run(
        {"kp": 5.0, "ki": 500.0, "kd": 5.0, "duration_s": 4.0, "dt_s": 2.0}
    )
    result_b = data_service.simulate_twin_run(
        {"kp": 20.0, "ki": 900.0, "kd": 15.0, "duration_s": 4.0, "dt_s": 2.0}
    )
    assert result_a["metrics"]["kp"] != result_b["metrics"]["kp"]
    assert result_a["metrics"]["kp"] == pytest.approx(5.0)
    assert result_b["metrics"]["kp"] == pytest.approx(20.0)
