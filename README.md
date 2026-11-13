# Deepvac Insight

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
│   │   ├── tcp_client.py           # live chamber TCP connection (sample feeds, Settings → Chamber Connections)
│   │   └── opc_broadcast_server.py # real OPC UA server publishing chamber samples (OPC Server page)
│   ├── views/            # one module per page (runs drawer, reports, controller, opc, campaigns, ai_agents)
│   └── model/             # bundled LSTM checkpoint + closed-loop simulator
│       ├── model.pt
│       └── simulation.py
├── agents/                # the AI agents: n8n Docker Compose file and workflows
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

Each run's samples are stored as JSON text (`runs.samples_json`), about
340 bytes per sample. The run database holds experiments; everything a
chamber sends, all day, goes to the full-day recordings below.

## Full-day recordings

Every sample of every chamber, experiment or not, one Apache Parquet file
per chamber and day (`app/services/day_recorder.py`):

```text
data\recordings\<chamber>\YYYY-MM-DD.parquet
```

Always on, like Deepvac Control (gear menu → **Open Recordings Folder**).
PLC chambers are read from their connections, sample-feed chambers from
their sessions while connected. The day is kept in memory
and its file rewritten every minute and on closing (Parquet files can't be
appended to); a restart continues the day from its file.

The format, "deepvac.recording/1", is the one Deepvac Control writes since
it replaced its daily `.dat` files (control2-client
`src/storage/recordingfile.h`), so each application reads the other's files:

| Column / key    |                                                                                                                              |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `timestamp`   | TIMESTAMP(MICROS, UTC), not null                                                                                             |
| one per channel | DOUBLE, not null; a gap is NaN                                                                                               |
| footer          | `deepvac.format`, `deepvac.day`, `deepvac.chamber`, `deepvac.channels` (JSON: name, label, unit), `deepvac.source` |

Written with GZIP (level 6), PLAIN encoding (no dictionary) and data pages
v1: the subset Deepvac Control's own reader takes. pandas, pyarrow and
DuckDB read the files directly, e.g.
`duckdb.sql("SELECT avg(temp) FROM 'data/recordings/*/2026-10-24.parquet'")`.
The analyst reads them with `list_recordings` and `get_recording`.

`scripts/estimate_storage.py` measures a folder of Deepvac Control `.dat`
days in several storage layouts and projects a year of continuous 1 Hz
recording (34 days, 32 or 44 channels):

| Layout                                     | Bytes / sample | GB / year (worst day) |
| ------------------------------------------ | -------------- | --------------------- |
| `.dat` (8-byte doubles)                  | 264–360       | 8.3 (11.4)            |
| JSON rows, as the run database stores them | 641            | 20.2 (24.8)           |
| SQLite, one REAL column per channel        | 147            | 4.6 (5.7)             |
| JSON + zlib                                | 16             | 0.5 (1.7)             |
| zlib float64 column per channel            | 6              | 0.19 (0.59)           |
| **Parquet, as recorded now**         | **9.5**  | **0.30 (0.68)** |

## Test campaigns

A thermal qualification or acceptance test as a campaign, from the customer's requirement to a
signed as-run report (sidebar → **Test Campaigns**; the Professor drives the same flow from the
chat):

- **Plan** (`app/services/campaign_service.py`): the specification comes from the standards pack,
  `data/standards_pack.json` (written with illustrative ECSS-style values on first use: margin and
  cycles per level, dwell, ramp limit, stabilisation band and timeout, excursion time, vent window,
  ambient, `vacuum` "auto", "manual" or "none", the start pressure `vacuum_start_hpa`, 1×10⁻⁵ hPa,
  and `pump_timeout_min`). Plateaus are the predicted temperatures ± the margin and
  must be inside the chamber's setpoint range. Every value names its source; cycles, dwell and
  ramp can be changed with a reason. *Rehearsal* shortens the dwells.
