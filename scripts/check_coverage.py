"""Checks test coverage against per-file and project floors."""

import json
import sys
from pathlib import Path

COVERAGE_JSON = Path(__file__).resolve().parent.parent / "coverage.json"

PER_FILE_THRESHOLDS = {
    "app/model/simulation.py": 85.0,
    "app/services/data_service.py": 65.0,
}
PROJECT_THRESHOLD = 30.0


def _percent_covered(file_summary):
    return file_summary["summary"]["percent_covered"]


def main():
    if not COVERAGE_JSON.exists():
        print(f"ERROR: {COVERAGE_JSON} not found -- run pytest with --cov-report=json first.")
        return 1

    data = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    failures = []

    for rel_path, threshold in PER_FILE_THRESHOLDS.items():
        match = next(
            (f for p, f in data["files"].items() if p.replace("\\", "/") == rel_path), None
        )
        if match is None:
            failures.append(
                f"{rel_path}: not found in coverage.json (was it imported by any test?)"
            )
            continue
        actual = _percent_covered(match)
        if actual < threshold:
            failures.append(f"{rel_path}: {actual:.1f}% < required {threshold:.1f}%")
        else:
            print(f"OK   {rel_path}: {actual:.1f}% >= {threshold:.1f}%")

    project_actual = data["totals"]["percent_covered"]
    if project_actual < PROJECT_THRESHOLD:
        failures.append(f"whole project: {project_actual:.1f}% < required {PROJECT_THRESHOLD:.1f}%")
    else:
        print(f"OK   whole project: {project_actual:.1f}% >= {PROJECT_THRESHOLD:.1f}%")

    if failures:
        print("\nCoverage check FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nAll coverage thresholds met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
