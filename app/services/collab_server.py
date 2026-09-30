"""Embedded local-network collaboration host: a small HTTPS server that
paired Deepvac Insight installations use to share annotations, variable
rules, run metadata and alarm events."""

import hmac
import json
import re
import secrets
import socket
import threading
import time
from base64 import urlsafe_b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services import collab_host_service, collab_tls

DEFAULT_PORT = 8765
JOIN_CODE_TTL_S = 600
JOIN_CODE_MAX_FAILURES = 5
_JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_MAX_BODY_BYTES = 1_000_000
_HANDSHAKE_TIMEOUT_S = 10
_ED25519_KEY_BYTES = 32
_UID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _optional(convert):
    return lambda value: None if value is None else convert(value)


KIND_SPECS = {
    "annotation": {
        "mutable": False,
        "fields": {
            "run_uid": str,
            "user_name": str,
            "x0": float,
            "x1": float,
            "label": str,
            "color": str,
            "created_at": str,
        },
    },
    "variable_rule": {
        "mutable": False,
        "fields": {
            "run_uid": str,
            "user_name": str,
            "name": str,
            "channel": str,
            "lo": _optional(float),
            "hi": _optional(float),
            "color": str,
            "created_at": str,
        },
    },
    "run_metadata": {
        "mutable": True,
        "fields": {
            "shared_by": str,
            "id": str,
            "group": _optional(str),
            "samples": _optional(int),
            "duration_s": _optional(float),
            "mae": _optional(float),
            "cost": _optional(float),
            "tail_mae": _optional(float),
            "overshoot": _optional(float),
            "settle_time_s": _optional(float),
            "start_time": _optional(str),
            "end_time": _optional(str),
            "chamber": _optional(str),
            "test_profile": _optional(str),
        },
    },
    "alarm_event": {
        "mutable": True,
        "fields": {
            "chamber_ref": str,
            "chamber_name": _optional(str),
            "rule_name": str,
            "variable": str,
            "severity": str,
            "trigger_value": _optional(float),
            "triggered_at": str,
            "cleared_at": _optional(str),
            "acknowledged_at": _optional(str),
            "acknowledged_by": _optional(str),
            "comment": str,
        },
    },
}


class _HttpError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _normalize_code(code):
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


class CollabServer:
    def __init__(self):
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()
        self._join_code = None
        self._join_code_expires = 0.0
        self._join_code_failures = 0

    @property
    def host_name(self):
        return socket.gethostname()

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else None

    @property
    def cert_pem(self):
        return collab_tls.host_cert_pem()

    def is_running(self):
        return self._httpd is not None

    def start(self, port=DEFAULT_PORT):
        if self._httpd is not None:
            return
        httpd = _TlsServer(("0.0.0.0", port), _Handler, collab_tls.server_context())
        httpd.collab = self
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None
        with self._lock:
            self._join_code = None

    def new_join_code(self):
        raw = "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(8))
        with self._lock:
            self._join_code = raw
            self._join_code_expires = time.monotonic() + JOIN_CODE_TTL_S
            self._join_code_failures = 0
        return f"{raw[:4]}-{raw[4:]}"

    def consume_join_code(self, code):
        """Returns True and invalidates the code if it is the current, unexpired join code."""
        with self._lock:
            if self._join_code is None or time.monotonic() > self._join_code_expires:
                self._join_code = None
                return False
            if hmac.compare_digest(_normalize_code(code), self._join_code):
                self._join_code = None
                return True
            self._join_code_failures += 1
            if self._join_code_failures >= JOIN_CODE_MAX_FAILURES:
                self._join_code = None
            return False


class _TlsServer(ThreadingHTTPServer):
    def __init__(self, address, handler, tls_context):
        self._tls_context = tls_context
        super().__init__(address, handler)

    def get_request(self):
        sock, address = super().get_request()
        wrapped = self._tls_context.wrap_socket(
            sock, server_side=True, do_handshake_on_connect=False
        )
        return wrapped, address

    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "DeepvacCollab"

    def setup(self):
        self.request.settimeout(_HANDSHAKE_TIMEOUT_S)
        self.request.do_handshake()
        self.request.settimeout(None)
        super().setup()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            body = self._read_body()
            path = self.path.split("?", 1)[0]
            if method == "POST" and path == "/pair":
                self._send(200, self._pair(body))
                return
            self._authenticate(body)
            self._send(*self._route(method, path, body))
        except _HttpError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception:
            self._send(500, {"detail": "Internal error."})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            raise _HttpError(413, "Request body too large.")
        return self.rfile.read(length) if length else b""

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _HttpError(400, "Body must be JSON.") from exc
        if not isinstance(payload, dict):
            raise _HttpError(400, "Body must be a JSON object.")
        return payload

    def _pair(self, body):
        payload = self._json(body)
        try:
            public_key = urlsafe_b64decode(str(payload["device_public_key"]))
        except (KeyError, ValueError) as exc:
            raise _HttpError(400, "A valid device_public_key is required.") from exc
        if len(public_key) != _ED25519_KEY_BYTES:
            raise _HttpError(400, "A valid device_public_key is required.")
        if not self.server.collab.consume_join_code(payload.get("join_code", "")):
            raise _HttpError(403, "Invalid or expired join code.")
        collab_host_service.register_peer(public_key, str(payload.get("display_name") or ""))
        return {"host_name": self.server.collab.host_name}

    def _authenticate(self, body):
        key_hash = self.headers.get("X-Device-Key-Hash")
        signature_b64 = self.headers.get("X-Device-Signature")
        if not key_hash or not signature_b64:
            raise _HttpError(401, "Missing device signature headers.")
        peer = collab_host_service.get_peer(key_hash)
        if peer is None:
            raise _HttpError(403, "This device is not paired with this host.")
        try:
            signature = urlsafe_b64decode(signature_b64)
            Ed25519PublicKey.from_public_bytes(peer["public_key"]).verify(signature, body)
        except (InvalidSignature, ValueError) as exc:
            raise _HttpError(401, "Device signature verification failed.") from exc
        return peer

    def _route(self, method, path, body):
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "items":
            spec = KIND_SPECS.get(parts[1])
            if spec is None:
                raise _HttpError(404, "Unknown item kind.")
            kind = parts[1]
            if method == "GET" and len(parts) == 2:
                return 200, {
                    "items": collab_host_service.list_items(kind),
                    "deleted": collab_host_service.list_deleted_uids(kind),
                }
            if method == "POST" and len(parts) == 2:
                uid, data = self._item(self._json(body), spec)
                collab_host_service.put_item(kind, uid, data, spec["mutable"])
                return 201, {"uid": uid}
            if method == "POST" and len(parts) == 4 and parts[3] == "delete":
                return 200, {"deleted": collab_host_service.delete_item(kind, parts[2])}
        raise _HttpError(404, "Not found.")

    def _item(self, payload, spec):
        uid = str(payload.get("uid", ""))
        if not _UID_PATTERN.fullmatch(uid):
            raise _HttpError(400, "Invalid uid.")
        data = {}
        try:
            for name, convert in spec["fields"].items():
                data[name] = convert(payload[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpError(400, "Invalid item.") from exc
        return uid, data


def local_addresses(port):
    """Returns the https addresses this host is reachable at on its network interfaces."""
    addresses = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if not ip.startswith("127.") and f"https://{ip}:{port}" not in addresses:
            addresses.append(f"https://{ip}:{port}")
    return addresses
