# Contributing

## Local setup

Dependencies are declared in `pyproject.toml`, managed with
[uv](https://docs.astral.sh/uv/):

```powershell
uv sync --extra dev
uv run pre-commit install
```

`uv sync` creates a `.venv` with both the app's runtime dependencies and
the dev toolchain (pytest, ruff, mypy, pre-commit, pip-audit, pyinstaller).
`pre-commit install` wires up the git hook that runs ruff on every commit
(see `.pre-commit-config.yaml`).

## Local checks

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

`mypy` is currently scoped to five modules (`app/model/simulation.py`,
`app/services/data_service.py`, `app/services/auth_service.py`,
`app/services/backup_service.py`, `app/common.py`).

## Running specific test suites

```powershell
uv run pytest tests/unit           # fast, no I/O beyond tmp_path
uv run pytest tests/integration    # sqlite/filesystem via tmp_path
uv run pytest tests/ui             # PySide6 widgets, offscreen
uv run pytest -m "not integration" # skip the slower/checkpoint-loading tests
```

See [docs/testing.md](docs/testing.md) for how test isolation actually
works and what the golden simulation fixture is for.

## Coverage

```powershell
uv run pytest --cov=app --cov-branch --cov-report=term-missing --cov-report=json
uv run python scripts/check_coverage.py
```

`check_coverage.py` enforces per-file floors for the two highest-risk
modules (`app/model/simulation.py`: 85%, `app/services/data_service.py`:
65%) plus a whole-project floor (30%) -- see `scripts/check_coverage.py`
for the exact numbers and the reasoning for why those two files get a
much higher bar than everything else.

## Before opening a PR

```powershell
uv run pre-commit run --all-files
```

This is the same ruff format/lint pass the git hook runs, just against
every file instead of only what's staged.
