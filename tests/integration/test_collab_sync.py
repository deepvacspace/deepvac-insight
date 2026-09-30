"""End-to-end tests of the local-network collaboration host: a real TLS server
on loopback with simulated peers pairing and syncing shared data."""

import pytest

from app.services import (
    alarms_service,
    annotations_service,
    chambers_service,
    collab_client,
    collab_host_service,
    collab_sync_service,
    collab_tls,
    data_service,
    licensing_client,
    run_identity,
    settings_service,
    shared_runs_service,
)
from app.services.collab_server import CollabServer

pytestmark = pytest.mark.integration

_FINGERPRINT = "f" * 32


@pytest.fixture
def host(deepvac_data_dir, qsettings_isolated):
    server = CollabServer()
    server.start(port=0)
    yield server
    server.stop()


@pytest.fixture
def use_peer(tmp_path, monkeypatch):
    def switch(name, runs=None, cached_runs=None):
        peer_dir = tmp_path / f"peer_{name}"
        monkeypatch.setattr(licensing_client, "_LICENSE_DIR", peer_dir / "license")
        monkeypatch.setattr(
            licensing_client, "_DEVICE_PRIVATE_KEY_PATH", peer_dir / "license" / "device.bin"
        )
        monkeypatch.setattr(annotations_service, "ANNOTATIONS_DB", peer_dir / "annotations.sqlite3")
        monkeypatch.setattr(alarms_service, "ALARMS_DB", peer_dir / "alarms.sqlite3")
        monkeypatch.setattr(chambers_service, "CHAMBERS_DB", peer_dir / "chambers.sqlite3")
        monkeypatch.setattr(shared_runs_service, "SHARED_RUNS_DB", peer_dir / "shared.sqlite3")
        monkeypatch.setattr(run_identity, "run_fingerprints", lambda: dict(runs or {}))
        monkeypatch.setattr(data_service, "load_cached_runs", lambda: list(cached_runs or []))

    return switch


def _pair(host, name):
    address = f"127.0.0.1:{host.port}"
    collab_client.pair(address, host.new_join_code(), name, host.cert_pem)


def _run(key, **overrides):
    record = {
        "key": key,
        "id": key.split("/")[-1],
        "group": "g",
        "samples": 100,
        "duration_s": 60.0,
        "mae": 0.5,
        "cost": 1.0,
        "tail_mae": 0.2,
        "overshoot": None,
        "settle_time_s": None,
        "start_time": "2026-01-01 10:00:00",
        "end_time": "2026-01-01 11:00:00",
        "chamber": "Chamber 1",
        "test_profile": None,
    }
    record.update(overrides)
    return record


def test_pairing_with_a_valid_code_succeeds(host, use_peer):
    use_peer("a")

    _pair(host, "Peer A")

    assert collab_client.connection()["url"] == f"https://127.0.0.1:{host.port}"
    assert collab_host_service.peer_count() == 1


def test_join_code_is_single_use(host, use_peer):
    use_peer("a")
    code = host.new_join_code()
    collab_client.pair(f"127.0.0.1:{host.port}", code, "Peer A", host.cert_pem)

    with pytest.raises(collab_client.CollabError):
        collab_client.pair(f"127.0.0.1:{host.port}", code, "Peer A again", host.cert_pem)


def test_wrong_join_code_is_rejected(host, use_peer):
    use_peer("a")
    host.new_join_code()

    with pytest.raises(collab_client.CollabError):
        collab_client.pair(f"127.0.0.1:{host.port}", "AAAA-AAAA", "Peer A", host.cert_pem)


def test_join_code_locks_out_after_repeated_failures(host, use_peer):
    use_peer("a")
    code = host.new_join_code()
    for _ in range(5):
        assert host.consume_join_code("WRONG-000") is False

    assert host.consume_join_code(code) is False


def test_unpaired_device_cannot_read_items(host, use_peer):
    use_peer("stranger")
    settings_service.save_collab_connection(f"https://127.0.0.1:{host.port}", "Host", host.cert_pem)

    with pytest.raises(collab_client.CollabError, match="not paired"):
        collab_client.request("GET", "/items/annotation")


def test_pairing_fails_when_the_host_presents_a_different_certificate(
    host, use_peer, tmp_path, monkeypatch
):
    use_peer("a")
    monkeypatch.setattr(collab_tls, "HOST_TLS_DIR", tmp_path / "other_host_identity")
    other_cert = collab_tls.host_cert_pem()

    with pytest.raises(collab_client.CollabError, match="identity changed"):
        collab_client.pair(f"127.0.0.1:{host.port}", host.new_join_code(), "Peer A", other_cert)


