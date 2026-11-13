"""Tests of CodesysDiff."""

import pytest

from app.model.simulation import CodesysDiff

pytestmark = pytest.mark.unit


def test_initial_state():
    diff = CodesysDiff()
    assert diff.dc == 0.995
    assert diff.prev_value == 0.0
    assert diff.filter_out == 0.0
    assert diff.out == 0.0


def test_first_sample_diff_value_is_value_minus_zero():
    diff = CodesysDiff(dc=0.995)
    out = diff.update(2.0)
    expected = 10.0 * min(max(0.995 * 0.0 + 0.005 * 2.0, -5.0), 5.0)
    assert out == pytest.approx(expected)
    assert out == pytest.approx(0.1)
    assert diff.prev_value == 2.0


def test_constant_input_decays_toward_zero_after_initial_step():
    diff = CodesysDiff(dc=0.995)
    out1 = diff.update(2.0)
    out2 = diff.update(2.0)
    out3 = diff.update(2.0)
    expected_filter_out_1 = 0.005 * 2.0
    expected_filter_out_2 = 0.995 * expected_filter_out_1
    expected_filter_out_3 = 0.995 * expected_filter_out_2
    assert out1 == pytest.approx(10.0 * expected_filter_out_1)
    assert out2 == pytest.approx(10.0 * expected_filter_out_2)
    assert out3 == pytest.approx(10.0 * expected_filter_out_3)
    assert abs(out3) < abs(out2) < abs(out1)


def test_linear_ramp_converges_toward_steady_state_of_the_step_size():
    diff = CodesysDiff(dc=0.995)
    filter_outs = []
    value = 0.0
    for _ in range(50):
        value += 1.0
        diff.update(value)
        filter_outs.append(diff.filter_out)
    assert filter_outs == sorted(filter_outs)
    assert filter_outs[-1] < 1.0
    assert filter_outs[-1] > filter_outs[0]


def test_positive_step_clamps_filter_in_to_five():
    diff = CodesysDiff(dc=0.995)
    out = diff.update(100.0)
    expected_filter_out = 0.995 * 0.0 + 0.005 * 5.0
    assert out == pytest.approx(10.0 * expected_filter_out)
    assert out == pytest.approx(0.25)


def test_negative_step_clamps_filter_in_to_negative_five():
    diff = CodesysDiff(dc=0.995)
    out = diff.update(-100.0)
    expected_filter_out = 0.995 * 0.0 + 0.005 * (-5.0)
    assert out == pytest.approx(10.0 * expected_filter_out)
    assert out == pytest.approx(-0.25)


def test_reset_is_reconstruction_or_explicit_field_assignment():
    diff = CodesysDiff()
    diff.update(50.0)
    assert diff.prev_value != 0.0

    fresh = CodesysDiff()
    assert (fresh.prev_value, fresh.filter_out, fresh.out) == (0.0, 0.0, 0.0)

    diff.prev_value = 0.0
    diff.filter_out = 0.0
    diff.out = 0.0
    assert (diff.prev_value, diff.filter_out, diff.out) == (0.0, 0.0, 0.0)


def test_numerical_stability_over_a_realistic_sequence():
    diff = CodesysDiff(dc=0.995)
    temps = [27.0 + (60.0 - 27.0) * (i / 29) for i in range(30)]
    for t in temps:
        out = diff.update(t)
        assert -50.0 <= out <= 50.0
        assert out == out
        assert abs(out) != float("inf")


def test_repeated_execution_is_deterministic():
    def run():
        diff = CodesysDiff(dc=0.995)
        return [diff.update(v) for v in (1.0, 3.0, -2.0, 0.0, 5.5, -10.0)]

    assert run() == run()


def test_custom_dc_changes_filter_speed():
    fast = CodesysDiff(dc=0.5)
    slow = CodesysDiff(dc=0.995)
    out_fast = fast.update(2.0)
    out_slow = slow.update(2.0)
    assert abs(out_fast) > abs(out_slow)
