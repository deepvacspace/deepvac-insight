"""Tests for licensing_client's account-info verification and local cache."""

import json
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.services import licensing_client

pytestmark = pytest.mark.unit


def _account_info_payload(**overrides):
    payload = {
        "schema_version": 1,
        "user_id": "11111111-1111-1111-1111-111111111111",
        "email": "user@example.com",
        "display_name": "Test User",
        "organization_id": "22222222-2222-2222-2222-222222222222",
        "organization_name": "Acme Labs",
    }
    payload.update(overrides)
    return payload


def _sign(payload, private_key, key_id="test-key"):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    signature = private_key.sign(canonical.encode("utf-8"))
    return {
        "envelope_version": 1,
        "payload": payload,
        "signature": urlsafe_b64encode(signature).decode("ascii"),
        "key_id": key_id,
    }


@pytest.fixture
def signing_keypair():
    private_key = Ed25519PrivateKey.generate()
    raw_public = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    return private_key, raw_public


def test_verify_account_info_accepts_valid_signature(signing_keypair):
    private_key, raw_public = signing_keypair
    envelope = _sign(_account_info_payload(), private_key)

    payload = licensing_client.verify_account_info(envelope, {"test-key": raw_public})

    assert payload["email"] == "user@example.com"


def test_verify_account_info_rejects_unknown_key_id(signing_keypair):
    private_key, _raw_public = signing_keypair
    envelope = _sign(_account_info_payload(), private_key)

    with pytest.raises(licensing_client.InvalidLicenseError):
        licensing_client.verify_account_info(envelope, {})


def test_verify_account_info_rejects_tampered_payload(signing_keypair):
    private_key, raw_public = signing_keypair
    envelope = _sign(_account_info_payload(), private_key)
    envelope["payload"]["organization_name"] = "Tampered Org"

    with pytest.raises(licensing_client.InvalidLicenseError):
        licensing_client.verify_account_info(envelope, {"test-key": raw_public})


def test_verify_account_info_rejects_wrong_shape(signing_keypair):
    private_key, raw_public = signing_keypair
    envelope = _sign(_account_info_payload(extra_field="nope"), private_key)

    with pytest.raises(licensing_client.InvalidLicenseError):
        licensing_client.verify_account_info(envelope, {"test-key": raw_public})


@pytest.fixture
def isolated_license_dir(tmp_path, monkeypatch):
    license_dir = tmp_path / "license"
    monkeypatch.setattr(licensing_client, "_LICENSE_DIR", license_dir)
    monkeypatch.setattr(licensing_client, "_LICENSE_PATH", license_dir / "license.json")
    monkeypatch.setattr(licensing_client, "_ACCOUNT_INFO_PATH", license_dir / "account_info.json")
    monkeypatch.setattr(licensing_client, "_TRUSTED_KEYS_PATH", license_dir / "trusted_keys.json")
    monkeypatch.setattr(
        licensing_client, "_DEVICE_PRIVATE_KEY_PATH", license_dir / "device_private_key.bin"
    )
    return license_dir


def _license_payload(**overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "license_id": "id",
        "user_id": "id",
        "organization_id": "org-42",
        "device_id": "id",
        "device_public_key_hash": "hash",
        "product_code": "deepvac-insight",
        "edition_code": "professional",
        "features": [],
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "not_before": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key_id": "test-key",
        "license_version": 1,
    }
    payload.update(overrides)
    return payload


def _unreachable_hub(*_args, **_kwargs):
    raise licensing_client.ApiError("hub unreachable")


def test_current_organization_id_returns_none_without_a_cached_license(isolated_license_dir):
    assert licensing_client.current_organization_id() is None


def test_current_organization_id_reads_from_cached_license(isolated_license_dir):
    licensing_client.save_license(
        {
            "envelope_version": 1,
            "payload": _license_payload(),
            "signature": "x",
            "key_id": "test-key",
        }
    )

    assert licensing_client.current_organization_id() == "org-42"


def test_get_trusted_keys_fetches_once_then_uses_stored_copy(
    isolated_license_dir, signing_keypair, monkeypatch
):
    _private_key, raw_public = signing_keypair
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"test-key": raw_public}

    monkeypatch.setattr(licensing_client, "fetch_public_keys", fake_fetch)

    first = licensing_client.get_trusted_keys("test-key")
    second = licensing_client.get_trusted_keys("test-key")

    assert first == second == {"test-key": raw_public}
    assert len(calls) == 1


def test_get_trusted_keys_refetches_when_key_id_is_unknown(
    isolated_license_dir, signing_keypair, monkeypatch
):
    _private_key, raw_public = signing_keypair
    licensing_client.save_trusted_keys({"old-key": raw_public})
    monkeypatch.setattr(
        licensing_client,
        "fetch_public_keys",
        lambda: {"old-key": raw_public, "new-key": raw_public},
    )

    keys = licensing_client.get_trusted_keys("new-key")

    assert set(keys) == {"old-key", "new-key"}
    assert set(licensing_client.load_trusted_keys()) == {"old-key", "new-key"}


def test_has_valid_local_license_works_offline_once_keys_are_stored(
    isolated_license_dir, signing_keypair, monkeypatch
):
    private_key, raw_public = signing_keypair
    licensing_client.save_trusted_keys({"test-key": raw_public})
    licensing_client.save_license(_sign(_license_payload(), private_key))
    monkeypatch.setattr(licensing_client, "fetch_public_keys", _unreachable_hub)

    assert licensing_client.has_valid_local_license() is True


def test_has_valid_local_license_is_false_without_stored_keys_and_hub(
    isolated_license_dir, signing_keypair, monkeypatch
):
    private_key, _raw_public = signing_keypair
    licensing_client.save_license(_sign(_license_payload(), private_key))
    monkeypatch.setattr(licensing_client, "fetch_public_keys", _unreachable_hub)

    assert licensing_client.has_valid_local_license() is False


def test_save_and_load_account_info_roundtrip(isolated_license_dir, signing_keypair):
    private_key, _raw_public = signing_keypair
    envelope = _sign(_account_info_payload(), private_key)

    licensing_client.save_account_info(envelope)

    assert licensing_client.load_account_info() == envelope
