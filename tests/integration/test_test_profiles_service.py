"""Tests of test profiles."""

import pytest

from app.services import test_profiles_service as profiles

pytestmark = pytest.mark.integration


def _steps():
    return [
        {"setpoint_temp": 25.0, "setpoint_pressure": None, "duration_s": 60.0, "label": "Ramp up"},
        {"setpoint_temp": 80.0, "setpoint_pressure": 1.5, "duration_s": 300.0, "label": "Soak"},
        {"setpoint_temp": None, "setpoint_pressure": 0.0, "duration_s": 30.0, "label": "Vent"},
    ]


def test_add_and_list_profile(deepvac_data_dir):
    profiles.add_profile("Thermal Soak A", "Standard soak test", _steps())
    all_profiles = profiles.list_profiles()
    assert len(all_profiles) == 1
    assert all_profiles[0]["name"] == "Thermal Soak A"
    assert len(all_profiles[0]["steps"]) == 3


def test_steps_are_returned_in_order(deepvac_data_dir):
    created = profiles.add_profile("Thermal Soak A", "", _steps())
    labels = [s["label"] for s in created["steps"]]
    assert labels == ["Ramp up", "Soak", "Vent"]


def test_add_profile_empty_name_raises(deepvac_data_dir):
    with pytest.raises(profiles.TestProfileError):
        profiles.add_profile("   ", "", _steps())


def test_add_profile_no_steps_raises(deepvac_data_dir):
    with pytest.raises(profiles.TestProfileError):
        profiles.add_profile("Empty", "", [])


def test_add_profile_step_with_no_setpoints_raises(deepvac_data_dir):
    steps = [{"setpoint_temp": None, "setpoint_pressure": None, "duration_s": 10.0, "label": ""}]
    with pytest.raises(profiles.TestProfileError):
        profiles.add_profile("Bad", "", steps)


def test_add_profile_step_with_zero_duration_raises(deepvac_data_dir):
    steps = [{"setpoint_temp": 50.0, "setpoint_pressure": None, "duration_s": 0.0, "label": ""}]
    with pytest.raises(profiles.TestProfileError):
        profiles.add_profile("Bad", "", steps)


def test_add_profile_duplicate_name_raises(deepvac_data_dir):
    profiles.add_profile("Thermal Soak A", "", _steps())
    with pytest.raises(profiles.TestProfileError):
        profiles.add_profile("Thermal Soak A", "", _steps())


def test_update_profile_replaces_steps(deepvac_data_dir):
    created = profiles.add_profile("Thermal Soak A", "", _steps())
    new_steps = [
        {"setpoint_temp": 10.0, "setpoint_pressure": None, "duration_s": 5.0, "label": "Only step"}
    ]
    updated = profiles.update_profile(created["id"], "Thermal Soak A", "updated", new_steps)
    assert updated["description"] == "updated"
    assert len(updated["steps"]) == 1
    assert updated["steps"][0]["label"] == "Only step"


def test_update_profile_missing_id_raises(deepvac_data_dir):
    with pytest.raises(profiles.TestProfileError):
        profiles.update_profile(999, "Nope", "", _steps())


def test_delete_profile_removes_steps_too(deepvac_data_dir):
    created = profiles.add_profile("Thermal Soak A", "", _steps())
    profiles.delete_profile(created["id"])
    assert profiles.list_profiles() == []
    assert profiles.get_profile(created["id"]) is None


def test_get_profile_missing_returns_none(deepvac_data_dir):
    assert profiles.get_profile(999) is None


def test_total_duration_s(deepvac_data_dir):
    created = profiles.add_profile("Thermal Soak A", "", _steps())
    assert profiles.total_duration_s(created) == 60.0 + 300.0 + 30.0


def test_profiles_survive_a_fresh_connection(deepvac_data_dir):
    profiles.add_profile("Thermal Soak A", "", _steps())
    again = profiles.list_profiles()
    assert len(again) == 1
    assert len(again[0]["steps"]) == 3
