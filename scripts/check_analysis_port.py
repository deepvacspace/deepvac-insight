"""Compares the Python Analysis Core with summaries the C++ app stored."""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.analysis import ANALYSIS_VERSION, analyse_experiment  # noqa: E402
from app.core.analysis.runsamples import experiment_input_from_json  # noqa: E402


def differences(expected, actual, path="", rel=1e-9, abs_tol=1e-9):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in actual or key not in expected:
                yield f"{path}.{key}: {'missing' if key not in actual else 'extra'}"
            else:
                yield from differences(expected[key], actual[key], f"{path}.{key}", rel, abs_tol)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            yield f"{path}: length {len(expected)} != {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual, strict=False)):
            yield from differences(e, a, f"{path}[{i}]", rel, abs_tol)
    elif (
        isinstance(expected, (int, float))
        and isinstance(actual, (int, float))
        and not isinstance(expected, bool)
    ):
        if not math.isclose(expected, actual, rel_tol=rel, abs_tol=abs_tol):
            yield f"{path}: {expected!r} != {actual!r}"
    elif expected != actual:
        yield f"{path}: {expected!r} != {actual!r}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show", type=int, default=5, help="differences printed per failing run")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    query = "SELECT key, summary_json, samples_json FROM runs WHERE summary_json LIKE ?"
    params = [f'%"analysis_version":{ANALYSIS_VERSION},%']
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    checked = failed = 0
    for key, summary_json, samples_json in conn.execute(query, params):
        expected = json.loads(summary_json)
        actual = analyse_experiment(experiment_input_from_json(samples_json)).to_json()
        actual = json.loads(json.dumps(actual))
        diffs = list(differences(expected, actual))
        checked += 1
        if diffs:
            failed += 1
            print(f"\n{key}: {len(diffs)} differences")
            for line in diffs[: args.show]:
                print("   ", line)
    print(f"\nchecked {checked} runs, {failed} differ")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
