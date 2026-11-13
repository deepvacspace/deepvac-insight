"""Tests of ChamberPID."""

import pytest

from app.model.simulation import ChamberPID

pytestmark = pytest.mark.unit


def test_initial_state_is_zero():
    pid = ChamberPID()
    assert pid.p_part == 0.0
    assert pid.i_part == 0.0
    assert pid.d_part == 0.0


def test_disabled_zeroes_and_returns_all_zero():
    pid = ChamberPID(u_min=-1.0, u_max=1.0)
    pid.p_part, pid.i_part, pid.d_part = 5.0, 5.0, 5.0
    u, p, i, d = pid.step(
        enable=False, x_target=100, x_measured=0, p_coef=1.0, i_coef=1.0, d_coef=1.0, diff_out=0.0
    )
    assert (u, p, i, d) == (0.0, 0.0, 0.0, 0.0)
    assert (pid.p_part, pid.i_part, pid.d_part) == (0.0, 0.0, 0.0)


def test_zero_p_coef_returns_early_without_changing_state():
    pid = ChamberPID()
    pid.p_part, pid.i_part, pid.d_part = 0.3, 0.2, 0.1
    u, p, i, d = pid.step(
        enable=True, x_target=10, x_measured=0, p_coef=0.0, i_coef=1.0, d_coef=1.0, diff_out=0.5
    )
    assert u == 0.0
    assert (p, i, d) == (0.3, 0.2, 0.1)


def test_proportional_only_response():
    pid = ChamberPID(u_min=-1.0, u_max=1.0, pid_i_reverse_mul=0.333)
    p_coef, delta = 2.0, 2.0
    u, p, i, d = pid.step(
        enable=True,
        x_target=10.0,
        x_measured=8.0,
        p_coef=p_coef,
        i_coef=0.0,
        d_coef=0.0,
        diff_out=0.0,
    )
    expected_p = (1.0 / p_coef) * delta
    assert u == pytest.approx(expected_p)
    assert p == pytest.approx(expected_p)
    assert i == 0.0
    assert d == 0.0


def test_zero_error_leaves_i_part_unchanged_and_p_d_zero():
    pid = ChamberPID()
    u, p, i, d = pid.step(
        enable=True,
        x_target=5.0,
        x_measured=5.0,
        p_coef=2.0,
        i_coef=1.0,
        d_coef=0.0,
        diff_out=0.0,
    )
    assert (u, p, i, d) == (0.0, 0.0, 0.0, 0.0)


def test_derivative_response_to_known_diff_out():
    pid = ChamberPID(u_min=-1.0, u_max=1.0)
    p_coef, d_coef, diff_out = 2.0, 1.0, 0.4
    u, p, i, d = pid.step(
        enable=True,
        x_target=5.0,
        x_measured=5.0,
        p_coef=p_coef,
        i_coef=0.0,
        d_coef=d_coef,
        diff_out=diff_out,
    )
    expected_d = (1.0 / p_coef) * (d_coef * -diff_out)
    assert d == pytest.approx(expected_d)
    assert u == pytest.approx(expected_d)
    assert p == 0.0


def test_integral_accumulates_over_multiple_steps_same_sign_delta():
    pid = ChamberPID(u_min=-1.0, u_max=1.0, pid_i_reverse_mul=0.333)
    p_coef, i_coef = 2.0, 1.0
    delta = 1.0

    u1, p1, i1, d1 = pid.step(
        enable=True,
        x_target=10.0,
        x_measured=9.0,
        p_coef=p_coef,
        i_coef=i_coef,
        d_coef=0.0,
        diff_out=0.0,
    )
    expected_i1 = 0.0 + (1.0 / p_coef) * (delta * 0.1 / i_coef)
    expected_p = (1.0 / p_coef) * delta
    assert p1 == pytest.approx(expected_p)
    assert i1 == pytest.approx(expected_i1)
    assert u1 == pytest.approx(expected_p + expected_i1)

    u2, p2, i2, d2 = pid.step(
        enable=True,
        x_target=10.0,
        x_measured=9.0,
        p_coef=p_coef,
        i_coef=i_coef,
        d_coef=0.0,
        diff_out=0.0,
    )
    expected_i2 = expected_i1 + (1.0 / p_coef) * (delta * 0.1 / i_coef)
    assert i2 == pytest.approx(expected_i2)
    assert p2 == pytest.approx(expected_p)
    assert u2 == pytest.approx(expected_p + expected_i2)
    assert i2 > i1


