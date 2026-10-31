"""The agent API: MCP endpoints (streamable HTTP) behind one bearer token.

    /analyst/mcp   read-only analysis of recorded runs (the Analyst agent)
    /worker/mcp    the chamber now, and requests to change it (the Worker agent)
    /brain/mcp     the crew's knowledge: the wiki and the experiments database (the Professor)
    /professor/mcp test campaigns: requirement, plan, approval, run, report (the Professor)
    /observer/mcp  events Insight detected on the chambers (the Observer)
    /activity      the agents' recent tool calls, for the app's chat
    /requests      chamber requests by id (?ids=1,2), for the app's chat
    /campaigns     test campaigns by id (?ids=1,2), for the app's chat
    /observer/events  Observer events by id (?ids=1,2), for the Observer workflow
    /health        liveness, no token

Splitting the endpoints keeps each agent to its own tools whatever its MCP
client is configured to include: the analyst endpoint has no path to a chamber.

The token is DEEPVAC_AGENT_API_TOKEN, or generated once into
data/agent_api_token. Run headless with `python -m app.api`; the desktop app
starts the same server (start_in_background) when the Agent API is enabled.
"""

import argparse
import contextlib
import functools
import hmac
import inspect
import json
import logging
import os
import sys
import threading

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import app.common as common
from app.api import activity
from app.api.analyst_tools import AnalystTools
from app.api.auth import (  # noqa: F401 (api_token is part of this module's API)
    TOKEN_FILE,
    api_token,
)
from app.api.brain_tools import BrainTools
from app.api.campaign_supervisor import CampaignSupervisor
from app.api.chambers import ChamberLinks
from app.api.executor import RequestExecutor
from app.api.observer_monitor import ObserverMonitor
from app.api.observer_tools import ObserverTools
from app.api.professor_tools import ProfessorTools
from app.api.worker_tools import WorkerTools, public_request
from app.services import agent_requests_service as requests_service
from app.services import campaign_service, observer_service

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
REQUEST_TOOLS = (
    "request_setpoint",
    "request_program",
    "request_stop",
    "get_request",
    "wait_for_request",
)  # their result is one request: the activity feed notes its id and state
CAMPAIGN_TOOLS = (
    "draft_campaign",
    "submit_campaign",
    "get_campaign",
    "wait_for_campaign",
)
REQUEST = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

ANALYST_INSTRUCTIONS = (
    "Recorded DeepVac chamber experiments. Runs are numbered #1, #2, ... by start time (oldest first) and can "
    "also be named by title, 'latest' or 'previous'. Every number comes from the run data; quote them, don't "
    "estimate. Temperatures are in °C, times in seconds from the run start unless stated."
)
BRAIN_INSTRUCTIONS = (
    "The DeepVac crew's knowledge. The wiki holds how the chamber works, procedures, limits, definitions and "
    "lessons from experiments: search it (wiki_search) and read the pages (wiki_read) before planning, and quote "
    "them. The experiments database answers questions over many runs in SQL (sql_schema first, then sql_query). "
    "wiki_propose suggests a new page or a new version of one; a person approves it."
)
PROFESSOR_INSTRUCTIONS = (
    "Test campaigns on a DeepVac chamber, as a thermal qualification or acceptance test: draft_campaign derives "
    "the specification from the customer's requirement and the standards pack (every value names its source), "
    "submit_campaign asks an operator to approve it, the run supervisor in DeepVac Insight runs it, hold points "
    "stop it until the operator chooses an option (recommend one with your rationale), and issue_report writes "
    "the as-run report. You never send commands to the chamber and never decide a hold point."
)
OBSERVER_INSTRUCTIONS = (
    "Events DeepVac Insight detected on its chambers: alarms, jobs ending, drift from a steady setpoint, anomalies "
    "in the live temperature, a chamber offline, campaign hold points and requests waiting for a person. "
    "get_event gives the facts and context; report_event records your summary and whether people should be "
    "notified. propose_stop / propose_setpoint create a request an operator approves; never on a chamber running "
    "a test campaign."
)
WORKER_INSTRUCTIONS = (
    "The DeepVac chamber now. Reading tools return live PLC values. request_* tools never change the chamber "
    "themselves: they create a request an operator approves or rejects in DeepVac Insight. wait_for_request "
    "waits for that decision (up to a few minutes) and get_request reports the status. Never describe a "
    "requested change as done until the status is 'executed'."
)


