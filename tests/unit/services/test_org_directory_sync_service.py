"""Tests for org_directory_sync_service's read-only pull, against a fake
hub response standing in for licensing_client.signed_request_json."""

import pytest

from app.services import licensing_client, org_directory_service
from app.services import org_directory_sync_service as sync_service

pytestmark = pytest.mark.unit

_MEMBERS = [
    {
        "user_id": "u1",
        "display_name": "Ada Admin",
        "email": "ada@example.com",
        "role": "organization_admin",
    },
    {
        "user_id": "u2",
        "display_name": "Mo Member",
        "email": "mo@example.com",
        "role": "organization_member",
    },
]


def test_pull_requires_a_license(deepvac_data_dir, monkeypatch):
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: None)
    with pytest.raises(sync_service.SyncError):
        sync_service.pull()


def test_pull_replaces_local_cache(deepvac_data_dir, monkeypatch):
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: "org-1")
    monkeypatch.setattr(
        licensing_client, "signed_request_json", lambda method, url: {"members": _MEMBERS}
    )

    result = sync_service.pull()

    assert [m["display_name"] for m in result] == ["Ada Admin", "Mo Member"]
    assert org_directory_service.last_synced_at() is not None


def test_pull_overwrites_previous_cache_entirely(deepvac_data_dir, monkeypatch):
    monkeypatch.setattr(licensing_client, "current_organization_id", lambda: "org-1")
    monkeypatch.setattr(
        licensing_client, "signed_request_json", lambda method, url: {"members": _MEMBERS}
    )
    sync_service.pull()

    monkeypatch.setattr(
        licensing_client,
        "signed_request_json",
        lambda method, url: {"members": _MEMBERS[:1]},
    )
    result = sync_service.pull()

    assert len(result) == 1
    assert org_directory_service.list_members()[0]["display_name"] == "Ada Admin"