def test_integral_reverse_gain_when_delta_opposes_accumulated_i_part():
    pid = ChamberPID(u_min=-1.0, u_max=1.0, pid_i_reverse_mul=0.333)
    p_coef, i_coef = 2.0, 1.0
    pid.step(
        enable=True,
        x_target=10.0,
        x_measured=9.0,
        p_coef=p_coef,
        i_coef=i_coef,
        d_coef=0.0,
        diff_out=0.0,
    )
    prior_i_part = pid.i_part
    assert prior_i_part == pytest.approx(0.05)

    u, p, i, d = pid.step(
        enable=True,
        x_target=9.0,
        x_measured=10.0,
        p_coef=p_coef,
        i_coef=i_coef,
        d_coef=0.0,
        diff_out=0.0,
    )
    delta = -1.0
    effective_i_coef = i_coef * 0.333
    expected_p = (1.0 / p_coef) * delta
    expected_i = prior_i_part + (1.0 / p_coef) * (delta * 0.1 / effective_i_coef)
    assert p == pytest.approx(expected_p)
    assert i == pytest.approx(expected_i)
    assert i < prior_i_part


def test_i_part_anti_windup_clips_to_u_min_u_max():
    pid = ChamberPID(u_min=-0.05, u_max=0.05, pid_i_reverse_mul=0.333)
    p_coef, i_coef = 2.0, 1.0
    for _ in range(5):
        _, _, i, _ = pid.step(
            enable=True,
            x_target=10.0,
            x_measured=9.0,
            p_coef=p_coef,
            i_coef=i_coef,
            d_coef=0.0,
            diff_out=0.0,
        )
        assert -0.05 <= i <= 0.05
    assert pid.i_part == pytest.approx(0.05)


def test_d_part_clips_to_fixed_point_four_band_regardless_of_u_bounds():
    pid = ChamberPID(u_min=-100.0, u_max=100.0)
    _, _, _, d = pid.step(
        enable=True,
        x_target=5.0,
        x_measured=5.0,
        p_coef=1.0,
        i_coef=0.0,
        d_coef=1000.0,
        diff_out=1.0,
    )
    assert d == -0.4


def test_d_part_clips_negative_direction_too():
    pid = ChamberPID(u_min=-100.0, u_max=100.0)
    _, _, _, d = pid.step(
        enable=True,
        x_target=5.0,
        x_measured=5.0,
        p_coef=1.0,
        i_coef=0.0,
        d_coef=1000.0,
        diff_out=-1.0,
    )
    assert d == 0.4


def test_u_output_clips_to_instance_u_min_u_max():
    pid = ChamberPID(u_min=-0.1, u_max=0.1)
    u, p, i, d = pid.step(
        enable=True,
        x_target=100.0,
        x_measured=0.0,
        p_coef=1.0,
        i_coef=0.0,
        d_coef=0.0,
        diff_out=0.0,
    )
    assert u == 0.1
    assert p == 0.1


def test_positive_and_negative_errors_are_antisymmetric_for_p_only():
    pid_pos = ChamberPID(u_min=-10.0, u_max=10.0)
    pid_neg = ChamberPID(u_min=-10.0, u_max=10.0)
    u_pos, p_pos, _, _ = pid_pos.step(
        enable=True,
        x_target=10.0,
        x_measured=8.0,
        p_coef=2.0,
        i_coef=0.0,
        d_coef=0.0,
        diff_out=0.0,
    )
    u_neg, p_neg, _, _ = pid_neg.step(
        enable=True,
        x_target=8.0,
        x_measured=10.0,
        p_coef=2.0,
        i_coef=0.0,
        d_coef=0.0,
        diff_out=0.0,
    )
    assert u_pos == pytest.approx(-u_neg)
    assert p_pos == pytest.approx(-p_neg)


def test_reset_is_reconstruction_or_explicit_field_assignment():
    pid = ChamberPID()
    pid.step(
        enable=True,
        x_target=1.0,
        x_measured=0.0,
        p_coef=1.0,
        i_coef=1.0,
        d_coef=1.0,
        diff_out=1.0,
    )
    assert pid.i_part != 0.0

    fresh = ChamberPID()
    assert (fresh.p_part, fresh.i_part, fresh.d_part) == (0.0, 0.0, 0.0)

    pid.p_part = pid.i_part = pid.d_part = 0.0
    assert (pid.p_part, pid.i_part, pid.d_part) == (0.0, 0.0, 0.0)


def test_repeated_execution_is_deterministic():
    def run():
        pid = ChamberPID(u_min=-1.0, u_max=1.0, pid_i_reverse_mul=0.333)
        results = []
        for x_measured in (9.0, 9.5, 8.7, 9.9, 9.0):
            results.append(
                pid.step(
                    enable=True,
                    x_target=10.0,
                    x_measured=x_measured,
                    p_coef=2.0,
                    i_coef=1.0,
                    d_coef=0.5,
                    diff_out=0.1,
                )
            )
        return results

    assert run() == run()