def test_requests_fail_once_the_host_certificate_changes(host, use_peer, tmp_path, monkeypatch):
    use_peer("a")
    _pair(host, "Peer A")
    host.stop()
    monkeypatch.setattr(collab_tls, "HOST_TLS_DIR", tmp_path / "regenerated_identity")
    host.start(port=host.port or 0)
    settings_service.save_collab_connection(
        f"https://127.0.0.1:{host.port}",
        "Host",
        collab_client.connection()["cert"],
    )

    with pytest.raises(collab_client.CollabError, match="identity changed"):
        collab_client.request("GET", "/items/annotation")


def test_plain_http_clients_are_not_served(host, use_peer):
    import urllib.error
    import urllib.request

    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        urllib.request.urlopen(f"http://127.0.0.1:{host.port}/items/annotation", timeout=3)  # noqa: S310


def test_annotation_is_shared_between_peers_and_deletion_propagates(host, use_peer):
    use_peer("a", {"a/run1": _FINGERPRINT})
    _pair(host, "Peer A")
    annotations_service.add_annotation("a/run1", 1, "Ada", 1.0, 2.0, "spike", "#ff0000")
    assert collab_sync_service.sync().kinds["annotation"].pushed == 1

    use_peer("b", {"b/copy-of-run1": _FINGERPRINT})
    _pair(host, "Peer B")
    result = collab_sync_service.sync()
    pulled = annotations_service.list_annotations("b/copy-of-run1")

    assert result.kinds["annotation"].pulled == 1
    assert [(a["label"], a["user_name"]) for a in pulled] == [("spike", "Ada")]

    annotations_service.delete_annotation(pulled[0]["id"])
    collab_sync_service.sync()

    use_peer("a", {"a/run1": _FINGERPRINT})
    result = collab_sync_service.sync()

    assert result.kinds["annotation"].removed == 1
    assert annotations_service.list_annotations("a/run1") == []


def test_annotation_for_a_run_this_peer_lacks_is_skipped(host, use_peer):
    use_peer("a", {"a/run1": _FINGERPRINT})
    _pair(host, "Peer A")
    annotations_service.add_annotation("a/run1", 1, "Ada", 1.0, 2.0, "spike", "#ff0000")
    collab_sync_service.sync()

    use_peer("b")
    _pair(host, "Peer B")
    result = collab_sync_service.sync()

    assert result.kinds["annotation"].pulled == 0
    assert result.kinds["annotation"].skipped == 1


def test_annotations_are_shared_again_if_the_host_lost_its_data(host, use_peer):
    use_peer("a", {"a/run1": _FINGERPRINT})
    _pair(host, "Peer A")
    annotations_service.add_annotation("a/run1", 1, "Ada", 1.0, 2.0, "spike", "#ff0000")
    collab_sync_service.sync()
    conn = collab_host_service.connect_collab_host()
    conn.execute("DELETE FROM items")
    conn.commit()
    conn.close()

    result = collab_sync_service.sync()

    assert result.kinds["annotation"].pushed == 1
    assert annotations_service.list_annotations("a/run1")
    assert len(collab_host_service.list_items("annotation")) == 1


def test_variable_rule_is_shared_and_deletion_propagates(host, use_peer):
    use_peer("a", {"a/run1": _FINGERPRINT})
    _pair(host, "Peer A")
    annotations_service.add_rule("a/run1", 1, "Ada", "band", "temp", 20.0, 80.0, "#00ff00")
    collab_sync_service.sync()

    use_peer("b", {"b/run1": _FINGERPRINT})
    _pair(host, "Peer B")
    result = collab_sync_service.sync()
    pulled = annotations_service.list_rules("b/run1")

    assert result.kinds["variable_rule"].pulled == 1
    assert [(r["name"], r["lo"], r["hi"]) for r in pulled] == [("band", 20.0, 80.0)]

    annotations_service.delete_rule(pulled[0]["id"])
    collab_sync_service.sync()
    use_peer("a", {"a/run1": _FINGERPRINT})

    assert collab_sync_service.sync().kinds["variable_rule"].removed == 1
    assert annotations_service.list_rules("a/run1") == []


