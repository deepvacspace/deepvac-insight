"""Syncs shared data with the connected collaboration host: annotations,
variable rules, run metadata and alarm events."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services import (
    alarms_service,
    annotations_service,
    chambers_service,
    collab_client,
    data_service,
    run_identity,
    shared_runs_service,
)

KINDS = ("annotation", "variable_rule", "run_metadata", "alarm_event")
_MAX_SHARED_EVENTS = 2000
_RUN_METADATA_FIELDS = (
    "id",
    "group",
    "samples",
    "duration_s",
    "mae",
    "cost",
    "tail_mae",
    "overshoot",
    "settle_time_s",
    "start_time",
    "end_time",
    "chamber",
    "test_profile",
)


@dataclass
class KindResult:
    pushed: int = 0
    pulled: int = 0
    removed: int = 0
    skipped: int = 0


@dataclass
class SyncResult:
    kinds: dict = field(default_factory=lambda: {kind: KindResult() for kind in KINDS})
    errors: list = field(default_factory=list)


@dataclass
class _Context:
    user_name: str
    fingerprint_by_run: dict
    run_by_fingerprint: dict


@dataclass
class _RunScopedKind:
    kind: str
    list_local: Callable
    fields: Callable
    add_local: Callable
    delete_local: Callable
    set_uid: Callable


_ANNOTATIONS = _RunScopedKind(
    kind="annotation",
    list_local=lambda: annotations_service.list_all_annotations(),
    fields=lambda a: {
        "user_name": a["user_name"],
        "x0": a["x0"],
        "x1": a["x1"],
        "label": a["label"],
        "color": a["color"],
        "created_at": a["created_at"],
    },
    add_local=lambda run_key, r: annotations_service.add_annotation(
        run_key,
        None,
        r["user_name"],
        r["x0"],
        r["x1"],
        r["label"],
        r["color"],
        collab_uid=r["uid"],
        created_at=r["created_at"],
    ),
    delete_local=lambda row_id: annotations_service.delete_annotation_without_tombstone(row_id),
    set_uid=lambda row_id, uid: annotations_service.set_collab_uid(row_id, uid),
)

_VARIABLE_RULES = _RunScopedKind(
    kind="variable_rule",
    list_local=lambda: annotations_service.list_all_rules(),
    fields=lambda r: {
        "user_name": r["user_name"],
        "name": r["name"],
        "channel": r["channel"],
        "lo": r["lo"],
        "hi": r["hi"],
        "color": r["color"],
        "created_at": r["created_at"],
    },
    add_local=lambda run_key, r: annotations_service.add_rule(
        run_key,
        None,
        r["user_name"],
        r["name"],
        r["channel"],
        r["lo"],
        r["hi"],
        r["color"],
        collab_uid=r["uid"],
        created_at=r["created_at"],
    ),
    delete_local=lambda row_id: annotations_service.delete_rule_without_tombstone(row_id),
    set_uid=lambda row_id, uid: annotations_service.set_rule_collab_uid(row_id, uid),
)


def _fetch(kind):
    response = collab_client.request("GET", f"/items/{kind}")
    return {item["uid"]: item for item in response["items"]}, set(response["deleted"])


def _sync_run_scoped(spec, ctx, result):
    kind_result = result.kinds[spec.kind]
    host_by_uid, deleted_uids = _fetch(spec.kind)
    local = spec.list_local()
    local_uids = {row["collab_uid"] for row in local if row["collab_uid"]}
    tombstones = set(annotations_service.list_tombstones(spec.kind))

    for uid in tombstones:
        try:
            collab_client.request("POST", f"/items/{spec.kind}/{uid}/delete")
            annotations_service.clear_tombstone(uid)
        except collab_client.CollabError as exc:
            result.errors.append(str(exc))

    for row in local:
        uid = row["collab_uid"]
        if uid in deleted_uids:
            spec.delete_local(row["id"])
            kind_result.removed += 1
            continue
        if uid in host_by_uid:
            continue
        run_uid = ctx.fingerprint_by_run.get(row["run_key"])
        if run_uid is None:
            kind_result.skipped += 1
            continue
        uid = uid or uuid.uuid4().hex
        payload = {"uid": uid, "run_uid": run_uid, **spec.fields(row)}
        try:
            collab_client.request("POST", f"/items/{spec.kind}", payload)
        except collab_client.CollabError as exc:
            result.errors.append(str(exc))
            continue
        spec.set_uid(row["id"], uid)
        local_uids.add(uid)
        kind_result.pushed += 1

    for uid, remote in host_by_uid.items():
        if uid in local_uids or uid in tombstones:
            continue
        run_key = ctx.run_by_fingerprint.get(remote["run_uid"])
        if run_key is None:
            kind_result.skipped += 1
            continue
        spec.add_local(run_key, remote)
        kind_result.pulled += 1


def _sync_run_metadata(ctx, result):
    kind_result = result.kinds["run_metadata"]
    host_by_uid, _ = _fetch("run_metadata")

    for run in data_service.load_cached_runs():
        run_uid = ctx.fingerprint_by_run.get(run["key"])
        if run_uid is None:
            kind_result.skipped += 1
            continue
        fields = {name: run.get(name) for name in _RUN_METADATA_FIELDS}
        remote = host_by_uid.get(run_uid)
        if remote is not None and all(remote.get(name) == value for name, value in fields.items()):
            continue
        payload = {"uid": run_uid, "shared_by": ctx.user_name, **fields}
        try:
            collab_client.request("POST", "/items/run_metadata", payload)
        except collab_client.CollabError as exc:
            result.errors.append(str(exc))
            continue
        host_by_uid[run_uid] = payload
        kind_result.pushed += 1

    kind_result.pulled = sum(1 for uid in host_by_uid if uid not in ctx.run_by_fingerprint)
    shared_runs_service.replace_all(list(host_by_uid.values()))


def chamber_ref(chamber):
    """Identifies a physical chamber across computers by its network address."""
    return f"{chamber['host'].strip().lower()}:{chamber['port']}"


def _event_fields(event, chamber):
    return {
        "chamber_ref": chamber_ref(chamber),
        "chamber_name": chamber["name"],
        "rule_name": event["rule_name"],
        "variable": event["variable"],
        "severity": event["severity"],
        "trigger_value": event["trigger_value"],
        "triggered_at": event["triggered_at"],
        "cleared_at": event["cleared_at"],
        "acknowledged_at": event["acknowledged_at"],
        "acknowledged_by": event["acknowledged_by"],
        "comment": event["comment"] or "",
    }


def _event_state(fields):
    return {
        "cleared_at": fields["cleared_at"],
        "acknowledged_at": fields["acknowledged_at"],
        "acknowledged_by": fields["acknowledged_by"],
        "comment": fields["comment"] or "",
    }


def _earliest(first, second):
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def merge_event_state(first, second):
    """Merges two states of the same alarm event: earliest acknowledgement and earliest clear time win."""
    acknowledged = [s for s in (first, second) if s["acknowledged_at"]]
    if acknowledged:
        chosen = min(
            acknowledged,
            key=lambda s: (s["acknowledged_at"], s["acknowledged_by"] or "", s["comment"]),
        )
        acknowledgement = {
            "acknowledged_at": chosen["acknowledged_at"],
            "acknowledged_by": chosen["acknowledged_by"],
            "comment": chosen["comment"],
        }
    else:
        acknowledgement = {
            "acknowledged_at": None,
            "acknowledged_by": None,
            "comment": max(first["comment"], second["comment"]),
        }
    return {"cleared_at": _earliest(first["cleared_at"], second["cleared_at"]), **acknowledgement}


def _sync_alarm_events(ctx, result):
    kind_result = result.kinds["alarm_event"]
    host_by_uid, _ = _fetch("alarm_event")
    chambers = chambers_service.list_chambers()
    chamber_by_id = {c["id"]: c for c in chambers}
    chamber_by_ref = {chamber_ref(c): c for c in chambers}
    local = alarms_service.list_events(limit=1_000_000)
    known_uids = {e["collab_uid"] for e in local if e["collab_uid"]}

    for event in local[:_MAX_SHARED_EVENTS]:
        chamber = chamber_by_id.get(event["chamber_id"])
        if chamber is None:
            kind_result.skipped += 1
            continue
        uid = event["collab_uid"] or uuid.uuid4().hex
        fields = _event_fields(event, chamber)
        remote = host_by_uid.get(uid)
        if remote is None:
            payload = {"uid": uid, **fields}
        else:
            local_state = _event_state(fields)
            remote_state = _event_state(remote)
            merged = merge_event_state(local_state, remote_state)
            if merged != local_state:
                alarms_service.update_event_state(event["id"], **merged)
            if merged == remote_state:
                continue
            payload = {"uid": uid, **fields, **merged}
        try:
            collab_client.request("POST", "/items/alarm_event", payload)
        except collab_client.CollabError as exc:
            result.errors.append(str(exc))
            continue
        if not event["collab_uid"]:
            alarms_service.set_event_collab_uid(event["id"], uid)
        known_uids.add(uid)
        kind_result.pushed += 1

    for uid, remote in host_by_uid.items():
        if uid in known_uids:
            continue
        chamber = chamber_by_ref.get(remote["chamber_ref"])
        if chamber is None:
            kind_result.skipped += 1
            continue
        alarms_service.add_event_from_remote(remote, chamber["id"], chamber["name"], uid)
        kind_result.pulled += 1


def sync(user_name="Unknown"):
    fingerprint_by_run = run_identity.run_fingerprints()
    ctx = _Context(
        user_name=user_name,
        fingerprint_by_run=fingerprint_by_run,
        run_by_fingerprint={fp: key for key, fp in fingerprint_by_run.items()},
    )
    result = SyncResult()
    steps = (
        lambda: _sync_run_scoped(_ANNOTATIONS, ctx, result),
        lambda: _sync_run_scoped(_VARIABLE_RULES, ctx, result),
        lambda: _sync_run_metadata(ctx, result),
        lambda: _sync_alarm_events(ctx, result),
    )
    for step in steps:
        try:
            step()
        except collab_client.CollabError as exc:
            result.errors.append(str(exc))
            break
    return result
