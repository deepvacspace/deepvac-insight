"""Client for the deepvac hub's cloud licensing and account-linking API."""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.common import DATA_DIR

PRODUCT_CODE = "deepvac-insight"

DEFAULT_API_BASE_URL = "http://localhost:8080/api/v1"


def api_base_url() -> str:
    return os.environ.get("DEEPVAC_LICENSING_API_URL", DEFAULT_API_BASE_URL).rstrip("/")


_LICENSE_DIR = DATA_DIR / "license"
_DEVICE_PRIVATE_KEY_PATH = _LICENSE_DIR / "device_private_key.bin"
_LICENSE_PATH = _LICENSE_DIR / "license.json"
_ACCOUNT_INFO_PATH = _LICENSE_DIR / "account_info.json"

_REQUIRED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "license_id",
        "user_id",
        "organization_id",
        "device_id",
        "device_public_key_hash",
        "product_code",
        "edition_code",
        "features",
        "issued_at",
        "not_before",
        "expires_at",
        "key_id",
        "license_version",
    }
)

_ACCOUNT_INFO_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "user_id",
        "email",
        "display_name",
        "organization_id",
        "organization_name",
    }
)


class LicensingError(Exception):
    """Base class for all errors raised by this module."""


class ApiError(LicensingError):
    """A non-2xx response from licensing-api, or a network failure."""


class InvalidLicenseError(LicensingError):
    """A license envelope that fails signature, binding, or validity checks."""


def _canonicalize(payload: dict, *, required_keys: frozenset = _REQUIRED_PAYLOAD_KEYS) -> bytes:
    payload_keys = frozenset(payload.keys())
    if payload_keys != required_keys:
        missing = required_keys - payload_keys
        extra = payload_keys - required_keys
        raise InvalidLicenseError(
            f"Payload has invalid shape (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def device_public_key_hash(device_public_key_raw: bytes) -> str:
    return urlsafe_b64encode(sha256(device_public_key_raw).digest()).decode("ascii")


def verify_envelope(envelope: dict, trusted_public_keys: dict[str, bytes]) -> dict:
    """Verifies a signed license envelope and returns its payload."""
    key_id = envelope.get("key_id")
    raw_public_key = trusted_public_keys.get(key_id)
    if raw_public_key is None:
        raise InvalidLicenseError(f"Unknown or untrusted signing key_id: {key_id!r}")

    payload = envelope["payload"]
    canonical_bytes = _canonicalize(payload)
    signature = urlsafe_b64decode(envelope["signature"])
    public_key = Ed25519PublicKey.from_public_bytes(raw_public_key)
    try:
        public_key.verify(signature, canonical_bytes)
    except InvalidSignature as exc:
        raise InvalidLicenseError("License signature verification failed.") from exc

    now = datetime.now(timezone.utc)
    not_before = _parse_iso8601(payload["not_before"])
    expires_at = _parse_iso8601(payload["expires_at"])
    if now < not_before:
        raise InvalidLicenseError("License is not valid yet (not_before is in the future).")
    if now >= expires_at:
        raise InvalidLicenseError("License has expired.")

    local_public_key_raw = load_device_public_key_raw()
    if local_public_key_raw is not None:
        expected_hash = device_public_key_hash(local_public_key_raw)
        if payload["device_public_key_hash"] != expected_hash:
            raise InvalidLicenseError(
                "License is bound to a different device than this installation's keypair."
            )

    return payload


def verify_account_info(envelope: dict, trusted_public_keys: dict[str, bytes]) -> dict:
    """Verifies a signed AccountInfo envelope and returns its payload."""
    key_id = envelope.get("key_id")
    raw_public_key = trusted_public_keys.get(key_id)
    if raw_public_key is None:
        raise InvalidLicenseError(f"Unknown or untrusted signing key_id: {key_id!r}")

    payload = envelope["payload"]
    canonical_bytes = _canonicalize(payload, required_keys=_ACCOUNT_INFO_REQUIRED_KEYS)
    signature = urlsafe_b64decode(envelope["signature"])
    public_key = Ed25519PublicKey.from_public_bytes(raw_public_key)
    try:
        public_key.verify(signature, canonical_bytes)
    except InvalidSignature as exc:
        raise InvalidLicenseError("Account info signature verification failed.") from exc

    return payload


def get_or_create_device_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    """Returns this installation's device keypair, generating one if needed."""
    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    if _DEVICE_PRIVATE_KEY_PATH.exists():
        raw = _DEVICE_PRIVATE_KEY_PATH.read_bytes()
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
    else:
        private_key = Ed25519PrivateKey.generate()
        raw = private_key.private_bytes(
            encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption()
        )
        _DEVICE_PRIVATE_KEY_PATH.write_bytes(raw)
        with contextlib.suppress(NotImplementedError):
            _DEVICE_PRIVATE_KEY_PATH.chmod(0o600)
    public_raw = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    return private_key, public_raw


def load_device_public_key_raw() -> bytes | None:
    if not _DEVICE_PRIVATE_KEY_PATH.exists():
        return None
    raw = _DEVICE_PRIVATE_KEY_PATH.read_bytes()
    private_key = Ed25519PrivateKey.from_private_bytes(raw)
    return private_key.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)


