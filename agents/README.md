# Deepvac Agents: agent orchestration on n8n

A team of specialised agents, organised like a research lab. Each has one role; only a person
approves a change to the chamber.

```
                 Brain ─► wiki + experiments SQL ─► Deepvac Insight API /brain/mcp
                   │
                   ├─► campaign ─► test campaigns ─► Deepvac Insight API /professor/mcp ─► run supervisor ─► PLC
                   │               (decision points decided by an operator in Insight's AI Agents page)
Chat ─► Professor ─┼─► worker   ─► chamber tools ─► Deepvac Insight API /worker/mcp  ─► PLC ─► chamber
        Opus 5.5   │   Haiku 5.5                    (requests wait for approval in Insight)
                   └─► analyst  ─► data tools    ─► Deepvac Insight API /analyst/mcp ─► run database
                       Sonnet 5.5

Insight event ─► Observer ─┬─► observer tools ─► Deepvac Insight API /observer/mcp  (ready, not connected yet)
(alarm, drift…)  Sonnet 5.5 └─► Teams / email ─► people
```

| Agent | Model | Does |
|---|---|---|
| **Professor** | Claude Opus 5.5, effort medium | Plans, asks the analyst for evidence, has the worker act, answers the user; runs test campaigns from the customer's requirement to the as-run report (below) |
| **Worker** | Claude Haiku 5.5, effort low (or a local Qwen model) | Reads the chamber and files requests: set a temperature, run a program, stop |
| **Brain** | (tools, no model) | The agent's knowledge: the wiki (how the chamber works, procedures, limits, lessons) and the experiments database in SQL; the Professor reads it and proposes wiki changes a person approves |
| **Analyst** | Claude Sonnet 5.5, effort low | Evaluates recorded runs: summaries, stages, KPIs, anomalies, comparisons, PID simulation; reads the full-day recordings (what a chamber did on any day); exports chart images, PDF / HTML reports, CSV / TXT data and bundles |
| **Observer** | Claude Sonnet 5.5, effort low (its own workflow) | Event-driven: Insight sends it what it detects on the chambers (alarm, job ended, drift, anomaly, offline, decision point or request waiting); it summarises each event, notifies people by Teams or email when someone should act, and can propose a stop or setpoint an operator approves. Ready, not connected (below) |

The tools live in the Deepvac Insight app (this repository, see its [README](../README.md),
*Agent API*). For using the agent from Insight's interface, see the
[user guide](../docs/agents-user-guide.md). The
worker and analyst each reach their own endpoint, so the analyst has no path to the chamber.
The worker can only file requests: an operator approves or rejects each one in Insight's
**Agent Requests** dialog before anything is sent to the PLC.

## Files

| Path | What it is |
|---|---|
| `docker-compose.yml` | n8n 2.44.6 on this PC only, http://localhost:5678 |
| `workflows/deepvac-agents.json` | The agents: chat, Professor + memory, worker and analyst agents, their models and MCP tool nodes |
| `workflows/deepvac-observer.json` | The Observer: Insight's event webhook, the Observer agent and its MCP tools, its decision, Teams and email |

## Run it

1. **Insight API.** In Insight: gear menu → **Agent API** (or `uv run python -m app.api` at the
   repository root). Then gear menu → **Copy Agent API Token**.
2. **n8n.** From this `agents` folder:
   ```powershell
   docker compose up -d
   docker compose exec n8n n8n import:workflow --input=/workflows/deepvac-agents.json
   ```
3. Open http://localhost:5678, create the owner account (it stays on this PC), then create
   two credentials:
   - **Anthropic**: an API key from console.anthropic.com.
   - **Bearer Auth**: the Insight token from step 1.
4. Open **Deepvac Agents** and select the Anthropic credential in the three Claude nodes and the
   Bearer credential in the MCP nodes (`chamber`, `data`, `campaign`, `brain`), then **Publish** the workflow.
5. Chat: click the round chat button at the bottom right of Deepvac Insight (or **Open chat**
   in the n8n editor) and ask, e.g. *"How did the last two 0 °C runs compare, and what is the
   chamber doing now?"*

