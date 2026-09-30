"""Stable cross-machine run identifiers derived from each run's samples file."""

import hashlib
from pathlib import Path

from app.services import data_service

_HEAD_BYTES = 65536


def file_fingerprint(path):
    path = Path(path)
    digest = hashlib.sha256()
    digest.update(str(path.stat().st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(_HEAD_BYTES))
    return digest.hexdigest()[:32]


def run_fingerprints():
    """Returns {run_key: fingerprint} for every cached run whose samples file exists."""
    conn = data_service.connect_cache()
    try:
        rows = conn.execute("SELECT key, samples_path FROM runs").fetchall()
    finally:
        conn.close()
    fingerprints = {}
    for row in rows:
        try:
            fingerprints[row["key"]] = file_fingerprint(row["samples_path"])
        except OSError:
            continue
    return fingerprints