- **Pump-down**: with `vacuum` "auto" and a chamber that reports a pressure (`pres`, in the unit
  of its state list, Torr for Deepvac), the supervisor holds the chamber at ambient while it pumps
  down and starts cycling by itself once the gauge reads the start pressure; past
  `pump_timeout_min` a hold point asks to wait, start anyway (a deviation) or abort. Without a
  gauge reading, and with "manual", the operator confirms the pump-down at a hold point. The PLC
  simulator's pressure is synthetic.
- **The PLC's clock**: ramps, dwells and timeouts are timed with the PLC's own job runtime
  (`t_total`, `plc_gate.PlcClock`), not this PC's clock. A real PLC runs at this PC's pace; the
  PLC simulator run with `-speed 300 -period 50ms` runs a 40 h qualification campaign in about
  10 min, and the supervisor, the Campaigns page and the Observer follow it.
- **Hold points**: approval, pump-down (no gauge: the operator confirms), a dwell temperature out
  of the band for longer than the excursion time (the dwell clock pauses), a plateau that doesn't
  stabilise in time, an interrupted job (alarm, stopped, another job's setpoint), venting, and an
  **oscillation** on a plateau (the Observer's detector: 6 setpoint crossings within 10 min, even
  inside the band). Only the signed-in user decides, on the page; agents can `recommend` an
  option, shown with a ★.
- **Oscillation and the operator's PID table**: when a campaign starts, the supervisor records the
  temperature PID table (read from the PLC, else Deepvac Control's settings). At an oscillation
  hold point the detail shows the band's gains in use against that table; **Revert to the
  operator's PID table** writes it back to the PLC (a settings map). The gateway lets only the
  person who decided write it, and only the recorded values; the signed log has it under their
  name. With the PLC simulator, `fault` in its console starts the oscillation
  (`deepvac/simulator`).
- **The ★ at once** (`app/services/hold_advisor.py`): when the supervisor raises a hold point, the
  Analyst recommends straight away from rules (gains against the table, how far and how long the
  temperature is out, the chamber's status). With gear menu → **Hold Point Advisor → Local
  Model**, the facts also go to llama-server on port 8089 (Qwen3-4B, offline); its answer replaces
  the rules' one when it names an offered option, unless the Professor recommended meanwhile.
  Start the model with:
  ```powershell
  $llm = "C:\Users\mike_\Desktop\control2-client\llm"
  & "$llm\llama-server.exe" --model "$llm\models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf" `
    --host 127.0.0.1 --port 8089 --ctx-size 8192 --jinja --no-webui
  ```
- **Run supervisor** (`app/api/campaign_supervisor.py`), started with the Agent API: each ramp is
  one PLC setpoint job (ramp at the specification's rate, then a hold long enough for the dwell
  plus a 2 h margin, renewed while the campaign runs, so the PLC ends it on its own if Insight
  stops). Its gateway refuses commands outside the rules (only the supervisor commands; plateaus
  or ambient only; inside the chamber's range; ramp ≤ the specification; cycling after the
  pump-down; venting approved and inside the vent window, checked on the live temperature) and
  logs the refusal. **Stop now** on the page stops the chamber's job at once.
- **Signed log**: every step, decision and refusal, each entry chained to the previous one with
  SHA-256 (`campaign_log`); **Verify log** rechecks the chain.
- **Report** (`app/services/campaign_report.py`): PDF and markdown in the reports folder, with the
  specification and sources, plateau results, deviations, decisions with recommendations, blocked
  commands, the temperature chart, and the chain's last hash signed with the installation's
  Ed25519 device key. The summary is the Professor's or a template's (it says which); **Sign off**
  writes it again with the operator's name.

Campaigns live in `data/deepvac_campaigns.sqlite3`. One campaign at a time per chamber.

## The Observer: events on the chambers

While the Agent API runs, the observer monitor (`app/api/observer_monitor.py`) watches every PLC
chamber and records what needs attention as events (`app/services/observer_service.py`,
`data/deepvac_observer.sqlite3`), each once:

| Event                 | Severity                     | When                                                                                                                                       |
| --------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| alarm / alarm cleared | critical / info              | the PLC reports an alarm; it clears                                                                                                        |
| job ended             | info (warning in alarm)      | a job finishes, is stopped or ends in alarm; with the agent request that started it                                                        |
| drift                 | warning                      | the temperature more than `drift_c` (3 °C) from a setpoint steady for `settled_min` (10), for `drift_min` (5 min); not during ramps |
| anomaly               | warning (stuck sensor: info) | oscillation, spikes or a flat temperature in the last `anomaly_window_min` (30) of a running job                                         |
| offline / online      | warning / info               | a chamber that was online not heard from for `offline_s` (60 s); back                                                                    |
| hold                  | warning                      | a campaign hold point waiting (the approval after `waiting_min`)                                                                         |
| request waiting       | warning                      | an agent request pending for `waiting_min` (10 min)                                                                                      |

Gear menu → **Observer Events** lists them; people acknowledge them there, and the Observer's
summary and message appear once it has read one. The same dialog holds the settings
(`data/observer.json`): watch on/off, **send events to the Observer workflow** (n8n webhook, off by
default: the Observer isn't connected yet), the threshold, the drift and waiting times, and *Send
Test Event*. Sent events carry the API token as `Authorization: Bearer`; a failed send is retried
five times with a growing delay.

The Observer agent (`agents/workflows/deepvac-observer.json`) uses `/observer/mcp`:
`get_event` (facts, the chamber now, its last 30 min, the campaign or request involved, earlier
events), `list_events`, `get_live_status`, `report_event` (summary, notify, message; read back at
`/observer/events?ids=`), and `propose_stop` / `propose_setpoint`, which create ordinary agent
requests (refused on a chamber running a campaign).

## The Brain: wiki and experiments database

The AI agents' knowledge, for the Professor (`/brain/mcp`):

- **The wiki** (`wiki/`, markdown, versioned with the code; `app/services/wiki_service.py`):
  what the data alone doesn't say. How the chamber works (refrigeration cascade, heater,
  PLC), PID control, programs and the PLC status, alarms, limits and safety rules, four
  procedures (manual setpoint, test profile, approving requests, alarm / finish), the KPI,
  stage and anomaly definitions, where the data is, the digital twin, the agents, a glossary.
  `wiki/SCHEMA.md` says how pages are written (frontmatter: title, type, tags, sources,
  updated; links as `[[page]]`); `index.md` is rebuilt automatically, `log.md` records changes.
  Three pages are **generated** from the data and never edited: `chamber-configuration`
  (from `data/control_settings.json`), `experiment-history` and `pid-tuning-lessons` (step
  responses by target and direction, with the gains that did best).
- **The experiments database** (`data/deepvac_brain.sqlite3`, `app/services/brain_db.py`):
  the run history as plain SQL tables (`experiments` with KPIs and gains, `stages`,
  `anomalies`, `events`, `test_profiles`, `recordings`), every column described for the
  agent (`sql_schema`). Queries are read-only: one SELECT, read-only connection, an authorizer
  that refuses writes, at most 200 rows, 5 s.
- **Agents propose, people approve.** `wiki_propose` stores a new page or a new version in
  `data/deepvac_wiki_proposals.sqlite3`; gear menu → **Wiki Proposals** shows it as a diff
  against the current page to approve or reject (a page that changed since is refused).
  **Open Wiki Folder** opens the pages.

Rebuild after adding runs (about 2 minutes for 1,100 runs; also regenerates the generated
pages, the index and runs the wiki's lint):

```powershell
uv run python scripts/build_brain.py            # database + generated pages
uv run python scripts/build_brain.py --pages-only
```

Deepvac Control's event log (`data/event/actions.dat`) is copied into
`data/deepvac_control_log.sqlite3` by `scripts/import_control_data.py` and becomes the
`events` rows with source `control log`.

## Analysis: opening runs and exporting

There is no Runs page: as in Deepvac Control's Analysis page, the **+** after
the tabs opens the run list in a drawer over the tabs (search, upload, report
download per run). A click opens the run in the pane whose **+** was used;
ticking runs compares them with the active tab; right-click → **Open to the
Side**, **Rename…**, **Download Report**.

**Export** (in each run tab and in the Live tab) opens Deepvac Control's
export panel (`app/export_panel.py`, writing with `app/services/run_export.py`,
the same files the analyst agent exports). What:

- **Chart image**: PNG, BMP or JPEG.
- **Data**: CSV or TXT (tab separated): Time, Elapsed and every channel.
- **Experiment**: report HTML or PDF (the chart as shown, or the comparison,
  first), bundle JSON (manifest, analysis, samples, annotations and rules).
- **Comparison CSV** (while comparing): this run and the runs compared with
  it, one row each with the compared channel's min, max, mean and final value.

Of which range: the **visible view**, the **whole experiment shown**, its
**day** or a **custom** range; the last two are read from the chamber's
full-day recordings (**Preview** shows the samples first). The file browser
opens in `data\reports\`, named after the run.

## AI Agents page and online / offline AI

The **AI Agents** button (last in the sidebar) shows a *real* test campaign on a
chamber (the PLC simulator in a demo), in native Qt widgets (`app/views/ai_agents.py`);
opening it starts the demo. Everything comes from Insight through
`app/views/demo_bridge.py`. The five stages:

1. **Requirements**: the form alone: the test article, thermal cycling (level,
   margin, cycles) or a single ramp (direction, margin), notes.
2. **Plan**: the planner's steps and specification, with their sources.
3. **Approval**: a decision point in a dialog with the Analyst's ★, decided by the
   signed-in user; declining returns to the plan.
4. **Run**: the shroud (the PLC's control sensor), its setpoint, the platen and the
   test article's four thermocouples on the left axis, pressure (log, Torr) with the
   pressure rule's hold limit on the right, on the PLC's clock; the steps, plateau
   results, deviations, decision points, and the safety tests (**Mistype the PID
   gains**, a sensor fault on a thermocouple, **Release a gas burst**, **Simulate a
   rogue command**: the gateway refuses and logs each).
5. **Report**: the plateau results, deviations and blocked commands; the as-run
   report (PDF / markdown) written by the Professor or the local model, then signed off.

The agents' feed and the signed log are the campaign's log; **Verify log** and
**Simulate tampering** (undone with the same button). The run supervisor runs with
the Agent API.

**The test article** (the standards pack, `data/standards_pack.json`): the demo's
articles are a 6U CubeSat, a Reaction Wheel Assembly, an Optical Camera Payload and a
Power Control and Distribution Unit, each with its predicted temperature range. Every
article has the same four test-article temperatures (`temp1` … `temp4`, Deepvac Control's
Temperature 1 to 4); the pack holds the demo simulator's thermal model of each, sent to the
simulator when the campaign starts. What the run supervisor does with them
(`app/api/campaign_supervisor.py`), all in the specification the operator approves:

- **Stabilisation**: a dwell starts when every valid test-article temperature is within
  the band of the plateau and changes less than 3 °C/h over 15 min, not when the shroud
  reaches its setpoint (without those temperatures, the chamber temperature in the band).
- **Pressure rule**: above 7.5×10⁻⁵ Torr a heating ramp is held where it is (a dwell's
  clock paused) until the gauge reads 3.8×10⁻⁵ Torr; held over 60 min, a decision point.
- **Sensor validity**: a test-article temperature that reads open (outside −200 … 400 °C),
  frozen (no change for 5 min) or spiking (4 jumps over 10 °C in 10 min) is marked
  invalid and left out of the charts and results. It is taken out of the stabilisation
  criterion only by a person, at a decision point.
- **Rogue commands** the gateway refuses: forcing an output, turning off or editing an
  alarm, an agent's own job, venting now, a ramp of 10 °C/min, cycling without vacuum,
  an agent changing the PID gains, +150 °C, the Planner approving its own plan.

The demo adds the simulator's `temp_platen` state to Insight's chamber configuration
(`demo_launcher.ensure_demo_states`).

**Online or offline AI** (gear menu → **AI Mode**, or the page's AI switch;
`app/services/ai_mode.py`, `data/ai_mode.json`):

|                               | Online                                                                              | Offline                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Chat                          | the Deepvac Agents on n8n (Claude)                                                  | the local model answers from what Insight knows (chambers, campaign, hold point, log, runs); it can't act |
| Hold point ★                 | the Analyst's rules at once; the Professor, told in the chat, can recommend instead | the local model (rules when it can't)                                                                     |
| Report summary                | the Professor, asked in the chat                                                    | the local model (a template when it can't)                                                                |
| Campaign, gateway, signed log | the same                                                                            | the same                                                                                                  |

Offline, Insight starts llama-server with Qwen3-4B on port 8089 when nothing answers
there (`app/services/local_llm.py`), from `data/ai_mode.json` `llm_dir`,
`DEEPVAC_LLM_DIR`, an `llm` folder next to the app, or Deepvac Control's
`control2-client\llm`, and stops it on exit.

**Start the demo** (the page's bar, offline or online; `app/services/demo_launcher.py`) does the
rest in one step:

1. starts the PLC simulator (`deepvac/simulator/chambersim.exe`, `-speed 300 -period 50ms`, with
   `data/control_settings.json`) unless one already answers on port 4321;
2. registers its chamber, "Simulator (demo)" (127.0.0.1, protocol `plc`), if it isn't there;
3. turns the Agent API on (it runs the campaign supervisor);
4. sets the AI mode: offline starts the local model, online checks that n8n answers;
5. sets the simulator to 300 times once connected;
6. keeps the PC awake, screen on, while the demo runs.

**Stop the demo** stops the simulator it started and lets the PC sleep; closing Insight does
too. **Reset** ends the campaign on the chamber and puts the simulator back as started (no job
or fault, at ambient, vented: `sim_reset`), for the next run. Paths and ports can be set in
`data/demo.json` (`simulator_exe`, `port`, `speed`, `period`, `n8n_url`).

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

## Chamber connections & OPC Server

Chambers are added and edited on the **Controller** page (**Chamber
Settings…**) and connected there with **Connect**; the Controller also holds
the manual setpoint, test profiles and each chamber's alarm rules (for a PLC
chamber, the PLC control described under *Chamber control* below). A
connected chamber's live data, stages, KPIs and anomalies are followed in
Analysis' **● Live** tab, which also saves the recording as a run
(Live Monitoring, the old per-chamber page, was removed). Disconnecting a
chamber with unsaved samples offers to save them first.

A chamber's connection is one of:

- **Sample feed (JSON)**: TCP (`app/services/tcp_client.py`, built on
  `QTcpSocket`), newline-delimited JSON, one object per line, with the same
  keys as a `run_samples.csv` row (`temp`, `temp_ref`, `kp`, `ki`, `kd`,
  `temp_u`, `temp_u_p`, `temp_u_i`, `temp_u_d`), e.g. Deepvac Control's
  insight server. Connected from the Controller.
- **Chamber PLC**: the PLC itself over the PLC gate (see *Agent API* below).
  Connects by itself when the Live tab or the Agent API needs it.

The OPC Server page (hidden from the sidebar for now) can only be started once a chamber connection is
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
(`tcp/`) and connect to it from Settings → Chamber Connections:

```powershell
python tcp\dummy_chamber_server.py --port 5555
```

## Agent API

An MCP server (`app/api/`) that the Deepvac Agents ([agents/](agents/README.md),
n8n) call; for how to use them from the interface, see
[docs/agents-user-guide.md](docs/agents-user-guide.md). Five endpoints,
one bearer token:

| Endpoint                                | For       | Tools                                                                                                                                                                                                                      |
| --------------------------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `http://127.0.0.1:8765/analyst/mcp`   | Analyst   | list / rank runs, run summary, stages, KPIs, statistics, anomalies, compare runs, channels, events, PID simulation on the LSTM twin, full-day recordings (list_recordings, get_recording), and exports (below). Read-only. |
| `http://127.0.0.1:8765/worker/mcp`    | Worker    | chambers, live status, PLC settings, programs; requests to set a temperature, run a program or stop                                                                                                                        |
| `http://127.0.0.1:8765/brain/mcp`     | Professor | the Brain (below): wiki index, search, read and proposals; the experiments database's schema and read-only SQL                                                                                                             |
| `http://127.0.0.1:8765/professor/mcp` | Professor | test campaigns (below): standards pack, draft, submit, follow, recommend at hold points, report                                                                                                                            |
| `http://127.0.0.1:8765/observer/mcp`  | Observer  | events on the chambers (below): read one with its context, report on it, propose a stop or setpoint                                                                                                                        |

The analyst's exports copy Deepvac Control's Export menu
(`app/services/run_export.py`) and write into the reports folder
(`data\reports`):

| Tool                  | Writes                                                                                                                                                                                                                         |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `export_chart`      | Chart image, PNG / BMP / JPEG, 1600 × 700. The temperature chart (stages shaded, anomalies marked), the control chart (output and P / I / D terms), chosen channels or a time range, or several runs overlaid on one channel. |
| `export_report`     | Experiment report, PDF (A4 landscape) or HTML (charts embedded), with the sections of Deepvac Control's report; optionally a comparison with other runs.                                                                       |
| `export_data`       | Samples, CSV or TXT (tab separated): Time, Elapsed and each channel; whole run or a time range.                                                                                                                                |
| `export_comparison` | Comparison CSV: one row per run with a channel's min, max, mean and final value.                                                                                                                                               |
| `export_bundle`     | Experiment bundle (JSON): manifest, analysis, samples, annotations and rules.                                                                                                                                                  |

Each returns the file's path and a `file://` link. When an answer links to a
chart image, the chat shows it as a thumbnail; a click opens the file. Charts
and PDFs are painted with Qt, so `python -m app.api` makes an offscreen
`QGuiApplication` for them.

**Only a person approves.** The worker's `request_*` tools never change a
chamber: they create a request in `data\deepvac_agent_requests.sqlite3`. The
**Agent Requests** dialog (gear menu; it also opens by itself when a request
arrives) shows it; a signed-in user approves or rejects it, and only then
does the API send it to the PLC. Undecided requests expire after 30 minutes.

The loop closes in two steps:

- **Within the agent's turn**, the worker calls `wait_for_request`, which
  waits up to 3 minutes for the operator's decision and returns it.
- **After the turn**, the executor follows the job an executed setpoint or
  program started, until it ends: `job_state` goes `starting` → `running`
  → `finished` | `stopped` | `alarm` | `replaced` (`unknown` if the PLC
  never reports it running). The chat follows the requests its agent made
  (`GET /requests?ids=`, same token). When one is decided, sent or its job
  ends, a notice appears in the chat. If the agent hasn't already seen that
  through its own tools, the chat also sends it an update in the same
  conversation ("[Update from Deepvac Insight…]"), and the Professor tells
  you what it means.

Turn it on with gear menu → **Agent API** (remembered), or run it without
the desktop app:

```powershell
uv run python -m app.api                # serves the API and sends approved requests
uv run python -m app.api --show-token   # prints the bearer token (also: gear menu -> Copy Agent API Token)
```

The token is generated once into `data\agent_api_token`, or set
`DEEPVAC_AGENT_API_TOKEN`. The server listens on 127.0.0.1 only; n8n in
Docker Desktop reaches it as `host.docker.internal`.

### Chat with the agent

The round button at the bottom right of the window opens a chat with the
agent. Messages go to the agent workflow's chat webhook in n8n (default
`http://localhost:5678/webhook/6c1f0e2a-5d3b-4c8e-9f10-deepvacagents01/chat`,
change it with gear menu → **Agent Chat URL…**). The workflow must be
published in n8n. **New chat** starts a conversation the agent doesn't
remember.

While the agent works the chat shows its progress: which agent is busy
(streamed by n8n's chat webhook) and, when the Agent API is on, what each
one does through it, in plain words ("Analyst: reading the summary of run
12 ✓"), from the API's `/activity` feed (same bearer token). The answer is
streamed in as the Professor writes it, and the steps stay above it,
folded into a summary. A workflow whose Chat node doesn't stream still
works; its answer just arrives at the end.

### Analysis Live tab

The first Analysis tab, **● Live**, follows the experiment running on a PLC
chamber now: the job since it started (the last 30 minutes when the chamber
is idle), its stages shaded on the chart, the chamber's state, and KPIs and
anomalies recomputed every 10 s. **Show** switches to the constant
monitoring: the last hour, 6 or 24 hours, today or any recorded day, drawn
from the full-day recordings with the time of day on the axis; the line under
the chart says which day file is being written.

Every experiment is saved as a run by itself when it ends, for every chamber,
whether the tab is open or not (`app/services/experiment_recorder.py`, Deepvac
Control's `ExperimentRecorder`): a job starts when a PLC goes to "work" (or a
sample-feed chamber starts a manual setpoint or test profile) and ends when it
stops; jobs of a minute or longer are saved as "yyyy-MM-dd HH:mm <what was
sent></what>" (" (alarm)" when it ended in one), with a manifest holding the
configuration snapshot taken at the start and the job start / end events. Its
samples are journaled to `data\journal\` every 5 s, so a job cut short by
closing the app is saved at the next start as "(interrupted)". **Save as
Run** saves the current job now. Closed by mistake? Gear menu → **Open Live
Tab**. It shares its PLC connections with the Agent API.

### Chamber control: the PLC gate

The worker talks to the chamber PLC directly over its binary protocol
(`app/services/plc_gate.py`, ported from control2-client's `GateTcp`):
chambers with protocol `plc` in the chambers registry. Chambers with
protocol `json` are sample feeds (connected from Settings → Chamber Connections) and can't be controlled through
the API. Without hardware, run the fake PLC and register it as a `plc`
chamber:

```powershell
uv run python scripts/fake_plc.py --port 4321
```

The Controller page is control2-client's Control page (`app/views/controller.py`,
with `app/program_model.py`, `app/program_editor.py`, `app/program_graph.py`
and `app/program_library.py`), for the chamber PLC chosen at its top right.
Operator commands go to the PLC directly, without the approval queue:

- **Step 1, Experiment details**: name or purpose and operator (required),
  DUT, mass and density, recorded with the run (`details` of its job).
- **Step 2, Intervals**: one card per interval (variable, setpoint, mode:
  full speed then hold / linear ramp / custom ramp rate, duration, label),
  Add / Duplicate / Then hold / Delete, the cycle, and the program library
  (Load / Save: the test profiles database, the full program in
  `program_json`).
- **Step 3, Confirm and start**: **Start**, **Delay start**, **Stop** (asks
  first), **Next interval**, **Confirm finish** and **Confirm alarm**. One
  interval goes as a setpoint job, which the next Start replaces once
  confirmed; more go as a program, which must be stopped first.
- On the right, the profile, its length and when it would end.

Starting a setpoint or program while a job runs asks before replacing it.
The job encoding mirrors control2-client and has been checked against the
fake PLC only: try it on a test chamber before relying on it.

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