The Chat node streams its response (option *Response Mode: Streaming*): Insight's chat shows
which agent is working and the answer as the Professor writes it. **If you imported the
workflow before streaming, the approval loop, the exports, the recording tools and the campaigns were added, import it again and Publish** (re-select the
credentials), or set that option on the Chat node yourself.

**Approval loop.** After filing a request the worker calls `wait_for_request`, which waits up
to 2 minutes for the operator (the `chamber` node's timeout is 200 s to allow for it). If you
decide later, or when the job ends, Insight posts an update into the same chat session and the
Professor tells you what came of it. These updates are real agent turns and cost tokens like any
other message.

The Chat node is a public webhook so that Insight can post to it without logging in to n8n;
that is safe only because n8n listens on 127.0.0.1 (see `docker-compose.yml`). If n8n is ever
exposed on a network, set the Chat node's authentication to basic auth first.

To keep changes made in the n8n editor, export the workflow back into git:
```powershell
docker compose exec n8n n8n export:workflow --id=deepvacAgents01 --pretty --output=/home/node/export.json
docker compose cp n8n:/home/node/export.json workflows/deepvac-agents.json
```

## Test campaigns (the Professor)

The Professor runs a thermal test campaign the way a test engineer would, in five stages:

| Stage | Who | What happens |
|---|---|---|
| Requirement | user ↔ Professor | the test article, its lowest and highest predicted temperatures, qualification or acceptance |
| Plan | Professor (`draft_campaign`) | Insight derives the specification from the standards pack (`data/standards_pack.json` in Insight; illustrative ECSS-style values until an engineer replaces them): plateaus = predictions ± the level's margin, cycles, dwell, ramp limit, stabilisation band. Every value names its source; a change asks for a reason |
| Approval | operator | `submit_campaign` opens a hold point in Insight's **Test Campaigns** page; nothing moves until the operator approves |
| Run | Insight's run supervisor | deterministic code, not an agent: ramps and dwells on the PLC, through gateway rules (only the supervisor commands the chamber; plateaus inside the chamber's range; ramp ≤ the specification; cycling after the pump-down is confirmed; venting only inside the vent window and approved). Hold points stop the campaign for the operator: pump-down, a temperature excursion during a dwell, a plateau that doesn't stabilise, an interrupted job (alarm, stop, another job), venting. The Professor hears of each one in the chat, checks the data and `recommend`s an option; the operator decides |
| Report | Professor (`issue_report`) | an as-run PDF / markdown report: specification with sources, plateau results, deviations, decisions with the recommendations, blocked commands, the temperature chart, and the log's SHA-256 chain with its last hash signed by the installation's Ed25519 key. An operator signs it off |

Deepvac's chamber measures one temperature and no vacuum, so a campaign is thermal: pump-down
and venting are steps the operator confirms. A **rehearsal** campaign shortens the dwells to try
the flow out. The campaigns run while Insight's Agent API is on (it hosts the supervisor); they
survive a restart of Insight, and the PLC ends each hold on its own if Insight stays away.

## The Observer (ready, not connected)

The Observer is not part of the chat: it has its own workflow, **Deepvac Observer**, started by
Insight whenever something happens on a chamber.

1. **Insight detects** (the observer monitor, deterministic code running with the Agent API):
   an alarm (critical) and when it clears; a job ending (finished, stopped, in alarm); drift, the
   temperature more than 3 °C from a setpoint that has been steady for 10 min, for 5 min (not
   during ramps); anomalies in the live temperature (oscillation, spikes, a stuck sensor, with
   the run analysis's detectors); a chamber not heard from for 60 s and when it is back; a campaign
   hold point waiting (the approval after 10 min) and an agent request waiting 10 min. Each event
   is recorded once in Insight (gear menu → **Observer Events**, where people acknowledge them).
2. **Insight sends** the events from *warning* up to the workflow's webhook (POST, with the API
   token as `Authorization: Bearer`), retrying with a growing delay. **Off by default.**
3. **The Observer** reads the event with its tools (`get_event`: facts, the chamber now, its last
   30 min, the campaign or request involved, earlier events; `list_events`; `get_live_status`),
   then `report_event`: a summary, whether to notify, and the message. When the chamber is at risk
   and nobody may be there it can `propose_stop` or `propose_setpoint` (back to ambient): ordinary
   requests an operator approves, refused on a chamber running a test campaign.
