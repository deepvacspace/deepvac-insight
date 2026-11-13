"""Tests of derived variables."""

import numpy as np
import pytest

from app.services import derived_variables_service as dv

pytestmark = pytest.mark.integration


def test_add_and_list_derived_variable(deepvac_data_dir):
    dv.add_derived_variable(
        "temperature_error", dv.TYPE_DIFFERENCE, source_channel="temp_ref", source_channel2="temp"
    )
    items = dv.list_derived_variables()
    assert len(items) == 1
    assert items[0]["name"] == "temperature_error"
    assert items[0]["type"] == dv.TYPE_DIFFERENCE


def test_duplicate_name_raises(deepvac_data_dir):
    dv.add_derived_variable("heating_rate", dv.TYPE_RATE_OF_CHANGE, source_channel="temp")
    with pytest.raises(dv.DerivedVariableError):
        dv.add_derived_variable("heating_rate", dv.TYPE_RATE_OF_CHANGE, source_channel="temp")


def test_difference_requires_two_source_channels(deepvac_data_dir):
    with pytest.raises(dv.DerivedVariableError):
        dv.add_derived_variable("bad", dv.TYPE_DIFFERENCE, source_channel="temp")


def test_rolling_std_requires_window_of_at_least_two(deepvac_data_dir):
    with pytest.raises(dv.DerivedVariableError):
        dv.add_derived_variable("bad", dv.TYPE_ROLLING_STD, source_channel="temp", window=1)


def test_custom_requires_valid_expression(deepvac_data_dir):
    with pytest.raises(dv.DerivedVariableError):
        dv.add_derived_variable("bad", dv.TYPE_CUSTOM, expression="1 +")


def test_delete_derived_variable(deepvac_data_dir):
    created = dv.add_derived_variable(
        "temperature_error", dv.TYPE_DIFFERENCE, source_channel="temp_ref", source_channel2="temp"
    )
    dv.delete_derived_variable(created["id"])
    assert dv.list_derived_variables() == []


def test_required_channels_for_difference():
    definition = {"type": dv.TYPE_DIFFERENCE, "source_channel": "a", "source_channel2": "b"}
    assert dv.required_channels(definition) == {"a", "b"}


def test_required_channels_for_custom_expression():
    definition = {"type": dv.TYPE_CUSTOM, "expression": "a + b - c"}
    assert dv.required_channels(definition) == {"a", "b", "c"}


def test_missing_channels_detects_unavailable_columns():
    definition = {"type": dv.TYPE_DIFFERENCE, "source_channel": "a", "source_channel2": "b"}
    assert dv.missing_channels(definition, ["a"]) == {"b"}
    assert dv.missing_channels(definition, ["a", "b"]) == set()


def test_compute_series_difference():
    definition = {
        "name": "temperature_error",
        "type": dv.TYPE_DIFFERENCE,
        "source_channel": "temp_ref",
        "source_channel2": "temp",
    }
    columns_data = {
        "temp_ref": np.array([60.0, 60.0, 60.0]),
        "temp": np.array([50.0, 55.0, 58.0]),
    }
    result = dv.compute_series(definition, columns_data, np.array([0.0, 1.0, 2.0]))
    np.testing.assert_allclose(result, [10.0, 5.0, 2.0])


def test_compute_series_rate_of_change():
    definition = {"name": "heating_rate", "type": dv.TYPE_RATE_OF_CHANGE, "source_channel": "temp"}
    columns_data = {"temp": np.array([20.0, 22.0, 26.0, 26.0])}
    elapsed = np.array([0.0, 1.0, 2.0, 3.0])
    result = dv.compute_series(definition, columns_data, elapsed)
    np.testing.assert_allclose(result[1:], [2.0, 4.0, 0.0])


def test_compute_series_rolling_std():
    definition = {
        "name": "rolling_std",
        "type": dv.TYPE_ROLLING_STD,
        "source_channel": "temp",
        "window": 3,
    }
    columns_data = {"temp": np.array([1.0, 2.0, 3.0, 4.0])}
    result = dv.compute_series(definition, columns_data, np.array([0.0, 1.0, 2.0, 3.0]))
    assert np.isnan(result[0])
    assert result[1] == pytest.approx(float(np.std([1.0, 2.0], ddof=1)))
    assert result[2] == pytest.approx(float(np.std([1.0, 2.0, 3.0], ddof=1)))
    assert result[3] == pytest.approx(float(np.std([2.0, 3.0, 4.0], ddof=1)))


def test_compute_series_cumulative_integral():
    definition = {
        "name": "energy_use",
        "type": dv.TYPE_CUMULATIVE_INTEGRAL,
        "source_channel": "power",
    }
    columns_data = {"power": np.array([2.0, 2.0, 2.0])}
    elapsed = np.array([0.0, 1.0, 2.0])
    result = dv.compute_series(definition, columns_data, elapsed)
    np.testing.assert_allclose(result, [0.0, 2.0, 4.0])


def test_compute_series_custom_expression():
    definition = {
        "name": "control_effort",
        "type": dv.TYPE_CUSTOM,
        "expression": "temp_u_p + temp_u_i",
    }
    columns_data = {
        "temp_u_p": np.array([1.0, 2.0]),
        "temp_u_i": np.array([0.5, 0.5]),
    }
    result = dv.compute_series(definition, columns_data, np.array([0.0, 1.0]))
    np.testing.assert_allclose(result, [1.5, 2.5])


def test_compute_series_custom_expression_with_whitelisted_function():
    definition = {"name": "rms_error", "type": dv.TYPE_CUSTOM, "expression": "sqrt(abs(temp))"}
    assert dv.required_channels(definition) == {"temp"}
    columns_data = {"temp": np.array([-16.0, 4.0])}
    result = dv.compute_series(definition, columns_data, np.array([0.0, 1.0]))
    np.testing.assert_allclose(result, [4.0, 2.0])


def test_compute_series_raises_on_missing_channel():
    definition = {
        "name": "temperature_error",
        "type": dv.TYPE_DIFFERENCE,
        "source_channel": "temp_ref",
        "source_channel2": "temp",
    }
    columns_data = {"temp_ref": np.array([60.0])}
    with pytest.raises(dv.DerivedVariableError):
        dv.compute_series(definition, columns_data, np.array([0.0]))
