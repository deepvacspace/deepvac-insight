"""TLS identity for the collaboration host: a self-signed certificate that
peers pin when they pair."""

import contextlib
import datetime
import hashlib
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.common import DATA_DIR

HOST_TLS_DIR = DATA_DIR / "collab"
_CERT_NAME = "host_cert.pem"
_KEY_NAME = "host_key.pem"
_VALIDITY_DAYS = 3650
_FETCH_TIMEOUT_S = 5


def load_or_create_host_identity():
    """Returns (cert_path, key_path), creating a self-signed certificate on first use."""
    cert_path = HOST_TLS_DIR / _CERT_NAME
    key_path = HOST_TLS_DIR / _KEY_NAME
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    HOST_TLS_DIR.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, socket.gethostname() or "deepvac-host")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with contextlib.suppress(NotImplementedError):
        key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def host_cert_pem():
    cert_path, _ = load_or_create_host_identity()
    return cert_path.read_text(encoding="ascii")


def fingerprint(pem):
    """Returns the certificate's SHA-256 fingerprint as colon-separated hex."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def server_context():
    cert_path, key_path = load_or_create_host_identity()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return context


def pinned_context(pem):
    """Returns a client context that trusts only the given certificate."""
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def fetch_server_cert_pem(host, port):
    """Reads the certificate a host presents, without trusting it."""
    return ssl.get_server_certificate((host, port), timeout=_FETCH_TIMEOUT_S)