4. **The workflow** reads the decision from Insight (`/observer/events`) and, when it says notify,
   posts the message to a Teams channel (a Teams *Workflows* webhook, Adaptive Card) and sends an
   email (SMTP).

To connect it, once the agent works:

```powershell
docker compose exec n8n n8n import:workflow --input=/workflows/deepvac-observer.json
```

1. In n8n create a **Header Auth** credential: name `Authorization`, value `Bearer <Insight token>`.
2. Open **Deepvac Observer**: select it in *Insight event*, the Anthropic credential in the Claude
   node, and the Bearer credential in *observer* and *Decision*.
3. *Teams*: in Teams, add the *Workflows* template "Post to a channel when a webhook request is
   received" to the lab's channel, paste its URL into the node and enable it. *Email*: create an
   SMTP credential, set the addresses and enable it. Either or both.
4. **Publish**. In Insight: gear menu → **Observer Events** → tick *Send events to the Observer
   workflow* → **Save Settings**, then **Send Test Event** (the Observer should report it without
   notifying anyone).

Each event the Observer reads is an agent run and costs tokens; the threshold (*Send events from*)
and the drift settings are in the same dialog, or in Insight's `data/observer.json`.

## The Brain

The **brain** node (Insight's `/brain/mcp`: `wiki_index`, `wiki_search`, `wiki_read`,
`wiki_propose`, `sql_schema`, `sql_query`) is on the Professor's *Tool* input, with the same Bearer
credential as the other MCP nodes. The Professor's system message tells it to search and read the
wiki before planning and before recommending at a decision point, to query the experiments
database for questions over many runs, and to propose wiki pages when it learns something. The
wiki and the experiments database are described in Insight's README (*The Brain*).

- Rebuild the experiments database and the generated pages after adding runs:
  `uv run python scripts/build_brain.py` in Insight (about 2 minutes).
- Wiki changes the Professor proposes wait in Insight (gear menu → **Wiki Proposals**).
- Insight must be running with its Agent API on (port 8765) for any of the MCP nodes to answer.

## Worker on a local model

The workflow has a disconnected **Local model (worker, alternative)** node: Qwen3-4B-Instruct
through llama-server's OpenAI-compatible API, as shipped in `control2-client/llm`. To use it:

1. Start llama-server on port 8089, for example
   `llama-server.exe -m models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf --host 127.0.0.1 --port 8089 --jinja`
   (or let control2-client's assistant start it).
2. In n8n create an **OpenAI** credential with any API key and Base URL
   `http://host.docker.internal:8089/v1`, and select it in that node.
3. Drag the node's output onto the worker's *Chat Model* input in place of Haiku.

A 4B model follows these tools less reliably than Haiku; the approval step in Insight is what
keeps the chamber safe either way.

## Things to know

- **Thinking can't be turned off** on these Claude models; the workflow sets adaptive thinking
  and an effort per agent.
- **Known issue (n8n 2.44.6):** after a response with thinking, n8n sends earlier tool calls
  back to Claude with empty inputs and splits parallel calls into separate turns (seen with
  a mock of the API). On Anthropic accounts created on or after 2026-08-31, Claude Opus 5.5,
  Sonnet 5.5 and Haiku 5.5 reject that edited history: an agent's second tool round fails with
  *"…bound to a different conversation"*. If you see it, use `claude-opus-5` / `claude-sonnet-5`
  in the affected nodes, which don't run that check.
- **Tested** end to end with a mock of the Claude API: n8n's MCP client against the Insight
  API and a fake PLC. Not yet tested against the real Claude API or a real chamber.
- n8n prefixes MCP tools with the node name: the worker sees `chamber_get_live_status`, the
  analyst `data_compare_runs`.
- **Data** (workflows, executions, credentials and their encryption key) lives in the `n8n_data`
  Docker volume; `docker compose down -v` deletes it.

## Next

- **Observer**: built, not connected (above); connect it after the worker tests, with the
  team's Teams channel or mail addresses.
