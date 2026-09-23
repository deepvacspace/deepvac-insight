"""Syncs local test profiles with Deepvac Hub: pushes local-only profiles
up, pulls hub-only profiles down, and surfaces conflicts for profiles that
exist on both sides with different content."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services import licensing_client, test_profiles_service


class SyncError(Exception):
    pass


@dataclass
class SyncConflict:
    profile_id: int
    hub_id: str
    local: dict
    hub: dict


@dataclass
class SyncResult:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    conflicts: list[SyncConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _profiles_url():
    return f"{licensing_client.api_base_url()}/test-profiles"


def _steps_for_wire(steps):
    return [
        {
            "step_order": i,
            "setpoint_temp": step["setpoint_temp"],
            "setpoint_pressure": step["setpoint_pressure"],
            "duration_s": step["duration_s"],
            "label": step["label"],
        }
        for i, step in enumerate(steps)
    ]


def _step_key(step):
    return (step["setpoint_temp"], step["setpoint_pressure"], step["duration_s"], step["label"])


def _steps_equal(local_steps, hub_steps):
    return [_step_key(s) for s in local_steps] == [_step_key(s) for s in hub_steps]


def _fetch_hub_profiles():
    try:
        response = licensing_client.signed_request_json("GET", _profiles_url())
    except licensing_client.LicensingError as exc:
        raise SyncError(str(exc)) from exc
    return response["profiles"]


def _push_profile(profile):
    payload = {
        "name": profile["name"],
        "description": profile["description"],
        "steps": _steps_for_wire(profile["steps"]),
    }
    return licensing_client.signed_request_json("POST", _profiles_url(), payload)


def _replace_hub_profile(hub_id, profile):
    payload = {
        "name": profile["name"],
        "description": profile["description"],
        "steps": _steps_for_wire(profile["steps"]),
    }
    return licensing_client.signed_request_json(
        "POST", f"{_profiles_url()}/{hub_id}/replace", payload
    )


def sync():
    if licensing_client.current_organization_id() is None:
        raise SyncError("No license found for this installation yet.")

    hub_profiles = _fetch_hub_profiles()
    local_profiles = test_profiles_service.list_profiles()
    hub_by_id = {p["id"]: p for p in hub_profiles}
    local_by_hub_id = {p["hub_id"]: p for p in local_profiles if p["hub_id"]}

    result = SyncResult()

    for profile in local_profiles:
        if profile["hub_id"] is not None:
            continue
        try:
            created = _push_profile(profile)
            test_profiles_service.mark_hub_synced(profile["id"], created["id"])
            result.pushed.append(profile["name"])
        except licensing_client.LicensingError as exc:
            result.errors.append(f"{profile['name']}: {exc}")

    for hub_profile in hub_profiles:
        if hub_profile["id"] in local_by_hub_id:
            continue
        try:
            test_profiles_service.add_profile(
                hub_profile["name"],
                hub_profile["description"],
                hub_profile["steps"],
                created_by="Deepvac Hub",
                hub_id=hub_profile["id"],
            )
            result.pulled.append(hub_profile["name"])
        except test_profiles_service.TestProfileError as exc:
            result.errors.append(f"{hub_profile['name']}: {exc}")

    for hub_id, local_profile in local_by_hub_id.items():
        hub_profile = hub_by_id.get(hub_id)
        if hub_profile is None:
            continue
        if (
            local_profile["name"] != hub_profile["name"]
            or local_profile["description"] != hub_profile["description"]
            or not _steps_equal(local_profile["steps"], hub_profile["steps"])
        ):
            result.conflicts.append(
                SyncConflict(
                    profile_id=local_profile["id"],
                    hub_id=hub_id,
                    local=local_profile,
                    hub=hub_profile,
                )
            )

    return result


def resolve_conflict(conflict, keep):
    """keep is "local" (push local content over hub's) or "hub" (overwrite
    the local profile with hub's content)."""
    if keep == "local":
        _replace_hub_profile(conflict.hub_id, conflict.local)
        test_profiles_service.touch_hub_synced(conflict.profile_id)
    elif keep == "hub":
        test_profiles_service.update_profile(
            conflict.profile_id,
            conflict.hub["name"],
            conflict.hub["description"],
            conflict.hub["steps"],
        )
        test_profiles_service.touch_hub_synced(conflict.profile_id)
    else:
        raise ValueError(f"Unknown resolution: {keep!r}")
