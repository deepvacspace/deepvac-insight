"""How much disk a year of chamber recordings takes, by storage layout.

Reads DeepVac Control's daily recordings (.dat: QDataStream, big endian: an
empty QVariantMap header, QVector<double> times, QVector<QVector<double>>
states) and stores every day in the layouts we could use, measuring bytes per
sample:

    dat        the .dat file itself (8-byte doubles, uncompressed)
    json       Insight's runs.samples_json today: a JSON array of {channel: value} rows
    wide       SQLite, one row per sample and one REAL column per channel
    json_z     that JSON, zlib-compressed into a BLOB
    blob64_z   per run, one zlib-compressed float64 BLOB per channel (and the times)
    blob32_z   the same with float32 values (times stay float64)
    parquet    the full-day recording both applications write now (app/services/day_recorder.py)

then projects a year of continuous recording at the measured sample period.

    uv run python scripts/estimate_storage.py <folder with .dat files> [--runs-db data\\deepvac_runs.sqlite3]
"""

import argparse
import json
import sqlite3
import statistics
import struct
import tempfile
import zlib
from array import array
from pathlib import Path

LAYOUTS = ("dat", "json", "wide", "json_z", "blob64_z", "blob32_z", "parquet")
YEAR_S = 365 * 86400


def read_dat(path: Path):
    raw = path.read_bytes()
    pos = 0

    def u32():
        nonlocal pos
        (value,) = struct.unpack_from(">I", raw, pos)
        pos += 4
        return value

    def doubles(count):
        nonlocal pos
        values = array("d", raw[pos : pos + 8 * count])
        values.byteswap()  # QDataStream is big endian
        pos += 8 * count
        return values

    if u32() != 0:
        raise ValueError("the header is not empty")
    times = doubles(u32())
    states = [doubles(u32()) for _ in range(u32())]
    return times, states, len(raw)


def parquet_bytes(times, states, names, scratch: Path) -> int:
    from app.services import day_recorder

    path = scratch / "day.parquet"
    day_recorder.write_day(
        path, list(times), {n: list(s) for n, s in zip(names, states, strict=True)}
    )
    return path.stat().st_size


def day_sizes(path: Path, scratch: Path) -> dict:
    times, states, dat_bytes = read_dat(path)
    names = [f"ch{i}" for i in range(len(states))]
    n = len(times)
    db = scratch / "wide.sqlite3"
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        f"CREATE TABLE samples (run INTEGER, t REAL, {', '.join(c + ' REAL' for c in names)})"
    )
    conn.executemany(
        f"INSERT INTO samples VALUES (1, ?{', ?' * len(names)})",
        ([times[i], *(s[i] for s in states)] for i in range(n)),
    )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    text = json.dumps(
        [
            {"timestamp": times[i], **{names[k]: states[k][i] for k in range(len(states))}}
            for i in range(n)
        ]
    ).encode()
    times_z = len(zlib.compress(times.tobytes(), 6))
    steps = [b - a for a, b in zip(times, times[1:], strict=False) if 0 < b - a < 60]
    return {
        "day": path.stem,
        "samples": n,
        "channels": len(states),
        "period_s": statistics.median(steps) if steps else None,
        "dat": dat_bytes,
        "json": len(text),
        "wide": db.stat().st_size,
        "json_z": len(zlib.compress(text, 6)),
        "blob64_z": times_z + sum(len(zlib.compress(s.tobytes(), 6)) for s in states),
        "blob32_z": times_z + sum(len(zlib.compress(array("f", s).tobytes(), 6)) for s in states),
        "parquet": parquet_bytes(times, states, names, scratch),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dat_dir", type=Path)
    parser.add_argument(
        "--runs-db", type=Path, help="Insight's run database, to report its size today"
    )
    parser.add_argument("--min-samples", type=int, default=1000, help="skip shorter days")
    args = parser.parse_args(argv)

    days = []
    with tempfile.TemporaryDirectory() as scratch:
        for path in sorted(args.dat_dir.glob("*.dat")):
            try:
                sizes = day_sizes(path, Path(scratch))
            except (ValueError, struct.error) as exc:
                print(f"skipped {path.name}: {exc}")
                continue
            if sizes["samples"] >= args.min_samples:
                days.append(sizes)
                print(
                    f"{sizes['day']}: {sizes['samples']:6d} samples x {sizes['channels']} channels  "
                    + "  ".join(f"{k} {sizes[k] / sizes['samples']:.1f}" for k in LAYOUTS)
                    + "  B/sample"
                )
    if not days:
        raise SystemExit("No .dat days to measure.")
    period = statistics.median(d["period_s"] for d in days if d["period_s"])
    per_year = YEAR_S / period
    print(
        f"\n{len(days)} days, sample period {period:g} s: a year of continuous recording is {per_year:,.0f} samples"
    )
    print(f"{'layout':10s} {'B/sample':>9s} {'worst day':>10s} {'GB/year':>8s} {'worst':>7s}")
    for key in LAYOUTS:
        per = [d[key] / d["samples"] for d in days]
        median, worst = statistics.median(per), max(per)
        print(
            f"{key:10s} {median:9.1f} {worst:10.1f} {median * per_year / 1e9:8.2f} {worst * per_year / 1e9:7.2f}"
        )
    if args.runs_db and args.runs_db.exists():
        conn = sqlite3.connect(args.runs_db)
        runs, samples, text = conn.execute(
            "SELECT COUNT(*), SUM(samples), SUM(LENGTH(samples_json)) FROM runs"
        ).fetchone()
        conn.close()
        print(
            f"\n{args.runs_db.name}: {args.runs_db.stat().st_size / 1e6:.0f} MB, {runs} runs, {samples:,} samples, "
            f"samples_json {text / samples:.0f} B/sample"
        )


if __name__ == "__main__":
    main()
