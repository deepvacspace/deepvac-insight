"""Syncs the local chamber registry with Deepvac Hub: pushes local-only
chambers up, pulls hub-only chambers down, and surfaces conflicts for
chambers that exist on both sides with different content."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services import chambers_service, licensing_client


class SyncError(Exception):
    pass


@dataclass
class SyncConflict:
    chamber_id: int
    hub_id: str
    local: dict
    hub: dict


@dataclass
class SyncResult:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    conflicts: list[SyncConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _chambers_url():
    return f"{licensing_client.api_base_url()}/chambers"


def _fetch_hub_chambers():
    try:
        response = licensing_client.signed_request_json("GET", _chambers_url())
    except licensing_client.LicensingError as exc:
        raise SyncError(str(exc)) from exc
    return response["chambers"]


def _push_chamber(chamber):
    payload = {"name": chamber["name"], "host": chamber["host"], "port": chamber["port"]}
    return licensing_client.signed_request_json("POST", _chambers_url(), payload)


def _replace_hub_chamber(hub_id, chamber):
    payload = {"name": chamber["name"], "host": chamber["host"], "port": chamber["port"]}
    return licensing_client.signed_request_json(
        "POST", f"{_chambers_url()}/{hub_id}/replace", payload
    )


def sync():
    if licensing_client.current_organization_id() is None:
        raise SyncError("Not authenticated. Authenticate from your Profile first.")

    hub_chambers = _fetch_hub_chambers()
    local_chambers = chambers_service.list_chambers()
    hub_by_id = {c["id"]: c for c in hub_chambers}
    local_by_hub_id = {c["hub_id"]: c for c in local_chambers if c["hub_id"]}

    result = SyncResult()

    for chamber in local_chambers:
        if chamber["hub_id"] is not None:
            continue
        try:
            created = _push_chamber(chamber)
            chambers_service.mark_hub_synced(chamber["id"], created["id"])
            result.pushed.append(chamber["name"])
        except licensing_client.LicensingError as exc:
            result.errors.append(f"{chamber['name']}: {exc}")

    for hub_chamber in hub_chambers:
        if hub_chamber["id"] in local_by_hub_id:
            continue
        try:
            chambers_service.add_chamber(
                hub_chamber["name"],
                hub_chamber["host"],
                hub_chamber["port"],
                hub_id=hub_chamber["id"],
            )
            result.pulled.append(hub_chamber["name"])
        except chambers_service.ChamberError as exc:
            result.errors.append(f"{hub_chamber['name']}: {exc}")

    for hub_id, local_chamber in local_by_hub_id.items():
        hub_chamber = hub_by_id.get(hub_id)
        if hub_chamber is None:
            continue
        if (
            local_chamber["name"] != hub_chamber["name"]
            or local_chamber["host"] != hub_chamber["host"]
            or local_chamber["port"] != hub_chamber["port"]
        ):
            result.conflicts.append(
                SyncConflict(
                    chamber_id=local_chamber["id"],
                    hub_id=hub_id,
                    local=local_chamber,
                    hub=hub_chamber,
                )
            )

    return result


def resolve_conflict(conflict, keep):
    """keep is "local" (push local content over hub's) or "hub" (overwrite
    the local chamber with hub's content)."""
    if keep == "local":
        _replace_hub_chamber(conflict.hub_id, conflict.local)
        chambers_service.touch_hub_synced(conflict.chamber_id)
    elif keep == "hub":
        chambers_service.update_chamber(
            conflict.chamber_id,
            conflict.hub["name"],
            conflict.hub["host"],
            conflict.hub["port"],
        )
        chambers_service.touch_hub_synced(conflict.chamber_id)
    else:
        raise ValueError(f"Unknown resolution: {keep!r}")
