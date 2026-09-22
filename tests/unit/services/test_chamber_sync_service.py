"""Tests for chamber_sync_service's push/pull/conflict logic, against a
fake in-memory hub standing in for licensing_client.signed_request_json."""

import uuid

import pytest

from app.services import chamber_sync_service as sync_service
from app.services import chambers_service, licensing_client

pytestmark = pytest.mark.unit


class FakeHub:
    def __init__(self):
        self.chambers = {}

    def _wire(self, chamber_id, payload):
        return {
            "id": chamber_id,
            "name": payload["name"],
            "host": payload["host"],
            "port": payload["port"],
        }

    def request(self, method, url, payload=None):
        if method == "GET":
            return {"chambers": list(self.chambers.values())}
        if method == "POST" and url.endswith("/chambers"):
            chamber_id = str(uuid.uuid4())
            wired = self._wire(chamber_id, payload)
            self.chambers[chamber_id] = wired
            return wired
        if method == "POST" and "/replace" in url:
            chamber_id = url.rsplit("/", 2)[-2]
            wired = self._wire(chamber_id, payload)
            self.chambers[chamber_id] = wired
            return wired
        raise AssertionError(f"Unexpected request: {method} {url}")

    def seed(self, name, host, port):
        chamber_id = str(uuid.uuid4())
        self.chambers[chamber_id] = self._wire(
            chamber_id, {"name": name, "host": host, "port": port}
        )
        return chamber_id


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


def test_sync_pushes_local_only_chamber(deepvac_data_dir, hub):
    chambers_service.add_chamber("Chamber 2", "10.0.0.2", 5555)

    result = sync_service.sync()

    pushed_names = set(result.pushed)
    assert "Chamber 2" in pushed_names
    assert result.conflicts == []
    local = next(c for c in chambers_service.list_chambers() if c["name"] == "Chamber 2")
    assert local["hub_id"] in hub.chambers


def test_sync_pulls_hub_only_chamber(deepvac_data_dir, hub):
    hub.seed("Hub Chamber", "10.0.0.9", 6000)

    result = sync_service.sync()

    assert "Hub Chamber" in result.pulled
    local = next(c for c in chambers_service.list_chambers() if c["name"] == "Hub Chamber")
    assert local["host"] == "10.0.0.9"
    assert local["port"] == 6000


def test_sync_leaves_identical_chambers_alone(deepvac_data_dir, hub):
    hub_id = hub.seed("Same Everywhere", "10.0.0.5", 5555)
    chambers_service.add_chamber("Same Everywhere", "10.0.0.5", 5555, hub_id=hub_id)

    result = sync_service.sync()

    assert result.conflicts == []
    assert "Same Everywhere" not in result.pushed
    assert "Same Everywhere" not in result.pulled


def test_sync_reports_conflict_when_host_differs(deepvac_data_dir, hub):
    hub_id = hub.seed("Diverged", "10.0.0.5", 5555)
    local = chambers_service.add_chamber("Diverged", "10.0.0.6", 5555, hub_id=hub_id)

    result = sync_service.sync()

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.chamber_id == local["id"]
    assert conflict.hub_id == hub_id


def test_resolve_conflict_keep_local_overwrites_hub(deepvac_data_dir, hub):
    hub_id = hub.seed("Diverged", "10.0.0.5", 5555)
    local = chambers_service.add_chamber("Diverged", "10.0.0.6", 5555, hub_id=hub_id)
    conflict = sync_service.sync().conflicts[0]

    sync_service.resolve_conflict(conflict, "local")

    assert hub.chambers[hub_id]["host"] == "10.0.0.6"
    updated = next(c for c in chambers_service.list_chambers() if c["id"] == local["id"])
    assert updated["hub_synced_at"] is not None


def test_resolve_conflict_keep_hub_overwrites_local(deepvac_data_dir, hub):
    hub_id = hub.seed("Diverged", "10.0.0.5", 5555)
    local = chambers_service.add_chamber("Diverged", "10.0.0.6", 5555, hub_id=hub_id)
    conflict = sync_service.sync().conflicts[0]

    sync_service.resolve_conflict(conflict, "hub")

    updated = [c for c in chambers_service.list_chambers() if c["id"] == local["id"]][0]
    assert updated["host"] == "10.0.0.5"
