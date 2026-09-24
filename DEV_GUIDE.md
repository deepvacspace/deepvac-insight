# DeepVac Insight — Developer Guide

> Internal engineering documentation. Audience: engineers joining or working
> in this repo. For an end-user walkthrough of the app itself, see the
> DeepVac Visualization User Guide. This file is gitignored — it is
> maintained here for convenience and mirrored to Confluence.

---

## 1. What this is

DeepVac Insight (package name `deepvac-insight`, product name "DeepVac
Desktop") is a **PySide6 (Qt6) desktop application** for reviewing
thermal-chamber PID controller test runs, running closed-loop GRU
simulations without hardware, and monitoring/controlling a live chamber
over TCP. It is one of at least two related repos:

| Repo | Role |
|---|---|
| `insight` (this repo) | The desktop app itself |
| `hub` (sibling, `../hub`) | Cloud licensing control plane — device-code activation, org/license management |
| the DeepVac training pipeline (referenced by `workspace_root()` in `data_service.py`, dirs `optimization/` / `gru/`) | Produces the run data (`run_samples.csv`, `run_summary.csv`, `band_metrics.csv`) and the GRU checkpoint this app reads |

The app never trains anything itself — it's a viewer/operator console over
data and a model produced elsewhere.

### Tech stack

| Concern | Library | Version (pinned in `pyproject.toml`) |
|---|---|---|
| UI framework | PySide6 (Qt6) | 6.11.1 |
| Charts | pyqtgraph | 0.14.0 |
| Data handling | pandas / numpy | 2.3.3 / 2.2.6 |
| Report export | openpyxl (.xlsx) | 3.1.5 |
| GRU inference | torch | 2.12.1 |
| OPC UA server | asyncua | 2.0.1 |
| Crypto (license keypair) | cryptography | 43.0.1 |
| Python | CPython | 3.10 (`.python-version`, `requires-python >=3.10`) |
| Packaging | uv (env/deps), PyInstaller + Inno Setup (distributable) | — |

### Use cases

**Controls / R&D engineer — offline analysis**
- Browse historical PID test runs, drill into one (Dashboard → Runs → Analysis)
- Compare two or more runs on the same chart to validate a tuning change actually helped
- Build derived/computed channels (error signal, heating rate, custom formulas) without re-exporting data
- Annotate a run's timeline and set acceptable min/max bands per channel for review
- Audit data-quality issues (missing columns, timestamp gaps, implausible readings) on imported runs

**Controls / R&D engineer — simulation**
- Run the trained GRU controller against a synthetic thermal environment to test candidate PID gains before touching real hardware

**Operator — live chamber work**
- Connect to a physical or simulated chamber and stream/chart its data in real time
- Record a live session and save it into the run database as a normal run for later analysis
- Send a one-off manual setpoint, or run a scripted multi-step test profile against the chamber
- Configure alarm rules (thresholds, deadband, delay) and acknowledge/export alarm history

**Downstream tooling / integration**
- Re-broadcast live chamber data as a standard OPC UA feed so other plant/lab software can subscribe without talking to this app directly

**Reporting**
- Generate and export a spreadsheet report of any run's full sample data for sharing outside the app

**Org admin / IT (via the `hub` sibling system)**
- Approve device activation for new installations and manage per-org licensing, independent of end-user accounts in this app

---

## 2. Repository layout

```text
insight/
├── main.py                    # entry point: python main.py
├── pyproject.toml             # deps, ruff/mypy/pytest/coverage config
├── uv.lock                    # locked dependency graph (uv-managed)
├── deepvac.spec                # PyInstaller build spec
├── app/                        # the application package
│   ├── app.py                  # QApplication bootstrap, splash, license gate, login flow, main()
│   ├── main_window.py          # DeepVacDesktop — the main window (mixin composition root)
│   ├── common.py                # shared constants, resolve_app_paths(), SVG icon helpers
│   ├── title_bar.py             # frameless window custom title bar
│   ├── tab_system.py            # tab bar / split-pane editor area (EditorArea)
│   ├── chart_widget.py          # pyqtgraph chart infra (crosshair sync, box zoom)
│   ├── run_tab.py               # Analysis tab page (RunTabPage) + SimWorker
│   ├── login_window.py, license_activation_window.py, profile_dialog.py
│   ├── chambers_dialog.py, derived_variables_dialog.py, test_profiles_dialog.py,
│   │   alarm_history_dialog.py  # modal dialogs, one per feature
│   ├── services/                 # business logic, no Qt widgets (see §4.2)
│   ├── views/                     # one mixin module per sidebar page (see §4.1)
│   └── model/
│       ├── model.pt              # bundled trained GRU checkpoint (tracked in git, ~170KB)
│       └── simulation.py         # GRU + PID/Diff simulation engine (see §4.3)
├── resources/                     # icons, logo, window icon, i18n/*.ts,*.qm
├── data/                          # gitignored: sqlite DBs, backups, reports (see §5)
├── tcp/
│   └── dummy_chamber_server.py    # fake chamber TCP server for local dev/testing
├── scripts/
│   ├── check_coverage.py          # enforces per-file + whole-project coverage floors
│   └── update_simulation_golden.py# regenerates the golden simulation fixture (rare, reviewed)
├── installer/
│   ├── deepvac.iss                # Inno Setup script
│   └── README.md                  # freeze + installer build steps
├── docs/
│   └── testing.md                  # test isolation, fixtures, golden-fixture rationale (read this)
├── tests/
│   ├── conftest.py                  # shared fixtures — data-dir isolation, Qt offscreen, etc.
│   ├── unit/                        # no filesystem I/O beyond tmp_path
│   ├── integration/                 # real sqlite/CSV, real model checkpoint where marked
│   ├── ui/                          # PySide6 widget/smoke tests, QT_QPA_PLATFORM=offscreen
│   └── fixtures/                    # sample run CSVs, golden simulation JSON
├── CONTRIBUTING.md                  # local dev-loop commands (start here)
└── README.md                        # user/ops-facing setup + feature notes
```

---

## 3. Getting started

```powershell
uv sync --extra dev          # creates .venv: runtime deps + pytest/ruff/mypy/pre-commit/pip-audit/pyinstaller
uv run pre-commit install    # wires the ruff git hook (.pre-commit-config.yaml)
uv run python main.py        # run the app
```

`uv sync` is the single source of truth for the environment — don't
hand-install packages into `.venv` with pip. `uv.lock` is committed; if you
add/change a dependency, do it via `pyproject.toml` + `uv lock`, not by
editing the lock file directly.

### First run without the licensing stack

The app normally gates the login screen behind a cloud-license activation
check against `hub` (see §6). For unrelated app dev where you don't need
that flow running:

```powershell
$env:DEEPVAC_SKIP_LICENSE_CHECK = "1"
uv run python main.py
```

### Talking to a real chamber locally

No physical hardware needed — `tcp/dummy_chamber_server.py` speaks the same
newline-delimited-JSON protocol a real chamber does:

```powershell
python tcp\dummy_chamber_server.py --port 5555
```

Then point Live Monitoring's chamber picker at `localhost:5555`.

### Smoke-testing startup

```powershell
uv run python main.py --smoke-test --no-splash
```

Exercises the real startup path (splash → backup → window construction)
end-to-end with a fake user, then exits with code 0/1. See
`app/app.py::_run_smoke_test`. Useful for catching "won't even start"
regressions in packaging or CI-shaped checks — **note there is currently no
`.github/` CI workflow in this repo**; these checks are run locally / via
`pre-commit` today, not automated on push.

---

## 4. Architecture

### 4.1 Main window & views (`app/main_window.py`, `app/views/`)

`DeepVacDesktop` (`main_window.py`) is a `QMainWindow` built by **multiple
inheritance from per-page mixins**, one per sidebar destination:

```python
class DeepVacDesktop(
    DashboardMixin, RunsMixin, SimulatorMixin, ReportsMixin,
    MonitoringMixin, ControllerMixin, OpcMixin,
    QMainWindow,
):
```

Each mixin in `app/views/` owns one page's UI-building methods and
handlers (e.g. `DashboardMixin._dashboard_view()`,
`MonitoringMixin._mon_on_sample()`), and reaches into `self` for shared
state the main window owns (`self.tcp`, `self.opc_server`,
`self.current_user`, `self.dark`, ...). There's no page-level class
hierarchy beyond this — a page's logic lives entirely in its mixin file,
named-prefixed to avoid collisions (`_mon_*` for monitoring, `_dash_*` for
dashboard, etc.). The **Analysis** page is the odd one out: instead of a
fixed page in the stack, each opened run becomes a tab in
`app/tab_system.py`'s `EditorArea`, running `app/run_tab.py`'s
`RunTabPage`.

The chamber TCP connection (`self.tcp`, a `ChamberConnection`) and the OPC
broadcast server (`self.opc_server`) are owned by the main window itself
(not a view mixin) because both **Live Monitoring and OPC Server pages
share the one live connection** — see `_on_chamber_connected` /
`_on_chamber_disconnected` in `main_window.py` for how that fan-out works.

Styling is one large inline stylesheet string built in
`apply_theme()` (`main_window.py`) — there's no external `.qss` file or
per-widget stylesheet; dark/light both live in that one method as two
color dicts.

### 4.2 Services (`app/services/`)

Business logic and persistence, deliberately kept **free of Qt widget
code** (though several use Qt signals/`QObject`/`QTcpSocket` where that's
the natural async primitive). Each owns its own SQLite database file under
`DATA_DIR` unless noted.

| Module | Owns |
|---|---|
| `data_service.py` | Run cache (sync from the training-pipeline workspace into `deepvac_runs.sqlite3`), report generation, run discovery (`workspace_root()`, `data_root()`) |
| `data_quality.py` | `validate_samples()` — pure function, no I/O — checks imported run data for missing columns, bad/out-of-order timestamps, duplicate samples, sampling gaps, non-numeric values, implausible readings, mid-run unit changes |
| `auth_service.py` | User accounts, PBKDF2 password hashing, remember-me session tokens — own DB, separate from run cache |
| `licensing_client.py` | Desktop half of the device-code activation flow against `hub`; owns this install's Ed25519 keypair and the cached signed license cert |
| `annotations_service.py` | Chart annotations + variable rules, keyed by run + creating user |
| `derived_variables_service.py` | User-defined computed channels — reusable formulas, **not** scoped to one run (unlike annotations) |
| `safe_eval.py` | Restricted AST-walking expression evaluator backing custom derived-variable formulas — deliberately not `eval()`/`exec()` |
| `chambers_service.py` | Saved chamber connection profiles (name/host/port) |
| `test_profiles_service.py` | Named multi-step temperature/pressure test schedules |
| `alarms_service.py` | Persistent alarm rules + alarm event history for Live Monitoring |
| `tcp_client.py` | `ChamberConnection` — live chamber TCP client (`QTcpSocket`), newline-delimited JSON |
| `opc_broadcast_server.py` | Real, spec-compliant OPC UA server (asyncua) republishing chamber samples |
| `backup_service.py` | Automated rotating backups of every `*.sqlite3` under `DATA_DIR`, via SQLite's online backup API |
| `settings_service.py` | Persisted UI state via `QSettings("DeepVac", "Insight")` — theme, window geometry, open tabs, per-run channel selections; shared across accounts, not per-user |
| `i18n_service.py` | Language selection + Qt translator (`.qm`) loading |
| `log_service.py` | App-wide logging + global exception hook |

**Convention:** a service module owning a database exposes both a
module-level path constant (e.g. `data_service.CACHE_DB`) and connection
functions that read it lazily — this is what makes test isolation via
monkeypatching those constants possible (see §7).

### 4.3 Model / simulation (`app/model/simulation.py`)

Two things live here: a `torch` GRU plant model (loaded from the bundled
`model.pt` checkpoint) and a **Python reimplementation of the CODESYS
`ChamberPID`/`Diff` blocks**. `simulate_candidate()` runs them in a
closed loop — the GRU predicts next temperature, the PID/Diff blocks
compute the next control signal from that, and the loop feeds back
in-process for the given duration.

> **This file is partly generated.** Everything above
> `GENERATED_END_MARKER` is auto-generated by `deepvac package-model
> --target insight` (from the training pipeline repo's `scripts/deepvac/mpc.py`,
> `scripts/deepvac/schemas.py`, `scripts/gru/gru_common.py`) — **do not
> hand-edit that section**, it will be silently overwritten next time
> someone runs that command. Everything below the marker is this app's own
> Simulator-view glue and is safe to edit normally.

This is also the highest-scrutiny file in the repo: 85% coverage floor
(vs. 30% project-wide) and the only thing the golden-fixture test
(`tests/integration/test_simulation_golden.py`) exists to protect — see
§7.

---

## 5. Data & storage layout

Resolved by `app/common.py::resolve_app_paths()`. Priority: explicit
override param → `DEEPVAC_DATA_DIR` env var → production default (source
tree `data/` in dev, `%LOCALAPPDATA%\DeepVac\data` when frozen/installed).

```text
data/
├── deepvac_runs.sqlite3          # synced run cache (data_service.py)
├── deepvac_users.sqlite3          # accounts (auth_service.py)
├── deepvac_annotations.sqlite3    # annotations + variable rules (annotations_service.py)
├── license/                        # this install's Ed25519 keypair + cached signed license
├── backups/<db-name>/              # rotating backups, 14 most recent per DB
└── reports/                        # generated .xlsx reports
```

`data/` is entirely regenerated/derived — nothing under it is source, and
it's gitignored in full (see `.gitignore`). **Never point a test at this
directory** — see §7's isolation rules.

Run *source* data (what gets synced into the cache) is discovered
separately, via `data_service.workspace_root()` / `data_root()` /
`runs_root()`, walking up from this repo looking for a sibling
`optimization/` or `gru/` folder (the training-pipeline workspace) —
overridable with `DEEPVAC_DATA_ROOT` / `DEEPVAC_WORKSPACE_ROOT`.

---

## 6. External integrations

### 6.1 Cloud licensing (`hub`)

Before the sign-in screen, the app requires a valid signed license
certificate from the sibling `hub` repo's control plane. No
username/password is asked for this step — instead a device-code
activation flow: `LicenseActivationWindow` shows a short code, opens
`hub`'s browser activation page, and polls until an org admin approves the
device there. Implementation: `app/services/licensing_client.py` +
`app/license_activation_window.py`; protocol details live in
`../hub/docs/sequences.md`.

To exercise this locally against `hub`'s docker-compose stack, see the
"Cloud licensing" section of `README.md` — it walks through seeding a demo
org/license and pointing this app at `http://localhost:8080/api/v1`.
`DEEPVAC_SKIP_LICENSE_CHECK=1` bypasses the gate entirely for unrelated
dev work.

### 6.2 Live chamber TCP (`app/services/tcp_client.py`)

Inbound wire protocol: newline-delimited JSON, one sample object per line,
same keys as a `run_samples.csv` row (`temp`, `temp_ref`, `kp`, `ki`,
`kd`, `temp_u`, `temp_u_p`, `temp_u_i`, `temp_u_d`, ...). Built on
`QTcpSocket`. An unexpected drop triggers automatic reconnect with
backoff; a manual disconnect does not.

### 6.3 OPC UA server (`app/services/opc_broadcast_server.py`)

A real, spec-compliant OPC UA server (built on `asyncua`, not a
simplified/custom protocol) that republishes each key of an incoming
chamber sample as a variable node under `Objects/ChamberVariables`. Only
startable once a chamber is connected via Live Monitoring — it broadcasts
what that connection receives. Only anonymous, unencrypted access is
enforced today; the Security/Auth UI fields are placeholders for a future
implementation, not currently wired to real access control. Disconnecting
the chamber connection stops the server automatically.

---

## 7. Testing

Full rationale lives in `docs/testing.md` — read it before touching test
infrastructure, especially the isolation section. Summary:

```powershell
uv run pytest tests/unit           # fast, no I/O beyond tmp_path
uv run pytest tests/integration    # sqlite/filesystem via tmp_path; some load the real model.pt checkpoint
uv run pytest tests/ui             # PySide6 widgets, QT_QPA_PLATFORM=offscreen
uv run pytest -m "not integration" # skip the slower/checkpoint-loading tests
```

### The one rule everything else supports

**No test process may ever read or write real user data**
(`%LOCALAPPDATA%\DeepVac\data` or the source-tree `data/`). Enforced by:

1. `resolve_app_paths()` resolving the writable data dir from an override
   → `DEEPVAC_DATA_DIR` → production default, in that order.
2. `tests/conftest.py`'s `deepvac_data_dir` fixture setting
   `DEEPVAC_DATA_DIR` to a fresh `tmp_path` **and** monkeypatching the
   already-resolved module-level DB path constants directly — necessary
   because `app.common` is almost certainly already imported by the time a
   test runs, so the env var alone can't retroactively change a constant
   already computed from it.

**If you add a new module-level path constant to a service** (another
`SOMETHING_DB = DATA_DIR / "..."`), add it to `deepvac_data_dir` too, or
that service silently escapes isolation.

### Notable fixtures (`tests/conftest.py`)

- `no_modal_dialogs` / `no_blocking_menus` (autouse) — patch `QMessageBox`
  statics and poll-close `QMenu.exec()` popups so blocking Qt calls in UI
  code don't hang the suite. **`QMenu.exec` cannot be monkeypatched on the
  class** (it's a Shiboken/C++-backed instance method) — the working fix is
  a `QTimer` polling `QApplication.activePopupWidget()`. If a new test
  hangs on some other blocking Qt dialog, this is the pattern to reuse.
- `deepvac_ui` — composes data-dir isolation + `QSettings` isolation + an
  empty `DEEPVAC_DATA_ROOT` + the shared `QApplication`; tears down by
  closing every top-level window and asserting no `QThread` is still
  running.
- `fake_user` — throwaway in-memory user dict for constructing widgets that
  need *a* `current_user` without going through real login (same approach
  `--smoke-test` uses).

### The golden simulation fixture

`tests/fixtures/simulation/golden_case_1.json` is a frozen snapshot of
`simulate_candidate()`'s output against the **real production checkpoint**
for one fixed scenario. `tests/integration/test_simulation_golden.py`
re-runs it and compares within tolerance — this is the only thing catching
a silent regression in `ChamberPID`/`Diff`/`GRUModel`/wiring.

**Never regenerate it just to make a failing test pass.** It's only
touched deliberately via
`uv run python scripts/update_simulation_golden.py --confirm`, and only
after you can explain *why* the trajectory changed (a deliberately
retrained checkpoint, an intentional math change) and have reviewed the
diff of old vs. new values — otherwise you've just made the "golden" file
agree with a bug.

### Coverage floors (`scripts/check_coverage.py`)

| Path | Floor |
|---|---|
| `app/model/simulation.py` | 85% |
| `app/services/data_service.py` | 65% |
| whole project | 30% |

```powershell
uv run pytest --cov=app --cov-branch --cov-report=term-missing --cov-report=json
uv run python scripts/check_coverage.py
```

Floors, not targets — don't chase 100% project-wide, and don't exclude a
genuinely hard branch just to hit a number.

---

## 8. Code quality tooling

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run pre-commit run --all-files   # run before opening a PR — same checks as the git hook
```

- **ruff**: line length 100, rule sets `E4 E7 E9 F I B UP SIM`, `E501`
  ignored. Config in `pyproject.toml`'s `[tool.ruff]`.
- **mypy**: intentionally scoped to five modules only —
  `app/model/simulation.py`, `app/services/data_service.py`,
  `app/services/auth_service.py`, `app/services/backup_service.py`,
  `app/common.py` (see `[tool.mypy]`). Growing that list is a deliberate
  choice, not a default — adding a module means fixing whatever mypy
  surfaces there. `pandas.*` has a scoped `ignore_missing_imports` override
  since pandas ships no inline types.
- **pre-commit** (`.pre-commit-config.yaml`): trailing-whitespace,
  end-of-file-fixer, check-yaml/toml, check-merge-conflict,
  detect-private-key, check-added-large-files (1024 KB cap — raised from
  default specifically to allow the tracked `model.pt`, not to blanket-permit
  large commits), then `ruff --fix` + `ruff format`.
- **No `.github/` CI workflows exist in this repo currently** — the checks
  above are enforced locally via the pre-commit hook and by convention, not
  by a CI gate on push/PR.

---

## 9. Building a distributable

Two steps — freeze with PyInstaller, then wrap with Inno Setup. Full
detail in `installer/README.md`.

```powershell
uv run pyinstaller deepvac.spec --clean      # → dist/DeepVac/DeepVac.exe
iscc installer\deepvac.iss                    # → installer/output/DeepVacInsight-Setup-<version>.exe
```

Installed layout: app binary wherever the installer put it; user data
under `%LOCALAPPDATA%\DeepVac\data`, created on first run — kept separate
from the install location.

**Known limitations:**
- Not code-signed — SmartScreen will warn on first run until/unless a real
  code-signing cert is configured.
- Version number is hand-set in two places that must stay in sync:
  `pyproject.toml` and `installer/deepvac.iss` (`MyAppVersion`).
- No auto-update mechanism — a new version means re-running both build
  steps and distributing the installer manually.

---

## 10. Conventions & gotchas

- **i18n**: source strings are English, wrapped in `self.tr(...)`
  throughout UI modules (or `QCoreApplication.translate(...)` in
  non-`QObject` contexts like `data_service.py`'s `_tr()` helper, or
  when `self.tr()`'s `%n` plural overload doesn't reliably resolve context
  in this PySide6 version — see `main_window.py::_backup_now` for a worked
  example). Compiled `.qm` files live under `resources/i18n/`, built from
  `.ts` via `pyside6-lupdate`/`pyside6-lrelease`; `resources/i18n/*.py` is
  excluded from ruff.
- **Derived variables use a restricted evaluator, not `eval()`**
  (`app/services/safe_eval.py`) — an AST walker permitting arithmetic and a
  fixed set of math functions only. Don't route user-authored formulas
  through real `eval`/`exec` anywhere else either.
- **`simulation.py`'s generated section is off-limits** — see §4.3. If a
  change is needed above `GENERATED_END_MARKER`, it belongs in the
  upstream training-pipeline repo's `deepvac package-model` sources, not
  here.
- **New service DB constants must be added to `deepvac_data_dir` in
  `tests/conftest.py`** or that service silently escapes test isolation
  (§7).
- **Don't monkeypatch `QMenu.exec` on the class** in tests — it doesn't
  work (Shiboken/C++-backed instance method); use the
  `activePopupWidget()`-polling pattern the `no_blocking_menus` fixture
  already implements.
- Settings (`settings_service.py`) are intentionally **account-agnostic** —
  theme/window state/open tabs are shared across whoever is signed in on a
  given machine, matching how runs/annotations/variable rules are also
  treated as shared rather than per-user state.

---

## 11. Glossary

| Term | Definition |
|---|---|
| Run | One complete controller execution, start to steady-state; belongs to a run group or carries a chamber/test-profile tag if it came from Live Monitoring |
| GRU | Gated Recurrent Unit — the neural net architecture behind the plant model in `simulation.py` |
| PID / Diff | CODESYS-style controller blocks reimplemented in Python for the simulator (`ChamberPID`, `Diff`) |
| Chamber | A saved connection profile (name/host/port) for a physical or simulated thermal chamber |
| Test profile | A named, ordered schedule of temperature/pressure setpoints, run from the Controller page |
| Derived variable | A reusable formula computing a new plotted channel from existing ones, evaluated via `safe_eval.py` |
| Tail MAE | Mean Absolute Error during steady-state — the primary run-quality metric |
| Golden fixture | The frozen `simulate_candidate()` snapshot protecting against silent simulation regressions (§7) |
| `hub` | Sibling repo implementing the cloud licensing control plane this app activates against |
