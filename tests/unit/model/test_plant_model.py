"""Structural tests for GRUModel (app/model/simulation.py) -- shape,
dtype, batching, and determinism, using a small instantiated-in-test model
with seeded/fixed weights. Does NOT load the production model.pt checkpoint
-- that's a separate, explicitly `integration`-marked test, since it's the
only one that needs the real ~170 KB file on disk."""

import numpy as np
import pytest
import torch

from app.model.simulation import DEFAULT_CHECKPOINT, GRUModel

pytestmark = pytest.mark.unit


def _tiny_model(input_dim=5, hidden_dim=8, num_layers=1, dropout=0.0, seed=0):
    torch.manual_seed(seed)
    model = GRUModel(
        input_dim=input_dim, hidden_dim=hidden_dim, num_layers=num_layers, dropout=dropout
    )
    model.eval()
    return model


def test_output_shape_single_batch():
    model = _tiny_model(input_dim=5, hidden_dim=8)
    x = torch.zeros(1, 10, 5)  # (batch, seq_len, input_dim)
    with torch.no_grad():
        out = model(x)
    assert out.shape == (1, 1)


def test_output_shape_multi_batch():
    model = _tiny_model(input_dim=5, hidden_dim=8)
    x = torch.zeros(4, 10, 5)
    with torch.no_grad():
        out = model(x)
    assert out.shape == (4, 1)


def test_output_dtype_is_float():
    model = _tiny_model()
    x = torch.zeros(1, 10, 5)
    with torch.no_grad():
        out = model(x)
    assert out.dtype == torch.float32


def test_output_is_finite_for_zero_input():
    model = _tiny_model()
    x = torch.zeros(2, 6, 5)
    with torch.no_grad():
        out = model(x)
    assert torch.isfinite(out).all()


def test_output_is_finite_for_random_input():
    torch.manual_seed(1)
    model = _tiny_model(seed=2)
    x = torch.randn(3, 12, 5)
    with torch.no_grad():
        out = model(x)
    assert torch.isfinite(out).all()


def test_deterministic_repeated_inference_in_eval_mode():
    model = _tiny_model(dropout=0.5, seed=3)  # nonzero dropout to prove eval() disables it
    x = torch.randn(2, 8, 5)
    with torch.no_grad():
        out1 = model(x)
        out2 = model(x)
    assert torch.equal(out1, out2)


def test_two_identically_seeded_models_agree():
    x = torch.randn(2, 8, 5)
    model_a = _tiny_model(seed=42)
    with torch.no_grad():
        out_a = model_a(x)
    model_b = _tiny_model(seed=42)
    with torch.no_grad():
        out_b = model_b(x)
    assert torch.equal(out_a, out_b)


def test_different_seeds_generally_disagree():
    x = torch.randn(2, 8, 5)
    model_a = _tiny_model(seed=1)
    with torch.no_grad():
        out_a = model_a(x)
    model_b = _tiny_model(seed=2)
    with torch.no_grad():
        out_b = model_b(x)
    assert not torch.equal(out_a, out_b)


def test_wrong_input_feature_dim_raises():
    model = _tiny_model(input_dim=5, hidden_dim=8)
    x = torch.zeros(1, 10, 3)  # model expects 5 features, not 3
    with pytest.raises(RuntimeError), torch.no_grad():
        model(x)


def test_runs_on_cpu_explicitly():
    model = _tiny_model().to(torch.device("cpu"))
    x = torch.zeros(1, 5, 5, device=torch.device("cpu"))
    with torch.no_grad():
        out = model(x)
    assert out.device.type == "cpu"


def test_hidden_dim_and_num_layers_affect_parameter_count():
    small = _tiny_model(hidden_dim=4, num_layers=1)
    large = _tiny_model(hidden_dim=16, num_layers=2)
    small_params = sum(p.numel() for p in small.parameters())
    large_params = sum(p.numel() for p in large.parameters())
    assert large_params > small_params


def test_forward_return_type_is_tensor_not_numpy():
    model = _tiny_model()
    x = torch.zeros(1, 5, 5)
    with torch.no_grad():
        out = model(x)
    assert isinstance(out, torch.Tensor)


@pytest.mark.integration
def test_production_checkpoint_loads_and_infers():
    """Loads the real app/model/model.pt once, runs one minimal forward
    pass through the full load_model()/predict_delta_t1() path, and checks
    only shape/finiteness -- not an exact learned value, since that would
    also pin down the (separately maintained) feature preprocessing
    contract. See tests/integration/test_simulation_golden.py for the
    fixture that actually pins down real numerical behavior end to end."""
    from app.model.simulation import DEFAULT_FEATURE_NAMES, load_model, predict_delta_t1

    assert DEFAULT_CHECKPOINT.exists(), f"production checkpoint missing: {DEFAULT_CHECKPOINT}"
    device = torch.device("cpu")
    model, checkpoint = load_model(DEFAULT_CHECKPOINT, device)

    window_steps = int(checkpoint.get("window_steps", 60))
    feature_names = list(checkpoint.get("feature_names", DEFAULT_FEATURE_NAMES))
    feature_window = np.zeros((window_steps, len(feature_names)), dtype=np.float32)

    delta = predict_delta_t1(model, checkpoint, feature_window, device)
    assert isinstance(delta, float)
    assert np.isfinite(delta)
