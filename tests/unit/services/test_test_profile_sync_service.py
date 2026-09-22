"""Tests for test_profile_sync_service's push/pull/conflict logic, against
a fake in-memory hub standing in for licensing_client.signed_request_json."""

import uuid

import pytest

from app.services import licensing_client
from app.services import test_profile_sync_service as sync_service
from app.services import test_profiles_service as profiles

pytestmark = pytest.mark.unit


def _steps():
    return [
        {"setpoint_temp": 25.0, "setpoint_pressure": None, "duration_s": 60.0, "label": "warm-up"},
        {"setpoint_temp": 85.0, "setpoint_pressure": None, "duration_s": 300.0, "label": "hold"},
    ]


class FakeHub:
    def __init__(self):
        self.profiles = {}

    def _wire(self, profile_id, payload):
        return {
            "id": profile_id,
            "name": payload["name"],
            "description": payload["description"],
            "steps": payload["steps"],
        }

    def request(self, method, url, payload=None):
        if method == "GET":
            return {"profiles": list(self.profiles.values())}
        if method == "POST" and url.endswith("/test-profiles"):
            profile_id = str(uuid.uuid4())
            wired = self._wire(profile_id, payload)
            self.profiles[profile_id] = wired
            return wired
        if method == "POST" and "/replace" in url:
            profile_id = url.rsplit("/", 2)[-2]
            wired = self._wire(profile_id, payload)
            self.profiles[profile_id] = wired
            return wired
        raise AssertionError(f"Unexpected request: {method} {url}")

    def seed(self, name, description, steps):
        profile_id = str(uuid.uuid4())
        self.profiles[profile_id] = self._wire(
            profile_id, {"name": name, "description": description, "steps": steps}
        )
        return profile_id


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: "org-1")
    monkeypatch.setattr(licensing_client, "signed_request_json", fake.request)
    return fake


def test_sync_requires_a_license(deepvac_data_dir, monkeypatch):
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: None)
    with pytest.raises(sync_service.SyncError):
        sync_service.sync()


def test_sync_pushes_local_only_profile(deepvac_data_dir, hub):
    profiles.add_profile("Thermal Test A", "", _steps())

    result = sync_service.sync()

    assert result.pushed == ["Thermal Test A"]
    assert result.pulled == []
    assert result.conflicts == []
    local = profiles.list_profiles()[0]
    assert local["hub_id"] in hub.profiles


def test_sync_pulls_hub_only_profile(deepvac_data_dir, hub):
    hub.seed("Hub Profile", "", _steps())

    result = sync_service.sync()

    assert result.pulled == ["Hub Profile"]
    assert result.pushed == []
    local = profiles.list_profiles()[0]
    assert local["created_by"] == "DeepVac Hub"


def test_sync_leaves_identical_profiles_alone(deepvac_data_dir, hub):
    hub_id = hub.seed("Same Everywhere", "", _steps())
    profiles.add_profile("Same Everywhere", "", _steps(), hub_id=hub_id)

    result = sync_service.sync()

    assert result.pushed == []
    assert result.pulled == []
    assert result.conflicts == []


def test_sync_reports_conflict_when_steps_differ(deepvac_data_dir, hub):
    hub_id = hub.seed("Diverged", "", _steps())
    local = profiles.add_profile(
        "Diverged",
        "",
        [{"setpoint_temp": 1.0, "setpoint_pressure": None, "duration_s": 5.0, "label": "x"}],
        hub_id=hub_id,
    )

    result = sync_service.sync()

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.profile_id == local["id"]
    assert conflict.hub_id == hub_id


def test_resolve_conflict_keep_local_overwrites_hub(deepvac_data_dir, hub):
    hub_id = hub.seed("Diverged", "", _steps())
    local = profiles.add_profile(
        "Diverged",
        "",
        [{"setpoint_temp": 1.0, "setpoint_pressure": None, "duration_s": 5.0, "label": "x"}],
        hub_id=hub_id,
    )
    conflict = sync_service.sync().conflicts[0]

    sync_service.resolve_conflict(conflict, "local")

    assert hub.profiles[hub_id]["steps"][0]["label"] == "x"
    assert profiles.get_profile(local["id"])["hub_synced_at"] is not None


def test_resolve_conflict_keep_hub_overwrites_local(deepvac_data_dir, hub):
    hub_id = hub.seed("Diverged", "", _steps())
    local = profiles.add_profile(
        "Diverged",
        "",
        [{"setpoint_temp": 1.0, "setpoint_pressure": None, "duration_s": 5.0, "label": "x"}],
        hub_id=hub_id,
    )
    conflict = sync_service.sync().conflicts[0]

    sync_service.resolve_conflict(conflict, "hub")

    updated = profiles.get_profile(local["id"])
    assert [s["label"] for s in updated["steps"]] == ["warm-up", "hold"]
