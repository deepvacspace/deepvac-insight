# Deepvac Inisght

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

## Cloud authentication (optional)

The app runs fully offline and never requires authentication to start.
Authenticating is optional and only needed for optional cloud backup of
non-sensitive data (shared test profiles, chamber registry, alarm rules,
organization directory) through `hub` — the sibling repo implementing the
vendor cloud licensing control plane (`../hub`). To authenticate, open
**Profile** and click **Authenticate**: it shows an **Activate this
installation** window with a short code, opens `hub`'s browser activation
page, and polls until an organization admin approves the device there.
The signing keys used to verify the resulting license are fetched once and
stored locally, so an authenticated installation keeps working offline. See
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
by default (`DEEPVAC_LICENSING_API_URL` to override). Sign in locally,
then click **Authenticate** in Profile and approve the device using the
demo login when the activation window opens its browser tab. The device
keypair, cached license, and trusted signing keys live under `data\license\`.

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

## Agent API

An MCP server (`app/api/`) that the DeepVac crew of AI agents (`../agents`,
n8n) calls. Two endpoints, one bearer token:

| Endpoint | For | Tools |
|---|---|---|
| `http://127.0.0.1:8765/analyst/mcp` | Analyst | list / rank runs, run summary, stages, KPIs, statistics, anomalies, compare runs, channels, events, CSV export, PID simulation on the GRU twin. Read-only. |
| `http://127.0.0.1:8765/worker/mcp` | Worker | chambers, live status, PLC settings, programs; requests to set a temperature, run a program or stop |

**Only a person approves.** The worker's `request_*` tools never change a
chamber: they create a request in `data\deepvac_agent_requests.sqlite3`. The
**Agent Requests** dialog (gear menu; it also opens by itself when a request
arrives) shows it; a signed-in user approves or rejects it, and only then
does the API send it to the PLC. Undecided requests expire after 30 minutes.

Turn it on with gear menu → **Agent API** (remembered), or run it without
the desktop app:

```powershell
uv run python -m app.api                # serves the API and sends approved requests
uv run python -m app.api --show-token   # prints the bearer token (also: gear menu -> Copy Agent API Token)
```

The token is generated once into `data\agent_api_token`, or set
`DEEPVAC_AGENT_API_TOKEN`. The server listens on 127.0.0.1 only; n8n in
Docker Desktop reaches it as `host.docker.internal`.

### Chat with the crew

The round button at the bottom right of the window opens a chat with the
crew. Messages go to the crew workflow's chat webhook in n8n (default
`http://localhost:5678/webhook/6c1f0e2a-5d3b-4c8e-9f10-deepvaccrew01/chat`,
change it with gear menu → **Agent Chat URL…**). The workflow must be
published in n8n. **New chat** starts a conversation the crew doesn't
remember.

### Analysis Live tab

The first Analysis tab, **● Live**, follows the experiment running on a PLC
chamber now: the job since it started (the last 30 minutes when the chamber
is idle), its stages shaded on the chart, the chamber's state, and KPIs and
anomalies recomputed every 10 s. With **Save finished jobs as runs** (on by
default) every job that ends is saved to the run database, so experiments
started by the agents can be analysed like any other run. **Save as Run**
saves the current job now. Closed by mistake? Gear menu → **Open Live Tab**.
It shares its PLC connections with the Agent API.

### Chamber control: the PLC gate

The worker talks to the chamber PLC directly over its binary protocol
(`app/services/plc_gate.py`, ported from control2-client's `GateTcp`):
chambers with protocol `plc` in the chambers registry. Chambers with
protocol `json` are Live Monitoring feeds and can't be controlled through
the API. Without hardware, run the fake PLC and register it as a `plc`
chamber:

```powershell
uv run python scripts/fake_plc.py --port 4321
```

The job encoding (manual setpoint, programs) mirrors control2-client and has
been checked against the fake PLC only: try it on a test chamber before
relying on it.

### Analysis Core

`app/core/analysis/` is a port of control2-client's Analysis Core (stages,
KPIs, steady states, saturation, PID shares, anomalies, comparisons). It
reproduces the C++ results exactly; `tests/unit/core/test_analysis_port.py`
checks it against C++ output for real runs, and
`scripts/check_analysis_port.py` compares it with every summary a
control2-client run database stored.

### Importing control2-client's data

```powershell
uv run python scripts/import_control_data.py --control ..\..\control2-client --dry-run
uv run python scripts/import_control_data.py --control ..\..\control2-client
```

Copies its runs, test programs, the PLC (`settings.json` `tcp_server`) as a
`plc` chamber and the chamber configuration (`data\control_settings.json`:
states, program parameters, limits, alarm labels). It backs up each database
first (`data\backups\`), never deletes, and skips what is already here. Runs
that exist both as a synced folder run and as an imported run are listed
once by the API (the imported copy, which has units and events).
