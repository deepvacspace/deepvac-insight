"""Syncs one chamber's alarm rules with DeepVac Hub: pushes local-only
rules up, pulls hub-only rules down, and surfaces conflicts for rules that
exist on both sides with different content. Requires the chamber itself to
already be synced (see chamber_sync_service)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services import alarms_service, licensing_client


class SyncError(Exception):
    pass


@dataclass
class SyncConflict:
    rule_id: int
    hub_id: str
    local: dict
    hub: dict


@dataclass
class SyncResult:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    conflicts: list[SyncConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _rules_url():
    return f"{licensing_client.api_base_url()}/alarm-rules"


def _rule_fields(rule):
    return {
        "name": rule["name"],
        "variable": rule["variable"],
        "condition": rule["condition"],
        "value": rule["value"],
        "value2": rule["value2"],
        "severity": rule["severity"],
        "deadband": rule["deadband"],
        "delay_s": rule["delay_s"],
        "enabled": bool(rule["enabled"]),
    }


def _rules_equal(local_rule, hub_rule):
    return _rule_fields(local_rule) == _rule_fields(hub_rule)


def _fetch_hub_rules():
    try:
        response = licensing_client.signed_request_json("GET", _rules_url())
    except licensing_client.LicensingError as exc:
        raise SyncError(str(exc)) from exc
    return response["alarm_rules"]


def _push_rule(hub_chamber_id, rule):
    payload = {"chamber_id": hub_chamber_id, **_rule_fields(rule)}
    return licensing_client.signed_request_json("POST", _rules_url(), payload)


def _replace_hub_rule(hub_id, rule):
    payload = {"chamber_id": rule["_hub_chamber_id"], **_rule_fields(rule)}
    return licensing_client.signed_request_json("POST", f"{_rules_url()}/{hub_id}/replace", payload)


def sync(local_chamber):
    if licensing_client.current_organization_id() is None:
        raise SyncError("No license found for this installation yet.")
    hub_chamber_id = local_chamber.get("hub_id")
    if not hub_chamber_id:
        raise SyncError("Sync this chamber with Hub first (Chambers dialog).")

    hub_rules = [r for r in _fetch_hub_rules() if r["chamber_id"] == hub_chamber_id]
    local_rules = alarms_service.list_rules(chamber_id=local_chamber["id"])
    hub_by_id = {r["id"]: r for r in hub_rules}
    local_by_hub_id = {r["hub_id"]: r for r in local_rules if r["hub_id"]}

    result = SyncResult()

    for rule in local_rules:
        if rule["hub_id"] is not None:
            continue
        try:
            created = _push_rule(hub_chamber_id, rule)
            alarms_service.mark_hub_synced(rule["id"], created["id"])
            result.pushed.append(rule["name"])
        except licensing_client.LicensingError as exc:
            result.errors.append(f"{rule['name']}: {exc}")

    for hub_rule in hub_rules:
        if hub_rule["id"] in local_by_hub_id:
            continue
        alarms_service.add_rule(
            hub_rule["name"],
            hub_rule["variable"],
            hub_rule["condition"],
            hub_rule["value"],
            hub_rule["value2"],
            hub_rule["severity"],
            deadband=hub_rule["deadband"],
            delay_s=hub_rule["delay_s"],
            created_by="DeepVac Hub",
            chamber_id=local_chamber["id"],
            chamber_name=local_chamber["name"],
            hub_id=hub_rule["id"],
        )
        result.pulled.append(hub_rule["name"])

    for hub_id, local_rule in local_by_hub_id.items():
        hub_rule = hub_by_id.get(hub_id)
        if hub_rule is None:
            continue
        if not _rules_equal(local_rule, hub_rule):
            result.conflicts.append(
                SyncConflict(
                    rule_id=local_rule["id"], hub_id=hub_id, local=local_rule, hub=hub_rule
                )
            )

    return result


def resolve_conflict(conflict, keep, local_chamber):
    """keep is "local" (push local content over hub's) or "hub" (overwrite
    the local rule with hub's content)."""
    if keep == "local":
        rule = dict(conflict.local, _hub_chamber_id=local_chamber["hub_id"])
        _replace_hub_rule(conflict.hub_id, rule)
        alarms_service.touch_hub_synced(conflict.rule_id)
    elif keep == "hub":
        hub_rule = conflict.hub
        alarms_service.update_rule(
            conflict.rule_id,
            hub_rule["name"],
            hub_rule["variable"],
            hub_rule["condition"],
            hub_rule["value"],
            hub_rule["value2"],
            hub_rule["severity"],
            hub_rule["deadband"],
            hub_rule["delay_s"],
            hub_rule["enabled"],
        )
        alarms_service.touch_hub_synced(conflict.rule_id)
    else:
        raise ValueError(f"Unknown resolution: {keep!r}")
