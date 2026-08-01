# DeepVac Inisght

A PySide6 desktop dashboard application.

## Project layout

```text
insight/
├── main.py              # entry point — run this
├── requirements.txt
├── app/                 # application package
│   ├── app.py           # QApplication bootstrap, license gate, login flow, splash screen
│   ├── main_window.py   # DeepVacDesktop main window
│   ├── license_activation_window.py # cloud-license device-code activation screen
│   ├── login_window.py  # sign in / create account screen
│   ├── profile_dialog.py# change name, email, password
│   ├── common.py        # shared constants, path config, icon helpers
│   ├── title_bar.py, tab_system.py, chart_widget.py, run_tab.py
│   ├── services/
│   │   ├── data_service.py         # run cache (sqlite), report generation
│   │   ├── auth_service.py         # user accounts (sqlite), password hashing, sessions
│   │   ├── licensing_client.py     # hub cloud-licensing client (device activation/renewal)
│   │   ├── annotations_service.py  # chart annotations + variable rules (sqlite), per run/user
│   │   ├── backup_service.py       # automated sqlite backups + restore
│   │   ├── tcp_client.py           # live chamber TCP connection (Live Monitoring)
│   │   └── opc_broadcast_server.py # real OPC UA server publishing chamber samples (OPC Server page)
│   ├── views/            # one module per page (dashboard, runs, reports, simulator, monitoring, opc)
│   └── model/             # bundled GRU checkpoint + closed-loop simulator
│       ├── model.pt
│       └── simulation.py
├── resources/             # icons, logo, window icon
└── data/                  # local run cache + generated reports (gitignored)
    ├── deepvac_runs.sqlite3
    ├── deepvac_users.sqlite3
    ├── deepvac_annotations.sqlite3
    ├── license/             # device keypair + cached signed license certificate
    ├── backups/            # rotating daily backups of the databases above
    └── reports/
```

## Install

Dependencies are declared in `pyproject.toml`. Managed
with [uv](https://docs.astral.sh/uv/):

```powershell
uv sync --extra dev
```

This creates a `.venv` with the app's runtime dependencies plus the dev
toolchain (pytest, ruff, mypy, pre-commit, pip-audit, pyinstaller). See
[docs/testing.md](docs/testing.md) and [CONTRIBUTING.md](CONTRIBUTING.md)
for the full local dev-loop.

## Run

```powershell
uv run python main.py
```

```powershell
$env:DEEPVAC_SKIP_LICENSE_CHECK = "1"
uv run python main.py
```

## Building a distributable

To hand this to someone without a Python/conda setup, freeze it with
PyInstaller and wrap that into a Windows installer with Inno Setup — see
[installer/README.md](installer/README.md) for the two-step process
(`pyinstaller deepvac.spec` then `iscc installer\deepvac.iss`). The
installed app keeps its own data in `%LOCALAPPDATA%\DeepVac\data`,
separate from wherever it's installed.

## Local run database

On startup, the app syncs the current run history into:

```text
data\deepvac_runs.sqlite3
```

Generated reports are written to `data\reports\`.

## Cloud licensing (device activation)

Before the sign-in screen, the app requires this installation to hold a
valid signed license certificate from `hub` — the sibling repo implementing
the vendor cloud licensing control plane (`../hub`). It never asks for a
username/password itself for this: on first run (or whenever the cached
license stops verifying) it shows an **Activate this installation** window
with a short code, opens `hub`'s browser activation page, and polls until
an organization admin approves the device there. See
`app/services/licensing_client.py` and `app/license_activation_window.py`,
and `../hub/docs/sequences.md` for the full protocol.

To verify this locally against `hub`'s Docker Compose stack:

```powershell
cd ..\hub
docker compose up -d
docker compose run --rm tools alembic upgrade head
docker compose run --rm tools python scripts/generate_signing_key.py --key-id dev-key-2026 --out-dir ./secrets
docker compose run --rm tools python scripts/seed_development.py --key-id dev-key-2026 --public-key-file secrets/dev-key-2026.public.b64
```

That seeds a demo organization with an active `deepvac-insight` professional
license (login `demo@example.com` / `DemoPass123!` at `http://localhost:8080/login`).
Then just run `python main.py` here — it talks to `http://localhost:8080/api/v1`
by default (`DEEPVAC_LICENSING_API_URL` to override). Approve the device
using the demo login when the activation window opens its browser tab, and
the app proceeds to the local sign-in screen once activation completes.
The device keypair and cached license live under `data\license\`.

Set `DEEPVAC_SKIP_LICENSE_CHECK=1` to bypass this gate entirely for
unrelated dev work when no `hub` instance is running.

## Accounts

The app always opens to a sign-in screen, backed by `data\deepvac_users.sqlite3`
(passwords are salted + PBKDF2-hashed). "Remember me" persists
a session token via `QSettings` (`DeepVac`/`Insight`) so the app skips the
login screen on the next launch; logging out clears it. "Profile" lets the signed-in user change their name,
email, and password.

## Annotations & variable rules

Chart annotations (drag-to-label a time range) and variable rules (min/max
bands per channel), created from the Analysis tab, are persisted to
`data\deepvac_annotations.sqlite3` — keyed by the run and the user who
created them. They survive closing the
tab, closing the app, and are visible to any user who opens that run, with
the creator's name shown alongside each one.

## Backups

Every `*.sqlite3` database under `data\` is backed up automatically — once
at startup, and rechecked every 6 hours for sessions left open across a day
boundary — into `data\backups\<name>\`, using SQLite's own online backup API
(safe even while the database is in use). Each database keeps its 14 most
recent backups. Use the gear icon → **Back Up Now** to force an extra backup,
or **Open Backups Folder** to browse them. To restore, call
`backup_service.restore_backup(db_name, backup_path)` — it safety-copies the
current (possibly damaged) database first, so a bad restore can be undone.

## Live Monitoring & OPC Server

Live Monitoring connects to a real chamber over TCP (`app/services/tcp_client.py`,
built on `QTcpSocket`). Wire protocol: newline-delimited JSON, one object per
line, with the same keys as a `run_samples.csv` row (`temp`, `temp_ref`,
`kp`, `ki`, `kd`, `temp_u`, `temp_u_p`, `temp_u_i`, `temp_u_d`).

The OPC Server page can only be started once that chamber connection is
established — it runs a real, spec-compliant OPC UA server (built on
`asyncua`, see `app/services/opc_broadcast_server.py`) that publishes each
key of an incoming sample as a variable node under
`Objects/ChamberVariables`, updating them in place as new samples arrive.
Any standard OPC UA client (UAExpert, an `asyncua`/`python-opcua` client,
etc.) can connect to `opc.tcp://<host>:<port>/deepvac/insight/`, browse to
that node, and subscribe. The configured update rate caps how often the
latest sample is pushed to the nodes. Only anonymous, unencrypted access
is enforced today — the Security/Auth fields on the page are kept for a
future implementation, since real certificate- or credential-backed access
control isn't wired up yet. Disconnecting the chamber connection
automatically stops the server.

To test this without real hardware, run the dummy chamber server
(`tcp/`) and point Live Monitoring at it:

```powershell
python tcp\dummy_chamber_server.py --port 5555
```
