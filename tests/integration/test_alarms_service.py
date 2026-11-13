"""Tests of alarm rules and alarm history."""

import csv

import pytest

from app.services import alarms_service as alarms

pytestmark = pytest.mark.integration


def test_add_and_list_rule(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    rules = alarms.list_rules()
    assert len(rules) == 1
    assert rules[0]["id"] == rule["id"]
    assert rules[0]["name"] == "High Temp"
    assert rules[0]["deadband"] == 0.0
    assert rules[0]["delay_s"] == 0.0
    assert rules[0]["enabled"] is True


def test_add_rule_with_deadband_and_delay(deepvac_data_dir):
    rule = alarms.add_rule(
        "High Temp", "temp", "above", 80.0, None, "Warning", deadband=2.0, delay_s=5.0
    )
    assert rule["deadband"] == 2.0
    assert rule["delay_s"] == 5.0


def test_delete_rule(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    alarms.delete_rule(rule["id"])
    assert alarms.list_rules() == []


def test_rules_survive_a_fresh_connection(deepvac_data_dir):
    alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    rules_again = alarms.list_rules()
    assert len(rules_again) == 1


def test_record_trigger_and_list_events(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    event_id = alarms.record_trigger(rule, 85.0)
    events = alarms.list_events()
    assert len(events) == 1
    assert events[0]["id"] == event_id
    assert events[0]["rule_name"] == "High Temp"
    assert events[0]["trigger_value"] == 85.0
    assert events[0]["cleared_at"] is None
    assert events[0]["acknowledged_at"] is None


def test_record_clear_sets_cleared_at(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    event_id = alarms.record_trigger(rule, 85.0)
    alarms.record_clear(event_id)
    event = alarms.list_events()[0]
    assert event["cleared_at"] is not None


def test_record_clear_does_not_overwrite_an_existing_cleared_at(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    event_id = alarms.record_trigger(rule, 85.0)
    alarms.record_clear(event_id)
    first_cleared_at = alarms.list_events()[0]["cleared_at"]
    alarms.record_clear(event_id)
    assert alarms.list_events()[0]["cleared_at"] == first_cleared_at


def test_record_clear_with_none_event_id_is_a_noop(deepvac_data_dir):
    alarms.record_clear(None)


def test_acknowledge_event(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    event_id = alarms.record_trigger(rule, 85.0)
    alarms.acknowledge_event(event_id, "Alice", "Checked, false alarm")
    event = alarms.list_events()[0]
    assert event["acknowledged_by"] == "Alice"
    assert event["comment"] == "Checked, false alarm"
    assert event["acknowledged_at"] is not None


def test_list_events_ordered_most_recent_first(deepvac_data_dir):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    first_id = alarms.record_trigger(rule, 85.0)
    second_id = alarms.record_trigger(rule, 90.0)
    events = alarms.list_events()
    assert events[0]["id"] == second_id
    assert events[1]["id"] == first_id


def test_export_events_csv(deepvac_data_dir, tmp_path):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    event_id = alarms.record_trigger(rule, 85.0)
    alarms.acknowledge_event(event_id, "Alice", "Note, with a comma")

    out_path = tmp_path / "alarm_log.csv"
    alarms.export_events_csv(out_path)

    with open(out_path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["rule_name"] == "High Temp"
    assert rows[0]["comment"] == "Note, with a comma"


def test_export_events_csv_accepts_explicit_events_list(deepvac_data_dir, tmp_path):
    rule = alarms.add_rule("High Temp", "temp", "above", 80.0, None, "Critical")
    alarms.record_trigger(rule, 85.0)
    events = alarms.list_events()

    out_path = tmp_path / "alarm_log.csv"
    alarms.export_events_csv(out_path, events)

    with open(out_path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
