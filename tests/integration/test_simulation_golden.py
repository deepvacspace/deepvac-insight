"""Golden regression test of the closed-loop simulation."""

import json
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest
import torch

from app.model.simulation import DEFAULT_CHECKPOINT, load_model, simulate_candidate

pytestmark = pytest.mark.integration

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures/simulation/golden_case_1.json"


@pytest.fixture(scope="module")
def golden():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_golden_fixture_schema_is_understood(golden):
    assert golden["schema_version"] == 1
    assert golden["checkpoint"] == DEFAULT_CHECKPOINT.name


def test_closed_loop_trajectory_matches_golden_fixture(golden):
    assert DEFAULT_CHECKPOINT.exists(), f"production checkpoint missing: {DEFAULT_CHECKPOINT}"
    device = torch.device("cpu")
    model, checkpoint = load_model(DEFAULT_CHECKPOINT, device)

    metrics, traj = simulate_candidate(
        **golden["candidate"],
        model=model,
        checkpoint=checkpoint,
        feature_names=golden["feature_names"],
        window_steps=golden["window_steps"],
        args=Namespace(**golden["args"]),
        device=device,
        save_trajectory=True,
    )

    tol = golden["tolerance"]
    expected = golden["expected"]

    actual_temp = list(traj["temp"])
    assert actual_temp == pytest.approx(expected["temp"], rel=tol["rel"], abs=tol["abs"])
    assert list(traj["elapsed_s"]) == pytest.approx(expected["elapsed_s"])

    for key, expected_value in expected["metrics"].items():
        actual_value = metrics[key]
        if isinstance(expected_value, bool):
            assert actual_value == expected_value, key
        elif isinstance(expected_value, (int, float)):
            assert actual_value == pytest.approx(expected_value, rel=tol["rel"], abs=tol["abs"]), (
                key
            )
        else:
            assert actual_value == expected_value, key

    assert np.isfinite(actual_temp).all()