def load_license() -> dict | None:
    if not _LICENSE_PATH.exists():
        return None
    try:
        return json.loads(_LICENSE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_license(envelope: dict) -> None:
    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    _LICENSE_PATH.write_text(json.dumps(envelope), encoding="utf-8")


def load_account_info() -> dict | None:
    if not _ACCOUNT_INFO_PATH.exists():
        return None
    try:
        return json.loads(_ACCOUNT_INFO_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_account_info(envelope: dict) -> None:
    """Persists a signed AccountInfo envelope to local storage."""
    _LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    _ACCOUNT_INFO_PATH.write_text(json.dumps(envelope), encoding="utf-8")


def current_organization_id() -> str | None:
    """Returns the organization id from the locally cached license, if any."""
    envelope = load_license()
    if envelope is None:
        return None
    return envelope.get("payload", {}).get("organization_id")


def has_valid_local_license() -> bool:
    """True if a cached license exists and verifies against trusted keys."""
    envelope = load_license()
    if envelope is None:
        return False
    try:
        trusted_keys = fetch_public_keys()
        verify_envelope(envelope, trusted_keys)
    except LicensingError:
        return False
    return True


def _request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    timeout: float = 10.0,
    *,
    body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    if body is None:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local dev API)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        with contextlib.suppress(ValueError):
            detail = json.loads(body).get("detail", body)
        raise ApiError(f"{exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Could not reach licensing service at {url}: {exc.reason}") from exc


def fetch_public_keys() -> dict[str, bytes]:
    response = _request_json("GET", f"{api_base_url()}/licensing/public-keys")
    return {key["key_id"]: urlsafe_b64decode(key["public_key"]) for key in response.get("keys", [])}


@dataclass(frozen=True)
class ActivationStart:
    activation_id: str
    user_code: str
    verification_url: str
    expires_at: str
    polling_interval_seconds: int


def start_activation(
    product_code: str = PRODUCT_CODE, edition_code: str | None = None
) -> ActivationStart:
    payload: dict = {"product_code": product_code}
    if edition_code:
        payload["edition_code"] = edition_code
    response = _request_json("POST", f"{api_base_url()}/activations", payload)
    return ActivationStart(
        activation_id=response["activation_id"],
        user_code=response["user_code"],
        verification_url=response["verification_url"],
        expires_at=response["expires_at"],
        polling_interval_seconds=response["polling_interval_seconds"],
    )


def poll_activation_status(activation_id: str) -> str:
    response = _request_json("GET", f"{api_base_url()}/activations/{activation_id}")
    return response["status"]


def complete_activation(activation_id: str, display_name: str | None = None) -> tuple[dict, dict]:
    """Completes device activation and returns (license_payload, account_payload)."""
    _, public_raw = get_or_create_device_keypair()
    payload = {"device_public_key": urlsafe_b64encode(public_raw).decode("ascii")}
    if display_name:
        payload["display_name"] = display_name
    response = _request_json(
        "POST", f"{api_base_url()}/activations/{activation_id}/complete", payload
    )
    trusted_keys = fetch_public_keys()
    license_envelope = response["license"]
    account_envelope = response["account"]
    license_payload = verify_envelope(license_envelope, trusted_keys)
    account_payload = verify_account_info(account_envelope, trusted_keys)
    save_license(license_envelope)
    save_account_info(account_envelope)
    return license_payload, account_payload


@dataclass(frozen=True)
class AccountLinkStart:
    link_id: str
    user_code: str
    verification_url: str
    expires_at: str
    polling_interval_seconds: int


def start_account_link(organization_id: str) -> AccountLinkStart:
    payload = {"organization_id": organization_id}
    response = _request_json("POST", f"{api_base_url()}/account-links", payload)
    return AccountLinkStart(
        link_id=response["link_id"],
        user_code=response["user_code"],
        verification_url=response["verification_url"],
        expires_at=response["expires_at"],
        polling_interval_seconds=response["polling_interval_seconds"],
    )


def poll_account_link_status(link_id: str) -> str:
    response = _request_json("GET", f"{api_base_url()}/account-links/{link_id}")
    return response["status"]


def complete_account_link(link_id: str) -> dict:
    """Completes an account-link request and returns the verified account payload."""
    envelope = _request_json("POST", f"{api_base_url()}/account-links/{link_id}/complete")
    trusted_keys = fetch_public_keys()
    account_payload = verify_account_info(envelope, trusted_keys)
    save_account_info(envelope)
    return account_payload


def _device_signed_headers(body: bytes) -> dict[str, str]:
    private_key, public_raw = get_or_create_device_keypair()
    signature = private_key.sign(body)
    return {
        "X-Device-Key-Hash": device_public_key_hash(public_raw),
        "X-Device-Signature": urlsafe_b64encode(signature).decode("ascii"),
    }


def signed_request_json(method: str, url: str, payload: dict | None = None) -> dict:
    """Calls a device-signature-authenticated hub endpoint."""
    body = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        if payload is not None
        else b""
    )
    return _request_json(method, url, body=body, extra_headers=_device_signed_headers(body))
