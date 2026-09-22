"""Tests for alarm_rule_sync_service's push/pull/conflict logic, against a
fake in-memory hub standing in for licensing_client.signed_request_json."""

import uuid

import pytest

from app.services import alarm_rule_sync_service as sync_service
from app.services import alarms_service, licensing_client

pytestmark = pytest.mark.unit

_HUB_CHAMBER_ID = "hub-chamber-1"
_OTHER_HUB_CHAMBER_ID = "hub-chamber-2"


def _local_chamber(chamber_id=1, hub_id=_HUB_CHAMBER_ID):
    return {
        "id": chamber_id,
        "name": "Chamber 1",
        "host": "127.0.0.1",
        "port": 5555,
        "hub_id": hub_id,
    }


def _add_local_rule(chamber, **overrides):
    kwargs = {
        "name": "High Temp",
        "variable": "temp",
        "condition": "above",
        "value": 85.0,
        "value2": None,
        "severity": "Critical",
        "deadband": 0.5,
        "delay_s": 10.0,
        "created_by": "Test User",
        "chamber_id": chamber["id"],
        "chamber_name": chamber["name"],
    }
    kwargs.update(overrides)
    return alarms_service.add_rule(
        kwargs.pop("name"),
        kwargs.pop("variable"),
        kwargs.pop("condition"),
        kwargs.pop("value"),
        kwargs.pop("value2"),
        kwargs.pop("severity"),
        **kwargs,
    )


class FakeHub:
    def __init__(self):
        self.rules = {}

    def _wire(self, rule_id, payload):
        return {"id": rule_id, **payload}

    def request(self, method, url, payload=None):
        if method == "GET":
            return {"alarm_rules": list(self.rules.values())}
        if method == "POST" and url.endswith("/alarm-rules"):
            rule_id = str(uuid.uuid4())
            wired = self._wire(rule_id, payload)
            self.rules[rule_id] = wired
            return wired
        if method == "POST" and "/replace" in url:
            rule_id = url.rsplit("/", 2)[-2]
            wired = self._wire(rule_id, payload)
            self.rules[rule_id] = wired
            return wired
        raise AssertionError(f"Unexpected request: {method} {url}")

    def seed(self, chamber_id=_HUB_CHAMBER_ID, **overrides):
        payload = {
            "chamber_id": chamber_id,
            "name": "High Temp",
            "variable": "temp",
            "condition": "above",
            "value": 85.0,
            "value2": None,
            "severity": "Critical",
            "deadband": 0.5,
            "delay_s": 10.0,
            "enabled": True,
        }
        payload.update(overrides)
        rule_id = str(uuid.uuid4())
        self.rules[rule_id] = self._wire(rule_id, payload)
        return rule_id


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: "org-1")
    monkeypatch.setattr(licensing_client, "signed_request_json", fake.request)
    return fake


def test_sync_requires_a_license(deepvac_data_dir, monkeypatch):
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: None)
    with pytest.raises(sync_service.SyncError):
        sync_service.sync(_local_chamber())


def test_sync_requires_chamber_to_already_be_synced(deepvac_data_dir, hub):
    with pytest.raises(sync_service.SyncError):
        sync_service.sync(_local_chamber(hub_id=None))


def test_sync_pushes_local_only_rule(deepvac_data_dir, hub):
    chamber = _local_chamber()
    _add_local_rule(chamber)

    result = sync_service.sync(chamber)

    assert result.pushed == ["High Temp"]
    assert result.conflicts == []
    local = alarms_service.list_rules(chamber_id=chamber["id"])[0]
    assert local["hub_id"] in hub.rules


def test_sync_pulls_hub_only_rule_for_this_chamber(deepvac_data_dir, hub):
    chamber = _local_chamber()
    hub.seed(chamber_id=_HUB_CHAMBER_ID, name="Low Pressure", variable="pressure")

    result = sync_service.sync(chamber)

    assert result.pulled == ["Low Pressure"]
    local = alarms_service.list_rules(chamber_id=chamber["id"])[0]
    assert local["created_by"] == "DeepVac Hub"


def test_sync_ignores_rules_from_other_chambers(deepvac_data_dir, hub):
    chamber = _local_chamber()
    hub.seed(chamber_id=_OTHER_HUB_CHAMBER_ID, name="Not Mine")

    result = sync_service.sync(chamber)

    assert result.pulled == []
    assert alarms_service.list_rules(chamber_id=chamber["id"]) == []


def test_sync_leaves_identical_rules_alone(deepvac_data_dir, hub):
    chamber = _local_chamber()
    hub_id = hub.seed(chamber_id=_HUB_CHAMBER_ID)
    _add_local_rule(chamber, hub_id=hub_id)

    result = sync_service.sync(chamber)

    assert result.pushed == []
    assert result.pulled == []
    assert result.conflicts == []


def test_sync_reports_conflict_when_value_differs(deepvac_data_dir, hub):
    chamber = _local_chamber()
    hub_id = hub.seed(chamber_id=_HUB_CHAMBER_ID, value=85.0)
    local = _add_local_rule(chamber, value=90.0, hub_id=hub_id)

    result = sync_service.sync(chamber)

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.rule_id == local["id"]
    assert conflict.hub_id == hub_id


def test_resolve_conflict_keep_local_overwrites_hub(deepvac_data_dir, hub):
    chamber = _local_chamber()
    hub_id = hub.seed(chamber_id=_HUB_CHAMBER_ID, value=85.0)
    local = _add_local_rule(chamber, value=90.0, hub_id=hub_id)
    conflict = sync_service.sync(chamber).conflicts[0]

    sync_service.resolve_conflict(conflict, "local", chamber)

    assert hub.rules[hub_id]["value"] == 90.0
    updated = [
        r for r in alarms_service.list_rules(chamber_id=chamber["id"]) if r["id"] == local["id"]
    ][0]
    assert updated["hub_synced_at"] is not None


def test_resolve_conflict_keep_hub_overwrites_local(deepvac_data_dir, hub):
    chamber = _local_chamber()
    hub_id = hub.seed(chamber_id=_HUB_CHAMBER_ID, value=85.0)
    local = _add_local_rule(chamber, value=90.0, hub_id=hub_id)
    conflict = sync_service.sync(chamber).conflicts[0]

    sync_service.resolve_conflict(conflict, "hub", chamber)

    updated = [
        r for r in alarms_service.list_rules(chamber_id=chamber["id"]) if r["id"] == local["id"]
    ][0]
    assert updated["value"] == 85.0
