"""Regenerates the simulation golden fixture."""

import argparse
import json
import sys
from argparse import Namespace
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.model.simulation import (
    DEFAULT_CHECKPOINT,
    DEFAULT_FEATURE_NAMES,
    load_model,
    simulate_candidate,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/simulation/golden_case_1.json"
)

CANDIDATE = {"candidate_id": 1, "kp": 7.0, "ki": 700.0, "kd": 10.0}

ARGS = dict(
    start_temp=27.0,
    target_temp=60.0,
    duration_s=20.0,
    dt_s=2.0,
    precondition_ref=None,
    u_min=-1.0,
    u_max=1.0,
    control_feature_scale=100.0,
    pid_i_reverse_mul=0.333,
    pid_period_s=0.1,
    initial_i=0.0,
    initial_d=0.0,
    initial_p=0.0,
    tail_window_s=20.0,
    near_band=2.0,
    settle_band=0.5,
    w_tail_mae=1.0,
    w_overshoot_max=10.0,
    w_tail_std=0.5,
    w_final_error=0.5,
    w_invalid=1_000_000.0,
    max_abs_temp=200.0,
)


def generate():
    device = torch.device("cpu")
    model, checkpoint = load_model(DEFAULT_CHECKPOINT, device)
    window_steps = int(checkpoint.get("window_steps", 60))
    feature_names = list(checkpoint.get("feature_names", DEFAULT_FEATURE_NAMES))

    metrics, traj = simulate_candidate(
        **CANDIDATE,
        model=model,
        checkpoint=checkpoint,
        feature_names=feature_names,
        window_steps=window_steps,
        args=Namespace(**ARGS),
        device=device,
        save_trajectory=True,
    )

    fixture = {
        "schema_version": 1,
        "checkpoint": str(DEFAULT_CHECKPOINT.name),
        "candidate": CANDIDATE,
        "args": ARGS,
        "window_steps": window_steps,
        "feature_names": feature_names,
        "tolerance": {"rel": 1e-4, "abs": 1e-6},
        "expected": {
            "temp": [round(float(v), 8) for v in traj["temp"]],
            "elapsed_s": [round(float(v), 8) for v in traj["elapsed_s"]],
            "metrics": {
                k: (
                    round(float(v), 8)
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                    else v
                )
                for k, v in metrics.items()
            },
        },
    }
    return fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write the fixture (a dry run without this only prints it).",
    )
    args = parser.parse_args()

    fixture = generate()

    if not args.confirm:
        print(json.dumps(fixture, indent=2))
        print(f"\nDry run -- not written. Re-run with --confirm to overwrite {FIXTURE_PATH}")
        return

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
