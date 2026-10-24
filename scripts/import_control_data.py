"""Imports control2-client's experiment data and chamber configuration into Insight.

What it copies (never deletes anything; re-running it skips what is already here):
- runs: every row of control2-client's deepvac_runs.sqlite3 whose key Insight
  doesn't have (samples, stored analysis, manifest, identity), source unchanged;
- test profiles: by name, with their steps and PLC program (program_json);
- the chamber PLC (settings.json "tcp_server") as a chamber with protocol "plc";
- settings.json itself, as data/control_settings.json: the chamber's state,
  program, alarm and PLC settings definitions the PLC gate and the agent API use.

Before writing, each Insight database it touches is copied to
data/backups/<name>/<name>_before_control_import_<time>.sqlite3.

    uv run python scripts/import_control_data.py --control ../../control2-client [--dry-run]
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.common import DATA_DIR  # noqa: E402
from app.services import chambers_service, data_service, test_profiles_service  # noqa: E402

CONTROL_SETTINGS_FILE = "control_settings.json"


def find_data_dir(control: Path) -> Path:
    """The C++ app keeps its databases next to its executable (build/<kit>/data)."""
    candidates = [control / "data"] + sorted((control / "build").glob("*/data"))
    for candidate in candidates:
        if (candidate / "deepvac_runs.sqlite3").exists():
            return candidate
    raise SystemExit(
        f"No deepvac_runs.sqlite3 under {control}/data or {control}/build/*/data; pass --data-dir."
    )


def backup(db_path: Path, stamp: str) -> Path:
    target_dir = DATA_DIR / "backups" / db_path.stem
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{db_path.stem}_before_control_import_{stamp}.sqlite3"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def import_runs(source_db: Path, dry_run: bool) -> tuple[int, int]:
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    if dry_run:
        dst = sqlite3.connect(f"file:{data_service.cache_path()}?mode=ro", uri=True)
        dst.row_factory = sqlite3.Row
    else:
        dst = data_service.connect_cache()  # applies the schema migrations
    try:
        dst_columns = [r["name"] for r in dst.execute("PRAGMA table_info(runs)")]
        src_columns = [r["name"] for r in src.execute("PRAGMA table_info(runs)")]
        columns = [c for c in src_columns if c in dst_columns]
        existing = {r["key"] for r in dst.execute("SELECT key FROM runs")}
        inserted = skipped = 0
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders})"
        for row in src.execute(f"SELECT {', '.join(columns)} FROM runs"):
            if row["key"] in existing:
                skipped += 1
                continue
            if not dry_run:
                dst.execute(sql, tuple(row))
            inserted += 1
        if not dry_run:
            dst.commit()
        return inserted, skipped
    finally:
        src.close()
        dst.close()


def import_test_profiles(source_db: Path, dry_run: bool) -> tuple[int, int]:
    if not source_db.exists():
        return 0, 0
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    has_program = "program_json" in {
        r["name"] for r in src.execute("PRAGMA table_info(test_profiles)")
    }
    existing = {p["name"] for p in test_profiles_service.list_profiles()}
    inserted = skipped = 0
    try:
        for profile in src.execute("SELECT * FROM test_profiles ORDER BY id"):
            if profile["name"] in existing:
                skipped += 1
                continue
            steps = [
                {
                    "setpoint_temp": s["setpoint_temp"],
                    "setpoint_pressure": s["setpoint_pressure"],
                    "duration_s": s["duration_s"],
                    "label": s["label"],
                }
                for s in src.execute(
                    "SELECT * FROM test_profile_steps WHERE profile_id = ? ORDER BY step_order",
                    (profile["id"],),
                )
            ]
            inserted += 1
            if dry_run:
                continue
            added = test_profiles_service.add_profile(
                profile["name"], profile["description"], steps, created_by=profile["created_by"]
            )
            if has_program and profile["program_json"]:
                conn = test_profiles_service.connect_test_profiles()
                try:
                    conn.execute(
                        "UPDATE test_profiles SET program_json = ? WHERE id = ?",
                        (profile["program_json"], added["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
        return inserted, skipped
    finally:
        src.close()


def load_control_settings(settings_path: Path) -> dict:
    text = settings_path.read_bytes().decode("utf-8", errors="replace")
    # the C++ settings file has "°" saved in a single-byte code page: restore it
    return json.loads(text.replace("�", "°"))


def import_plc_chamber(settings: dict, dry_run: bool) -> str:
    tcp = settings.get("tcp_server") or {}
    host, port = tcp.get("server_ip"), tcp.get("server_port")
    if not host or not port:
        return "settings.json has no tcp_server entry"
    for chamber in chambers_service.list_chambers():
        if chamber["host"] == host and int(chamber["port"]) == int(port):
            return f"already registered as '{chamber['name']}'"
    name = (settings.get("opc_settings") or {}).get("root_name") or "Chamber PLC"
    names = {c["name"] for c in chambers_service.list_chambers()}
    if name in names:
        name = f"{name} (PLC)"
    if not dry_run:
        chambers_service.add_chamber(name, host, int(port), protocol="plc")
    return f"added '{name}' at {host}:{port}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--control", type=Path, default=Path(__file__).resolve().parents[3] / "control2-client"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="control2-client data directory (default: found under --control)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be imported, change nothing"
    )
    args = parser.parse_args()

    control = args.control.resolve()
    data_dir = (args.data_dir or find_data_dir(control)).resolve()
    settings_path = control / "settings.json"
    print(f"control2-client data: {data_dir}")
    print(f"Insight data:         {DATA_DIR}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not args.dry_run:
        for db in (
            data_service.CACHE_DB,
            test_profiles_service.TEST_PROFILES_DB,
            chambers_service.CHAMBERS_DB,
        ):
            if db.exists():
                print(f"backup:  {backup(db, stamp)}")

    inserted, skipped = import_runs(data_dir / "deepvac_runs.sqlite3", args.dry_run)
    print(f"runs:          {inserted} imported, {skipped} already present")
    inserted, skipped = import_test_profiles(
        data_dir / "deepvac_test_profiles.sqlite3", args.dry_run
    )
    print(f"test profiles: {inserted} imported, {skipped} already present")
    if settings_path.exists():
        settings = load_control_settings(settings_path)
        print(f"PLC chamber:   {import_plc_chamber(settings, args.dry_run)}")
        if not args.dry_run:
            target = DATA_DIR / CONTROL_SETTINGS_FILE
            target.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"settings:      {target}")
    else:
        print(f"settings:      {settings_path} not found, skipped")
    if args.dry_run:
        print("dry run: nothing was changed")


if __name__ == "__main__":
    main()
