"""Tests for app.services.chamber_session.ChamberSession's alarm evaluation
state machine (_evaluate_alarms): deadband (hysteresis) and delay (debounce)
behavior, and that trigger/clear transitions are persisted via
alarms_service. Alarm rules are scoped per-chamber (alarms_service.py's
chamber_id column) -- these tests use one fixed chamber throughout, so
scoping itself isn't what's under test here.

time.monotonic() is monkeypatched to a controllable fake clock so delay_s
behavior can be tested deterministically without real sleeps.
"""

import pytest

from app.services import alarms_service
from app.services.chamber_session import ChamberSession

pytestmark = pytest.mark.ui

_CHAMBER = {"id": 1, "name": "Chamber 1", "host": "127.0.0.1", "port": 5555}


@pytest.fixture
def session(deepvac_data_dir, qapp, qtbot):
    return ChamberSession(dict(_CHAMBER))


@pytest.fixture
def fake_clock(monkeypatch):
    # ChamberSession._evaluate_alarms() does `import time` at module level
    # in chamber_session.py -- patching the real time module's monotonic
    # reaches it since it's the same process-wide singleton object.
    import time

    state = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])
    return state


def _rule(**overrides):
    base = {
        "id": 1,
        "name": "High Temp",
        "variable": "temp",
        "condition": "above",
        "value": 80.0,
        "value2": None,
        "severity": "Critical",
        "deadband": 0.0,
        "delay_s": 0.0,
        "chamber_id": _CHAMBER["id"],
        "chamber_name": _CHAMBER["name"],
        "_active": False,
        "_last_value": None,
        "_condition_since": None,
        "_event_id": None,
    }
    base.update(overrides)
    return base


def _add_rule(**kwargs):
    defaults = dict(
        name="High Temp",
        variable="temp",
        condition="above",
        value=80.0,
        value2=None,
        severity="Critical",
        chamber_id=_CHAMBER["id"],
        chamber_name=_CHAMBER["name"],
    )
    defaults.update(kwargs)
    return alarms_service.add_rule(
        defaults["name"],
        defaults["variable"],
        defaults["condition"],
        defaults["value"],
        defaults["value2"],
        defaults["severity"],
        deadband=defaults.get("deadband", 0.0),
        delay_s=defaults.get("delay_s", 0.0),
        chamber_id=defaults["chamber_id"],
        chamber_name=defaults["chamber_name"],
    )


def test_above_condition_triggers_immediately_with_no_delay(deepvac_data_dir, session):
    rule = _add_rule()
    session.alarms = [_rule(id=rule["id"])]

    session._evaluate_alarms({"temp": 85.0})

    assert session.alarms[0]["_active"] is True
    events = alarms_service.list_events()
    assert len(events) == 1
    assert events[0]["trigger_value"] == 85.0
    assert events[0]["chamber_name"] == "Chamber 1"


def test_below_threshold_never_triggers(deepvac_data_dir, session):
    rule = _add_rule()
    session.alarms = [_rule(id=rule["id"])]

    session._evaluate_alarms({"temp": 50.0})

    assert session.alarms[0]["_active"] is False
    assert alarms_service.list_events() == []


def test_delay_suppresses_trigger_until_elapsed(deepvac_data_dir, session, fake_clock):
    rule = _add_rule(delay_s=10.0)
    session.alarms = [_rule(id=rule["id"], delay_s=10.0)]

    fake_clock["t"] = 0.0
    session._evaluate_alarms({"temp": 85.0})
    assert session.alarms[0]["_active"] is False  # condition just started

    fake_clock["t"] = 5.0
    session._evaluate_alarms({"temp": 85.0})
    assert session.alarms[0]["_active"] is False  # delay not yet elapsed

    fake_clock["t"] = 10.0
    session._evaluate_alarms({"temp": 85.0})
    assert session.alarms[0]["_active"] is True  # delay elapsed
    assert len(alarms_service.list_events()) == 1


def test_delay_resets_if_condition_stops_holding(deepvac_data_dir, session, fake_clock):
    rule = _add_rule(delay_s=10.0)
    session.alarms = [_rule(id=rule["id"], delay_s=10.0)]

    fake_clock["t"] = 0.0
    session._evaluate_alarms({"temp": 85.0})
    fake_clock["t"] = 5.0
    session._evaluate_alarms({"temp": 50.0})  # drops below threshold -- resets the timer
    fake_clock["t"] = 8.0
    session._evaluate_alarms({"temp": 85.0})  # condition restarts here, not at t=0

    assert session.alarms[0]["_active"] is False
    assert alarms_service.list_events() == []


def test_deadband_prevents_premature_clear(deepvac_data_dir, session):
    rule = _add_rule(deadband=5.0)
    session.alarms = [_rule(id=rule["id"], deadband=5.0)]

    session._evaluate_alarms({"temp": 85.0})
    assert session.alarms[0]["_active"] is True

    # Value drops back below the raw threshold (80) but stays within the
    # deadband zone (must go below 80 - 5 = 75 to actually clear).
    session._evaluate_alarms({"temp": 78.0})
    assert session.alarms[0]["_active"] is True

    session._evaluate_alarms({"temp": 70.0})
    assert session.alarms[0]["_active"] is False
    events = alarms_service.list_events()
    assert events[0]["cleared_at"] is not None


def test_outside_range_condition(deepvac_data_dir, session):
    rule = _add_rule(variable="pressure", condition="outside range", value=10.0, value2=20.0)
    session.alarms = [
        _rule(
            id=rule["id"], variable="pressure", condition="outside range", value=10.0, value2=20.0
        )
    ]

    session._evaluate_alarms({"pressure": 5.0})
    assert session.alarms[0]["_active"] is True

    session._evaluate_alarms({"pressure": 15.0})
    assert session.alarms[0]["_active"] is False


def test_non_numeric_sample_value_is_skipped_without_error(deepvac_data_dir, session):
    rule = _add_rule()
    session.alarms = [_rule(id=rule["id"])]

    session._evaluate_alarms({"temp": None})  # must not raise
    assert session.alarms[0]["_active"] is False


def test_delete_alarm_removes_rule_from_persistence(deepvac_data_dir, session):
    rule = _add_rule()
    session.alarms = [_rule(id=rule["id"])]

    session.delete_alarm_rule(0)

    assert session.alarms == []
    assert alarms_service.list_rules() == []


def test_alarm_rules_are_scoped_per_chamber(deepvac_data_dir, session):
    other_chamber = {"id": 2, "name": "Chamber 2", "host": "127.0.0.1", "port": 5556}
    _add_rule()  # belongs to _CHAMBER (id=1)
    _add_rule(chamber_id=other_chamber["id"], chamber_name=other_chamber["name"], name="Other")

    assert len(alarms_service.list_rules(chamber_id=_CHAMBER["id"])) == 1
    assert len(alarms_service.list_rules(chamber_id=other_chamber["id"])) == 1
    assert len(alarms_service.list_rules()) == 2
