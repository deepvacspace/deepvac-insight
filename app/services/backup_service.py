"""Automated local backups for the app's SQLite databases."""

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.common import DATA_DIR

BACKUPS_DIR = DATA_DIR / "backups"
MAX_BACKUPS_PER_DB = 14


def _source_databases(data_dir):
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob("*.sqlite3"))


def _today_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def backup_database(source_path, reason="startup", backups_dir=None):
    """Write one timestamped online backup of source_path."""
    backups_dir = backups_dir or BACKUPS_DIR
    dest_dir = backups_dir / source_path.stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest_path = dest_dir / f"{source_path.stem}_{stamp}_{reason}.sqlite3"

    src_conn = sqlite3.connect(str(source_path))
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    return dest_path


def _prune(dest_dir, keep=MAX_BACKUPS_PER_DB):
    backups = sorted(dest_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime)
    excess = len(backups) - keep
    for old in backups[: max(0, excess)]:
        old.unlink(missing_ok=True)


def backup_all(force=False, data_dir=None, backups_dir=None):
    """Backs up every database."""
    data_dir = data_dir or DATA_DIR
    backups_dir = backups_dir or BACKUPS_DIR
    results = []
    stamp = _today_stamp()
    for source_path in _source_databases(data_dir):
        dest_dir = backups_dir / source_path.stem
        already_today = (
            not force
            and dest_dir.exists()
            and any(
                p.name.startswith(f"{source_path.stem}_{stamp}") for p in dest_dir.glob("*.sqlite3")
            )
        )
        if already_today:
            continue
        try:
            dest_path = backup_database(
                source_path, reason="manual" if force else "daily", backups_dir=backups_dir
            )
            _prune(dest_dir)
            results.append(dest_path)
        except Exception as exc:
            print(f"[backup] failed to back up {source_path.name}: {exc}")
    return results


def list_backups(backups_dir=None):
    """Returns the backups of each database."""
    backups_dir = backups_dir or BACKUPS_DIR
    out: dict[str, list[Path]] = {}
    if not backups_dir.exists():
        return out
    for sub in sorted(backups_dir.iterdir()):
        if sub.is_dir():
            out[sub.name] = sorted(sub.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime)
    return out


def restore_backup(db_name, backup_path, data_dir=None, backups_dir=None):
    """Restore <data_dir>/<db_name> from a backup file."""
    data_dir = data_dir or DATA_DIR
    backups_dir = backups_dir or BACKUPS_DIR
    target = data_dir / db_name
    if target.exists():
        dest_dir = backups_dir / target.stem
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        shutil.copyfile(target, dest_dir / f"{target.stem}_{stamp}_pre-restore.sqlite3")
    shutil.copyfile(backup_path, target)
    for suffix in ("-wal", "-shm"):
        sidecar = target.with_name(target.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