def expose(mcp: MCPServer, annotations: ToolAnnotations):
    """Registers a tool; expected failures (a run or chamber that doesn't exist, a value outside the
    chamber's limits, the PLC offline) reach the agent with their message so it can correct itself.
    Anything else is a crash: logged here, reported to the agent without details."""

    agent = mcp.name.rsplit("-", 1)[-1]  # "deepvac-analyst" -> "analyst"

    def register(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def tool(*args, **kwargs):
            try:
                called_with = dict(signature.bind_partial(*args, **kwargs).arguments)
            except TypeError:
                called_with = dict(kwargs)
            event = activity.started(agent, fn.__name__, called_with)  # shown in the app's chat
            try:
                result = fn(*args, **kwargs)
            except (ValueError, LookupError, RuntimeError, ConnectionError) as exc:
                activity.finished(event, ok=False)
                raise ToolError(str(exc)) from exc
            except Exception:
                activity.finished(event, ok=False)
                raise
            request = result if fn.__name__ in REQUEST_TOOLS and isinstance(result, dict) else None
            campaign = (
                result if fn.__name__ in CAMPAIGN_TOOLS and isinstance(result, dict) else None
            )
            activity.finished(event, ok=True, request=request, campaign=campaign)
            # compact JSON text: an LLM reads it, and indented / structured duplicates cost tokens
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        mcp.tool(annotations=annotations, structured_output=False)(tool)
        return fn

    return register


def build_analyst_server(tools: AnalystTools) -> MCPServer:
    mcp = MCPServer("deepvac-analyst", instructions=ANALYST_INSTRUCTIONS)

    @expose(mcp, READ_ONLY)
    def list_runs(
        search: str = "", group: str = "", limit: int = 20, newest_first: bool = True
    ) -> dict:
        """Recorded runs with their number (#N), name, group, start, duration and headline KPIs
        (MAE, tail MAE, overshoot, settling time). Filter by text or group; at most 50."""
        return tools.list_runs(search, group, limit, newest_first)

    @expose(mcp, READ_ONLY)
    def rank_runs(
        metric: str = "tail_mae", group: str = "", limit: int = 10, best_first: bool = True
    ) -> dict:
        """Runs ordered by one metric: mae, tail_mae, overshoot, settling_time, cost (lower is better) or duration."""
        return tools.rank_runs(metric, group, limit, best_first)

    @expose(mcp, READ_ONLY)
    def get_run_summary(run: str) -> dict:
        """What happened in one run: setpoints, PID gains, stage counts, control KPIs, output saturation,
        PID term shares, anomaly counts and the list of stages. run: '#12', a name, 'latest' or 'previous'."""
        return tools.get_run_summary(run)

    @expose(mcp, READ_ONLY)
    def get_stages(run: str) -> dict:
        """The stages of a run (setpoint step, hold, ramp) with their KPIs, time to steady state,
        ramp rate and tracking lag, saturation and PID gains."""
        return tools.get_stages(run)

    @expose(mcp, READ_ONLY)
    def get_kpis(run: str, from_s: float | None = None, to_s: float | None = None) -> dict:
        """Control KPIs (MAE, RMSE, overshoot, rise/fall, settling, tail statistics) of a run,
        or of a time range in seconds from the run start."""
        return tools.get_kpis(run, from_s, to_s)

    @expose(mcp, READ_ONLY)
    def get_statistics(
        run: str,
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
    ) -> dict:
        """Min, max, mean, std, RMS, 95th percentile and trend of channels (default temp, temp_ref, temp_u),
        optionally within a time range in seconds from the run start."""
        return tools.get_statistics(run, channels, from_s, to_s)

    @expose(mcp, READ_ONLY)
    def get_anomalies(run: str, min_severity: str = "info") -> dict:
        """Detected anomalies of a run (limit violations, spikes, flat signals, gaps, oscillation,
        setpoints not reached, long output saturation). min_severity: info, warning or critical."""
        return tools.get_anomalies(run, min_severity)

    @expose(mcp, READ_ONLY)
    def compare_runs(runs: list[str]) -> dict:
        """Two to six runs side by side: KPIs with differences against the first run, the configuration
        that differs, and matching step responses."""
        return tools.compare_runs(runs)

    @expose(mcp, READ_ONLY)
    def list_channels(run: str) -> dict:
        """The recorded channels of a run with label, unit and role."""
        return tools.list_channels(run)

    @expose(mcp, READ_ONLY)
    def get_events(run: str, limit: int = 40) -> dict:
        """Events of a run recorded by DeepVac Control: job start/end, setpoint changes, ramps, PID changes,
        alarms, operator actions."""
        return tools.get_events(run, limit)

    @expose(mcp, READ_ONLY)
    def export_data(
        run: str,
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
        format: str = "csv",
    ) -> dict:
        """Write a run's samples (selected channels and time range) to a file in Insight's reports folder:
        format csv, or txt (tab separated). Returns the file path and a file:// link."""
        return tools.export_data(run, channels, from_s, to_s, format)

    @expose(mcp, READ_ONLY)
    def export_chart(
        run: str,
        kind: str = "temperature",
        channels: list[str] | None = None,
        from_s: float | None = None,
        to_s: float | None = None,
        format: str = "png",
        compare_with: list[str] | None = None,
    ) -> dict:
        """Save a chart of a run as an image (format png, bmp or jpg) in Insight's reports folder.
        kind 'temperature' (temperature and setpoint, stages shaded, anomalies marked) or 'control' (output
        and its P / I / D terms), or give channels; from_s / to_s zoom on a time range. compare_with: other
        runs to overlay on one channel (channels[0], default temp), aligned on their start.
        Returns the file path and a file:// link to give the user."""
        return tools.export_chart(run, kind, channels, from_s, to_s, format, compare_with)

    @expose(mcp, READ_ONLY)
    def export_report(run: str, format: str = "pdf", compare_with: list[str] | None = None) -> dict:
        """Write an experiment report of a run (format pdf or html) as DeepVac Control makes it: summary,
        details, control KPIs, configuration, charts, stages, steady states, anomalies, events, notes;
        with compare_with, also a comparison against those runs. Returns the file path and a file:// link."""
        return tools.export_report(run, format, compare_with)

    @expose(mcp, READ_ONLY)
    def export_comparison(runs: list[str], channel: str = "temp") -> dict:
        """Write a comparison CSV: one row per run with start, end, duration, samples and the min, max, mean
        and final value of a channel. Returns the file path and a file:// link."""
        return tools.export_comparison(runs, channel)

    @expose(mcp, READ_ONLY)
    def export_bundle(run: str) -> dict:
        """Write a run's experiment bundle (JSON: manifest, analysis, samples, annotations and rules), to
        archive it or move it to another installation. Returns the file path and a file:// link."""
        return tools.export_bundle(run)

    @expose(mcp, READ_ONLY)
    def list_recordings(chamber: str = "") -> dict:
        """Days of full-day recording (every sample a chamber sent, whether or not an experiment ran), per
        chamber, with their sample counts. The last 60 days."""
        return tools.list_recordings(chamber)

    @expose(mcp, READ_ONLY)
    def get_recording(
        chamber: str = "", start: str = "", end: str = "", channels: list[str] | None = None
    ) -> dict:
        """What a chamber did in a time range, from its full-day recording: per channel min, max, mean, first
        and last value, the status and setpoint changes, and gaps (the application not connected).
        start / end: local time "YYYY-MM-DD" (a whole day) or "YYYY-MM-DD HH:MM"; empty: today so far."""
        return tools.get_recording(chamber, start, end, channels)

    @expose(mcp, READ_ONLY)
    def simulate_pid(
        kp: float,
        ki: float,
        kd: float,
        target_c: float,
        start_c: float = 27.0,
        duration_min: float = 20.0,
    ) -> dict:
        """Simulate a PID setting on the GRU digital twin of the chamber and get its KPIs.
        A simulation, not a measurement; nothing is sent to a chamber. kp 1-50, ki 1-1000, kd 1-20."""
        return tools.simulate_pid(kp, ki, kd, target_c, start_c, duration_min)

    return mcp


def build_worker_server(tools: WorkerTools) -> MCPServer:
    mcp = MCPServer("deepvac-worker", instructions=WORKER_INSTRUCTIONS)

    @expose(mcp, READ_ONLY)
    def list_chambers() -> dict:
        """The chambers Insight knows, and which ones the API can control (protocol plc)."""
        return tools.list_chambers()

    @expose(mcp, READ_ONLY)
    def get_live_status(chamber: str = "") -> dict:
        """The chamber now: status (idle, work, finish, alarm), temperature and setpoint in °C, control output %,
        heating/cooling, program position, PID gains, active alarms. chamber: id or name (empty: the only PLC chamber)."""
        return tools.get_live_status(chamber)

    @expose(mcp, READ_ONLY)
    def get_settings(chamber: str = "") -> dict:
        """The PLC's current settings with their descriptions."""
        return tools.get_settings(chamber)

    @expose(mcp, READ_ONLY)
    def list_programs() -> dict:
        """Test programs that can be run on the chamber, with their setpoints and duration."""
        return tools.list_programs()

    @expose(mcp, REQUEST)
    def request_setpoint(
        temperature_c: float,
        reason: str,
        chamber: str = "",
        ramp_rate_c_per_min: float | None = None,
        hold_minutes: float | None = None,
    ) -> dict:
        """Ask to set the chamber temperature. Optional ramp (°C/min) from the current temperature, and hold
        time (otherwise held until stopped). Creates a request an operator must approve; nothing is sent yet."""
        return tools.request_setpoint(
            temperature_c, reason, chamber, ramp_rate_c_per_min, hold_minutes
        )

    @expose(mcp, REQUEST)
    def request_program(program: str, reason: str, chamber: str = "") -> dict:
        """Ask to run a test program (see list_programs). Creates a request an operator must approve."""
        return tools.request_program(program, reason, chamber)

    @expose(mcp, REQUEST)
    def request_stop(reason: str, chamber: str = "") -> dict:
        """Ask to stop the job running on the chamber. Creates a request an operator must approve."""
        return tools.request_stop(reason, chamber)

    @expose(mcp, READ_ONLY)
    def get_request(request_id: int) -> dict:
        """Status of a request: pending, approved, rejected, executing, executed, failed or expired,
        with who decided and what was sent."""
        return tools.get_request(request_id)

    @expose(mcp, READ_ONLY)
    def wait_for_request(request_id: int, timeout_s: float = 120) -> dict:
        """Wait until the operator decides on a request and it is sent to the chamber (or rejected, failed,
        expired); at most 180 s. Returns the status; a request still pending after that is decided later,
        and DeepVac Insight tells the chat then."""
        return tools.wait_for_request(request_id, timeout_s)

    @expose(mcp, READ_ONLY)
    def list_requests(status: str = "", limit: int = 10) -> dict:
        """Recent requests, newest first, optionally only one status."""
        return tools.list_requests(status, limit)

    return mcp


class BearerTokenMiddleware:
    """Rejects requests without `Authorization: Bearer <token>` (except the exempt paths)."""

    def __init__(self, app, token: str, exempt=("/health",)):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.exempt = exempt

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in self.exempt:
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                response = JSONResponse({"error": "missing or wrong bearer token"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_brain_server(tools: BrainTools) -> MCPServer:
    mcp = MCPServer("deepvac-brain", instructions=BRAIN_INSTRUCTIONS)

    @expose(mcp, READ_ONLY)
    def wiki_index() -> dict:
        """Every wiki page with its type and one-line summary."""
        return tools.wiki_index()

    @expose(mcp, READ_ONLY)
    def wiki_search(query: str, limit: int = 8) -> dict:
        """Find wiki pages about a topic (words, not a question): the best matches with a snippet."""
        return tools.wiki_search(query, limit)

    @expose(mcp, READ_ONLY)
    def wiki_read(page: str) -> dict:
        """A wiki page in full (markdown), with its sources and the pages it links to and from."""
        return tools.wiki_read(page)

    @expose(mcp, REQUEST)
    def wiki_propose(page: str, content: str, reason: str) -> dict:
        """Propose a new wiki page or a new version of one: the whole page in markdown, starting with
        frontmatter (title, type, tags, sources, updated) as the SCHEMA page describes, and the reason with its
        evidence (run numbers). Nothing changes until a person approves it."""
        return tools.wiki_propose(page, content, reason)

    @expose(mcp, READ_ONLY)
    def sql_schema() -> dict:
        """The experiments database: its tables, what each column means and how many rows each holds."""
        return tools.sql_schema()

    @expose(mcp, READ_ONLY)
    def sql_query(sql: str, max_rows: int = 50) -> dict:
        """Run one read-only SELECT on the experiments database (SQLite); at most 200 rows. Aggregate and filter
        in SQL rather than reading many rows."""
        return tools.sql_query(sql, max_rows)

    return mcp


def build_professor_server(tools: ProfessorTools) -> MCPServer:
    mcp = MCPServer("deepvac-professor", instructions=PROFESSOR_INSTRUCTIONS)

    @expose(mcp, READ_ONLY)
    def get_standards_pack() -> dict:
        """The standards pack campaigns are derived from (margins and cycles per level, dwell, ramp limit,
        stabilisation band, vent window), the chamber's setpoint range and the chambers campaigns can run on."""
        return tools.get_standards_pack()

    @expose(mcp, REQUEST)
    def draft_campaign(
        article: str,
        op_min_c: float,
        op_max_c: float,
        level: str = "qualification",
        chamber: str = "",
        notes: str = "",
        rehearsal: bool = False,
        cycles: int | None = None,
        dwell_min: float | None = None,
        ramp_c_per_min: float | None = None,
        reason: str = "",
    ) -> dict:
        """Draft a test campaign from the customer's requirement: the test article, its lowest and highest
        predicted temperatures (°C) and the level (qualification or acceptance). The plateaus, cycles, dwell, ramp
        and band come from the standards pack; cycles / dwell_min / ramp_c_per_min change them (give the reason).
        rehearsal: shortened dwells to try the campaign out. Returns the specification with the source of every
        value, the steps and the estimated duration. A draft sends nothing."""
        return tools.draft_campaign(
            article,
            op_min_c,
            op_max_c,
            level,
            chamber,
            notes,
            rehearsal,
            cycles,
            dwell_min,
            ramp_c_per_min,
            reason,
        )

    @expose(mcp, REQUEST)
    def submit_campaign(campaign_id: int) -> dict:
        """Submit a drafted campaign for approval: an operator approves or declines it in DeepVac Insight."""
        return tools.submit_campaign(campaign_id)

    @expose(mcp, READ_ONLY)
    def get_campaign(campaign_id: int = 0) -> dict:
        """A campaign (0: the latest): state, stage, the current step and its progress, the open hold point with
        its options, plateau results, deviations, decisions, blocked commands, the last 30 min of temperature."""
        return tools.get_campaign(campaign_id)

    @expose(mcp, READ_ONLY)
    def list_campaigns(limit: int = 10) -> dict:
        """Recent campaigns, newest first, with their state and current step."""
        return tools.list_campaigns(limit)

    @expose(mcp, READ_ONLY)
    def wait_for_campaign(campaign_id: int, timeout_s: float = 120) -> dict:
        """Wait until the campaign's state or hold point changes (approval decided, a hold point opens or is
        decided, the campaign ends); at most 180 s."""
        return tools.wait_for_campaign(campaign_id, timeout_s)

    @expose(mcp, REQUEST)
    def recommend(campaign_id: int, option: str, rationale: str) -> dict:
        """Recommend one option at the campaign's open hold point, with the rationale and the numbers behind it.
        The operator sees it in DeepVac Insight and decides."""
        return tools.recommend(campaign_id, option, rationale)

    @expose(mcp, READ_ONLY)
    def get_report_facts(campaign_id: int) -> dict:
        """The facts of an ended campaign the report's summary may use: plateaus, deviations, decisions,
        blocked commands, overrides."""
        return tools.get_report_facts(campaign_id)

    @expose(mcp, REQUEST)
    def issue_report(campaign_id: int, summary: str) -> dict:
        """Issue the as-run report of an ended campaign (PDF and markdown) with your summary: three plain
        sentences from get_report_facts only (cycles completed, deviations and the decisions taken, what the
        engineer should verify). Returns the file links."""
        return tools.issue_report(campaign_id, summary)

    @expose(mcp, REQUEST)
    def cancel_campaign(campaign_id: int, reason: str) -> dict:
        """Withdraw a draft or a campaign waiting for approval. A running campaign is stopped by an operator."""
        return tools.cancel_campaign(campaign_id, reason)

    return mcp


def build_observer_server(tools: ObserverTools) -> MCPServer:
    mcp = MCPServer("deepvac-observer", instructions=OBSERVER_INSTRUCTIONS)

    @expose(mcp, READ_ONLY)
    def get_event(event_id: int) -> dict:
        """One event with its facts, the chamber now, its last 30 min of temperature and setpoint, the campaign or
        request involved, and the chamber's other events of the last 24 h."""
        return tools.get_event(event_id)

    @expose(mcp, READ_ONLY)
    def list_events(
        hours: float = 24, min_severity: str = "info", chamber: str = "", limit: int = 20
    ) -> dict:
        """Recent events, newest first, with their summary and whether a person acknowledged them.
        min_severity: info, warning or critical."""
        return tools.list_events(hours, min_severity, chamber, limit)

    @expose(mcp, READ_ONLY)
    def get_live_status(chamber: str = "") -> dict:
        """The chamber now: status, temperature, setpoint, control output, active alarms."""
        return tools.get_live_status(chamber)

    @expose(mcp, REQUEST)
    def report_event(event_id: int, summary: str, notify: bool, message: str = "") -> dict:
        """Record your reading of an event: a summary from the facts (two or three sentences), notify true when a
        person should act or know now, and then the message for them (what happened, the numbers, what to check)."""
        return tools.report_event(event_id, summary, notify, message)

    @expose(mcp, REQUEST)
    def propose_stop(event_id: int, reason: str) -> dict:
        """Propose stopping the job on the event's chamber. Creates a request an operator approves or rejects in
        DeepVac Insight; nothing is sent yet. Refused on a chamber running a test campaign."""
        return tools.propose_stop(event_id, reason)

    @expose(mcp, REQUEST)
    def propose_setpoint(
        event_id: int,
        temperature_c: float,
        reason: str,
        ramp_c_per_min: float | None = None,
        hold_minutes: float | None = None,
    ) -> dict:
        """Propose a setpoint on the event's chamber (e.g. back to ambient), with optional ramp and hold. Creates a
        request an operator approves; nothing is sent yet. Refused on a chamber running a test campaign."""
        return tools.propose_setpoint(event_id, temperature_c, reason, ramp_c_per_min, hold_minutes)

    return mcp


def build_app(
    token: str,
    analyst: AnalystTools,
    worker: WorkerTools,
    host: str = DEFAULT_HOST,
    brain: BrainTools | None = None,
    professor: ProfessorTools | None = None,
    observer: ObserverTools | None = None,
):
    # Host header check against DNS rebinding: this machine, and n8n in Docker on it
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "host.docker.internal:*", f"{host}:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    analyst_mcp = build_analyst_server(analyst)
    worker_mcp = build_worker_server(worker)
    analyst_app = analyst_mcp.streamable_http_app(
        streamable_http_path="/mcp", transport_security=security, host=host
    )
    worker_app = worker_mcp.streamable_http_app(
        streamable_http_path="/mcp", transport_security=security, host=host
    )
    brain_mcp = build_brain_server(brain or BrainTools())
    brain_app = brain_mcp.streamable_http_app(
        streamable_http_path="/mcp", transport_security=security, host=host
    )
    professor_mcp = build_professor_server(professor or ProfessorTools(worker.links))
    professor_app = professor_mcp.streamable_http_app(
        streamable_http_path="/mcp", transport_security=security, host=host
    )
    observer_mcp = build_observer_server(observer or ObserverTools(worker.links))
    observer_app = observer_mcp.streamable_http_app(
        streamable_http_path="/mcp", transport_security=security, host=host
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(analyst_mcp.session_manager.run())
            await stack.enter_async_context(worker_mcp.session_manager.run())
            await stack.enter_async_context(brain_mcp.session_manager.run())
            await stack.enter_async_context(professor_mcp.session_manager.run())
            await stack.enter_async_context(observer_mcp.session_manager.run())
            yield

    async def health(_request):
        return JSONResponse({"ok": True, "service": "deepvac-agent-api"})

    async def recent_activity(request):
        """The agents' tool calls since ?since=<epoch s>, for the chat's progress lines."""
        try:
            since = float(request.query_params.get("since", "0"))
        except ValueError:
            since = 0.0
        return JSONResponse({"events": activity.since(since)})

    async def requests_by_id(request):
        """Chamber requests by id (?ids=1,2): the chat follows the ones its crew made."""
        try:
            ids = [int(i) for i in request.query_params.get("ids", "").split(",") if i.strip()]
        except ValueError:
            return JSONResponse({"error": "ids: comma-separated request ids"}, status_code=400)
        requests_service.expire_stale()
        return JSONResponse(
            {"requests": [public_request(r) for r in requests_service.get_requests(ids)]}
        )

    async def campaigns_by_id(request):
        """Test campaigns by id (?ids=1,2)."""
        try:
            ids = [int(i) for i in request.query_params.get("ids", "").split(",") if i.strip()]
        except ValueError:
            return JSONResponse({"error": "ids: comma-separated campaign ids"}, status_code=400)
        found = [campaign_service.get_campaign(i) for i in ids[:50]]
        return JSONResponse(
            {"campaigns": [campaign_service.public(c, detail=False) for c in found if c]}
        )

    async def observer_events(request):
        """Observer events by id (?ids=1,2)."""
        try:
            ids = [int(i) for i in request.query_params.get("ids", "").split(",") if i.strip()]
        except ValueError:
            return JSONResponse({"error": "ids: comma-separated event ids"}, status_code=400)
        events = observer_service.get_events(ids[:50])
        return JSONResponse({"events": [observer_service.public(e) for e in events]})

    starlette = Starlette(
        routes=[
            Route("/health", health),
            Route("/activity", recent_activity),
            Route("/requests", requests_by_id),
            Route("/campaigns", campaigns_by_id),
            Route("/observer/events", observer_events),
            Mount("/analyst", app=analyst_app),
            Mount("/worker", app=worker_app),
            Mount("/brain", app=brain_app),
            Mount("/professor", app=professor_app),
            Mount("/observer", app=observer_app),
        ],
        lifespan=lifespan,
    )
    return BearerTokenMiddleware(starlette, token)


class AgentApi:
    """The API server (and the executor of approved requests) in background threads."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        execute_requests: bool = True,
        links: ChamberLinks | None = None,
    ):
        """links: PLC connections to use; the desktop app passes the ones its Live tab
        uses, so a chamber's PLC gets one connection per app, not two."""
        self.host = host
        self.port = port
        self.token = api_token()
        self._own_links = links is None
        self.links = links or ChamberLinks()
        self.executor = RequestExecutor(self.links) if execute_requests else None
        self.supervisor = CampaignSupervisor(self.links) if execute_requests else None
        self.observer = ObserverMonitor(self.links, self.token) if execute_requests else None
        app = build_app(self.token, AnalystTools(), WorkerTools(self.links), host)
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        )
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self.executor:
            self.executor.start()
        if self.supervisor:
            self.supervisor.start()
        if self.observer:
            self.observer.start()
        self._thread = threading.Thread(target=self._server.run, name="agent-api", daemon=True)
        self._thread.start()

    def wait_started(self, timeout: float = 10.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return True
            if self._thread and not self._thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        if self.executor:
            self.executor.stop()
        if self.supervisor:
            self.supervisor.stop()
        if self.observer:
            self.observer.stop()
        if self._own_links:
            self.links.close()


def start_in_background(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> AgentApi:
    api = AgentApi(host, port)
    api.start()
    return api


def ensure_gui():
    """The analyst's chart images and PDF reports paint with Qt, which needs a QGuiApplication: the
    desktop app has one; headless, make an offscreen one (here, in the main thread)."""
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        if sys.platform == "win32":  # the offscreen platform looks for fonts here only
            fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
            os.environ.setdefault("QT_QPA_FONTDIR", fonts)
        app = QGuiApplication(["deepvac-agent-api"])
    return app


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="DeepVac agent API (MCP): analyst and worker endpoints."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("DEEPVAC_AGENT_API_HOST", DEFAULT_HOST),
        help="interface to listen on (default 127.0.0.1; 0.0.0.0 to reach it from other machines)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("DEEPVAC_AGENT_API_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="don't send approved requests to chambers (another process does)",
    )
    parser.add_argument("--show-token", action="store_true", help="print the bearer token and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.show_token:
        print(api_token())
        return
    gui = ensure_gui()  # noqa: F841 (kept alive while the API runs)
    api = AgentApi(args.host, args.port, execute_requests=not args.no_execute)
    api.start()
    if not api.wait_started():
        raise SystemExit(f"The agent API could not start on {args.host}:{args.port}.")
    print(
        f"DeepVac agent API on {api.url}  (analyst: {api.url}/analyst/mcp, worker: {api.url}/worker/mcp)"
    )
    print(
        f"Bearer token: in {common.DATA_DIR / TOKEN_FILE} (or DEEPVAC_AGENT_API_TOKEN); --show-token prints it"
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        api.stop()
