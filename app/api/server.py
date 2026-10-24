"""The agent API: two MCP endpoints (streamable HTTP) behind one bearer token.

    /analyst/mcp   read-only analysis of recorded runs (the Analyst agent)
    /worker/mcp    the chamber now, and requests to change it (the Worker agent)
    /activity      the agents' recent tool calls, for the app's chat
    /requests      chamber requests by id (?ids=1,2), for the app's chat
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
from app.api.chambers import ChamberLinks
from app.api.executor import RequestExecutor
from app.api.worker_tools import WorkerTools, public_request
from app.services import agent_requests_service as requests_service

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
REQUEST = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

ANALYST_INSTRUCTIONS = (
    "Recorded DeepVac chamber experiments. Runs are numbered #1, #2, ... by start time (oldest first) and can "
    "also be named by title, 'latest' or 'previous'. Every number comes from the run data; quote them, don't "
    "estimate. Temperatures are in °C, times in seconds from the run start unless stated."
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
            activity.finished(event, ok=True, request=request)
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
    ) -> dict:
        """Write a run's samples (selected channels and time range) to a CSV file in Insight's reports folder."""
        return tools.export_data(run, channels, from_s, to_s)

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


def build_app(token: str, analyst: AnalystTools, worker: WorkerTools, host: str = DEFAULT_HOST):
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

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(analyst_mcp.session_manager.run())
            await stack.enter_async_context(worker_mcp.session_manager.run())
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

    starlette = Starlette(
        routes=[
            Route("/health", health),
            Route("/activity", recent_activity),
            Route("/requests", requests_by_id),
            Mount("/analyst", app=analyst_app),
            Mount("/worker", app=worker_app),
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
        if self._own_links:
            self.links.close()


def start_in_background(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> AgentApi:
    api = AgentApi(host, port)
    api.start()
    return api


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