def test_run_metadata_is_shared_and_appears_in_the_catalog(host, use_peer):
    use_peer("a", {"a/run1": _FINGERPRINT}, [_run("a/run1")])
    _pair(host, "Peer A")
    assert collab_sync_service.sync("Ada").kinds["run_metadata"].pushed == 1

    use_peer("b")
    _pair(host, "Peer B")
    result = collab_sync_service.sync("Bo")
    catalog = shared_runs_service.list_runs()

    assert result.kinds["run_metadata"].pulled == 1
    assert [(r["id"], r["mae"], r["shared_by"]) for r in catalog] == [("run1", 0.5, "Ada")]


def test_run_metadata_updates_when_a_run_changes(host, use_peer):
    use_peer("a", {"a/run1": _FINGERPRINT}, [_run("a/run1", mae=0.5)])
    _pair(host, "Peer A")
    collab_sync_service.sync("Ada")

    use_peer("a", {"a/run1": _FINGERPRINT}, [_run("a/run1", mae=0.3)])
    result = collab_sync_service.sync("Ada")

    assert result.kinds["run_metadata"].pushed == 1
    assert shared_runs_service.list_runs()[0]["mae"] == 0.3


def _add_chamber_event(chamber_name="Chamber 1", host_address="10.0.0.5", **overrides):
    chamber = chambers_service.add_chamber(chamber_name, host_address, 5555)
    rule = {
        "id": None,
        "name": "High temp",
        "variable": "temp",
        "severity": "Critical",
        "chamber_id": chamber["id"],
        "chamber_name": chamber["name"],
    }
    event_id = alarms_service.record_trigger(rule, 91.5)
    return chamber, event_id


def test_alarm_events_are_shared_between_peers_for_the_same_chamber(host, use_peer):
    use_peer("a")
    _pair(host, "Peer A")
    _add_chamber_event("Oven A", "10.0.0.5")
    assert collab_sync_service.sync().kinds["alarm_event"].pushed == 1

    use_peer("b")
    _pair(host, "Peer B")
    chambers_service.add_chamber("Their name for it", "10.0.0.5", 5555)
    result = collab_sync_service.sync()
    events = alarms_service.list_events(limit=10)

    assert result.kinds["alarm_event"].pulled == 1
    assert [(e["rule_name"], e["chamber_name"], e["trigger_value"]) for e in events] == [
        ("High temp", "Their name for it", 91.5)
    ]


def test_alarm_events_for_unknown_chambers_are_skipped(host, use_peer):
    use_peer("a")
    _pair(host, "Peer A")
    _add_chamber_event("Oven A", "10.0.0.5")
    collab_sync_service.sync()

    use_peer("b")
    _pair(host, "Peer B")
    result = collab_sync_service.sync()

    assert result.kinds["alarm_event"].pulled == 0
    assert result.kinds["alarm_event"].skipped == 1


def test_acknowledgement_and_clearing_propagate_to_the_other_peer(host, use_peer):
    use_peer("a")
    _pair(host, "Peer A")
    _chamber, event_id = _add_chamber_event("Oven A", "10.0.0.5")
    collab_sync_service.sync()

    use_peer("b")
    _pair(host, "Peer B")
    chambers_service.add_chamber("Oven A", "10.0.0.5", 5555)
    collab_sync_service.sync()
    local_event = alarms_service.list_events(limit=10)[0]
    alarms_service.acknowledge_event(local_event["id"], "Bo", "checked the door")
    alarms_service.record_clear(local_event["id"])
    collab_sync_service.sync()

    use_peer("a")
    collab_sync_service.sync()
    event = next(e for e in alarms_service.list_events(limit=10) if e["id"] == event_id)

    assert (event["acknowledged_by"], event["comment"]) == ("Bo", "checked the door")
    assert event["acknowledged_at"] is not None


def test_merge_event_state_is_order_independent_and_keeps_the_earliest_acknowledgement():
    early = {
        "cleared_at": None,
        "acknowledged_at": "2026-01-01T10:00:00",
        "acknowledged_by": "Ada",
        "comment": "first",
    }
    late = {
        "cleared_at": "2026-01-01T12:00:00",
        "acknowledged_at": "2026-01-01T11:00:00",
        "acknowledged_by": "Bo",
        "comment": "second",
    }

    merged = collab_sync_service.merge_event_state(early, late)

    assert merged == collab_sync_service.merge_event_state(late, early)
    assert merged["acknowledged_by"] == "Ada"
    assert merged["cleared_at"] == "2026-01-01T12:00:00"
