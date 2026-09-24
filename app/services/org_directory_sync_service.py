"""Pulls the organization member directory from Deepvac Hub. Read-only:
this never pushes local data, it only replaces the local cache with
whatever Hub currently reports."""

from __future__ import annotations

from app.services import licensing_client, org_directory_service


class SyncError(Exception):
    pass


def pull():
    if licensing_client.current_organization_id() is None:
        raise SyncError("Not authenticated. Authenticate from your Profile first.")
    url = f"{licensing_client.api_base_url()}/organization-members"
    try:
        response = licensing_client.signed_request_json("GET", url)
    except licensing_client.LicensingError as exc:
        raise SyncError(str(exc)) from exc
    org_directory_service.replace_all(response["members"])
    return org_directory_service.list_members()
