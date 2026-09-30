"""Client side of the local-network collaboration: pairing with a host and
making signed requests to it over a pinned TLS connection."""

from base64 import urlsafe_b64encode
from urllib.parse import urlparse

from app.services import (
    collab_host_service,
    collab_tls,
    licensing_client,
    settings_service,
)
from app.services.collab_server import DEFAULT_PORT


class CollabError(Exception):
    pass


def normalize_host_url(text):
    raw = str(text).strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if not parsed.hostname:
        raise CollabError("Enter the host's address, for example 192.168.1.20:8765.")
    return f"https://{parsed.hostname}:{parsed.port or DEFAULT_PORT}"


def connection():
    return settings_service.load_collab_connection()


def fetch_host_certificate(host_url):
    """Returns the PEM certificate the host presents, so it can be verified and pinned."""
    parsed = urlparse(normalize_host_url(host_url))
    try:
        return collab_tls.fetch_server_cert_pem(parsed.hostname, parsed.port)
    except OSError as exc:
        raise CollabError(f"Could not reach {parsed.hostname}:{parsed.port}: {exc}") from exc


def _call(function, *args, cert_pem):
    try:
        return function(*args, ssl_context=collab_tls.pinned_context(cert_pem))
    except licensing_client.LicensingError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            raise CollabError(
                "The host's identity changed. Disconnect and pair again if you trust this host."
            ) from exc
        raise CollabError(str(exc)) from exc


def pair(host_url, join_code, display_name, cert_pem):
    url = normalize_host_url(host_url)
    _, public_raw = licensing_client.get_or_create_device_keypair()
    payload = {
        "join_code": join_code,
        "device_public_key": urlsafe_b64encode(public_raw).decode("ascii"),
        "display_name": display_name,
    }
    response = _call(
        licensing_client.request_json, "POST", f"{url}/pair", payload, cert_pem=cert_pem
    )
    settings_service.save_collab_connection(url, response["host_name"], cert_pem)


def connect_local(port, host_name, display_name, cert_pem):
    """Registers this device on its own host and connects to it over loopback."""
    _, public_raw = licensing_client.get_or_create_device_keypair()
    collab_host_service.register_peer(public_raw, display_name)
    settings_service.save_collab_connection(f"https://127.0.0.1:{port}", host_name, cert_pem)


def disconnect():
    settings_service.clear_collab_connection()


def request(method, path, payload=None):
    current = connection()
    if current is None:
        raise CollabError("Not connected to a collaboration host.")
    return _call(
        licensing_client.signed_request_json,
        method,
        f"{current['url']}{path}",
        payload,
        cert_pem=current["cert"],
    )
