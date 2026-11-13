"""Tests of the expression evaluator."""

import numpy as np
import pytest

from app.services.safe_eval import SafeEvalError, evaluate, referenced_names

pytestmark = pytest.mark.unit


def test_basic_arithmetic():
    assert evaluate("1 + 2 * 3", {}) == 7


def test_variable_reference():
    result = evaluate("temp_ref - temp", {"temp_ref": 60.0, "temp": 45.0})
    assert result == 15.0


def test_array_variables_broadcast():
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([0.5, 0.5, 0.5])
    result = evaluate("a - b", {"a": a, "b": b})
    np.testing.assert_allclose(result, [0.5, 1.5, 2.5])


def test_unary_minus():
    assert evaluate("-x", {"x": 5.0}) == -5.0


def test_whitelisted_function_call():
    result = evaluate("sqrt(abs(x))", {"x": -16.0})
    assert result == 4.0


def test_power_and_mod():
    assert evaluate("2 ** 3", {}) == 8
    assert evaluate("7 % 3", {}) == 1


def test_unknown_variable_raises():
    with pytest.raises(SafeEvalError):
        evaluate("missing_var + 1", {"known": 1.0})


def test_unsupported_function_call_raises():
    with pytest.raises(SafeEvalError):
        evaluate("__import__('os')", {})


def test_attribute_access_raises():
    with pytest.raises(SafeEvalError):
        evaluate("x.__class__", {"x": 1.0})


def test_open_call_raises():
    with pytest.raises(SafeEvalError):
        evaluate("open('secrets.txt')", {})


def test_subscript_raises():
    with pytest.raises(SafeEvalError):
        evaluate("x[0]", {"x": [1, 2, 3]})


def test_keyword_arguments_raise():
    with pytest.raises(SafeEvalError):
        evaluate("max(1, 2, key=abs)", {})


def test_invalid_syntax_raises():
    with pytest.raises(SafeEvalError):
        evaluate("1 +", {})


def test_string_constant_raises():
    with pytest.raises(SafeEvalError):
        evaluate("'hello'", {})


def test_referenced_names_finds_all_variables():
    names = referenced_names("temp_u_p + temp_u_i + temp_u_d - offset")
    assert names == {"temp_u_p", "temp_u_i", "temp_u_d", "offset"}


def test_referenced_names_ignores_function_names():
    names = referenced_names("sqrt(x) + abs(y)")
    assert names == {"x", "y"}


def test_referenced_names_invalid_syntax_raises():
    with pytest.raises(SafeEvalError):
        referenced_names("1 +")
