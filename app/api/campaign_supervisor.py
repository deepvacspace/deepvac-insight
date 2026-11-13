"""Run supervisor: runs approved test campaigns on their chamber."""

import contextlib
import json
import logging
import math
import threading
import time

from app.api import control_settings
from app.api.chambers import ChamberLinks
from app.core.analysis import anomalies
from app.services import campaign_service as cs
from app.services import hold_advisor, plc_gate
from app.services.pressure import to_torr

log = logging.getLogger(__name__)


def _torr(value, unit):
    """Formats a pressure in Torr."""
    return f"{cs.sci(to_torr(value, unit))} Torr"


SUPERVISOR = "Run supervisor"
GATEWAY = "Gateway"
TRACE_S = 15.0
STALE_S = 15.0
START_TIMEOUT_S = 60.0
WATCHDOG_MARGIN_MIN = 120.0
RENEW_BEFORE_S = 30 * 60.0
SETPOINT_TOLERANCE_C = 0.5
TAKEOVER_S = 30.0
TAKEOVER_WALL_S = 3.0
SETTINGS_WAIT_S = 5.0
OSCILLATION_CHECK_S = 30.0
PUMP_EXTEND_MIN = 60.0
MIN_OSCILLATION_SAMPLES = 20
SENSOR_CHECK_S = 15.0
SENSOR_RECHECK_S = 120.0
PRESSURE_OVERRIDE_S = 30 * 60.0
CYCLING = ("ramp", "dwell")


def _finite(value):
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


class Context:
    """One campaign during one supervisor pass."""

    def __init__(self, campaign):
        self.campaign = campaign
        self.id = campaign["id"]
        self.original_state = campaign["state"]
        self.state = campaign["state"]
        self.spec = campaign["spec"]
        self.steps = campaign["steps"]
        self.rt = dict(campaign["runtime"] or {})
        self.results = list(campaign["results"] or [])
        self.deviations = list(campaign["deviations"] or [])
        self.blocked = list(campaign["blocked"] or [])
        self.fields = {}
        self.snap = None
        self.wall = None

    @property
    def step(self):
        index = self.rt.get("step_index", -1)
        return self.steps[index] if 0 <= index < len(self.steps) else None

    def end(self, state, reason):
        self.state = state
        self.fields["ended_at"] = cs.now_iso()
        self.fields["end_reason"] = reason


class CampaignSupervisor:
    POLL_S = 1.0

    def __init__(self, links: ChamberLinks, advisor=None):
        self.links = links
        self.advisor = advisor or hold_advisor.advise
        self._attempts: list[tuple[int, str, dict]] = []
        self._attempts_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="campaign-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_S):
            try:
                self.run_once()
            except Exception:
                log.exception("campaign supervisor")

    def run_once(self, now: float | None = None) -> None:
        for campaign in cs.list_campaigns(20, cs.ACTIVE):
            try:
                self.tick(campaign, now or time.time())
            except Exception:
                log.exception("campaign %s", campaign["id"])

    def submit_attempt(self, campaign_id: int, actor: str, command: dict) -> None:
        """Queues a command from another actor for the gateway to check."""
        with self._attempts_lock:
            self._attempts.append((int(campaign_id), actor, dict(command)))

    def _check_attempts(self, ctx: Context, now: float) -> None:
        with self._attempts_lock:
            mine = [a for a in self._attempts if a[0] == ctx.id]
            self._attempts = [a for a in self._attempts if a[0] != ctx.id]
        for _, actor, command in mine:
            self.gateway(ctx, actor, command, now)

    def tick(self, campaign: dict, wall: float) -> None:
        ctx = Context(campaign)
        ctx.wall = wall
        now = self._chamber_now(ctx, wall)
        ctx.rt["clock"] = {"plc": now, "wall": wall, "rate": self._snapshot(ctx).plc_rate or 1.0}
        self._check_attempts(ctx, now)
        if campaign.get("stop_request") and ctx.state in (cs.RUNNING, cs.HOLDING):
            self._stop_now(ctx, json.loads(campaign["stop_request"]), now)
        else:
            for hold in cs.decided_unapplied(ctx.id):
                if cs.mark_applied(hold["id"]) and ctx.state in cs.ACTIVE:
                    self._apply(ctx, hold, now)
            if ctx.state == cs.HOLDING and cs.open_hold_of(ctx.id) is None:
                ctx.state = cs.RUNNING
            if ctx.state in (cs.RUNNING, cs.HOLDING):
                self._supervise(ctx, now)
        self._save(ctx)

    def _save(self, ctx: Context) -> None:
        cs.update(
            ctx.id,
            ctx.original_state,
            state=ctx.state,
            runtime=ctx.rt,
            results=ctx.results,
            deviations=ctx.deviations,
            blocked=ctx.blocked,
            **ctx.fields,
        )

    def _snapshot(self, ctx: Context):
        if ctx.snap is None:
            chamber = self.links.resolve(ctx.campaign["chamber_id"])
            ctx.snap = self.links.client(chamber, wait=False).snapshot()
        return ctx.snap

    def _client(self, ctx: Context):
        return self.links.client(self.links.resolve(ctx.campaign["chamber_id"]), wait=False)

    def _chamber_now(self, ctx: Context, wall: float) -> float:
        """Returns the PLC clock now."""
        snap = self._snapshot(ctx)
        if snap.plc_time is None or snap.values_at is None:
            return wall
        return plc_gate.plc_clock_now(snap.plc_time, snap.values_at, snap.plc_rate or 1.0, wall)

    def _live(self, ctx: Context, now: float = 0.0):
        """Returns the snapshot when the chamber is connected with fresh values, else None."""
        snap = self._snapshot(ctx)
        wall = ctx.wall if ctx.wall is not None else now
        if snap.connected and snap.values_at and wall - snap.values_at <= STALE_S:
            return snap
        return None

    def _samples(self, ctx: Context, since: float) -> list[dict]:
        """The chamber's samples newer than `since` on the PLC's clock, each with "t" on it."""
        client = self._client(ctx)
        plc_history = getattr(client, "plc_history", None)
        if plc_history is not None:
            return [{**s, "t": s["plc_time"]} for s in plc_history(since)]
        history = getattr(client, "history", None)
        return [{**s, "t": s["timestamp"]} for s in history(since)] if history else []

    def _pressure(self, ctx: Context):
        """Returns the chamber pressure, or None."""
        snap = self._live(ctx)
        value = snap.values.get("pres") if snap is not None else None
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return float(value)
        return None

    def _pressure_unit(self) -> str:
        meta = control_settings.state_meta(control_settings.load()).get("pres") or {}
        return meta.get("unit") or "Torr"

    def gateway(self, ctx: Context, actor: str, command: dict, now: float) -> list[str]:
        """Returns the reasons a command is refused."""
        reasons = []
        name = command["name"]
        spec = ctx.spec
        if name == "pid_table":
            if not command.get("decided_by") or actor != command["decided_by"]:
                reasons.append("Only a person changes PID settings, at a decision point.")
            table = ctx.rt.get("pid_table") or {}
            values = command.get("values") or {}
            if not values or set(values) - set(table.get("gains") or {}):
                reasons.append("Only the gains of the temperature PID table can be written.")
            elif any(abs(float(v) - table["gains"][k]) > 1e-6 for k, v in values.items()):
                reasons.append("Only the operator's recorded PID table can be written back.")
        elif name == "force_output":
            reasons.append(
                "Nobody forces PLC outputs during a campaign: the PLC's own interlocks stay primary."
            )
        elif name == "settings":
            keys = [str(k).lower() for k in (command.get("values") or {})]
            if any(k.startswith(("alarm", "interlock")) for k in keys):
                reasons.append(
                    "Only the customer's Service Engineer changes alarm and interlock settings, "
                    "never during a campaign."
                )
            else:
                reasons.append(
                    "Only a person changes PID and calibration settings, at a decision point."
                )
        elif name == "decide":
            reasons.append(
                "Only a signed-in person decides at decision points: an agent's recommendation is "
                "never a decision, and nobody approves their own plan."
            )
        elif actor != SUPERVISOR:
            reasons.append("Only the run supervisor may send commands to the chamber.")
        if ctx.state not in (cs.RUNNING, cs.HOLDING):
            reasons.append("The campaign is not approved and running.")
        snap = self._live(ctx, now)
        if name in ("setpoint", "stop", "pid_table") and snap is None:
            reasons.append("The chamber is not connected.")
        if name == "setpoint":
            if snap is not None and snap.status == "alarm":
                reasons.append("The chamber is in alarm: acknowledge it at the chamber first.")
            target = float(command["target_c"])
            allowed = (spec["hot_c"], spec["cold_c"], spec["ambient_c"])
            held = (
                command.get("hold") == "pressure"
                and spec.get("pressure_rule")
                and actor == SUPERVISOR
                and min(allowed) - 1e-6 <= target <= max(allowed) + 1e-6
            )
            if command.get("skip_vacuum") and spec["vacuum"] in ("manual", "auto"):
                reasons.append(
                    "Thermal cycling runs only in vacuum: the pump-down to "
                    f"{cs.sci(cs.hpa_to_torr(spec['vacuum_start_hpa']))} Torr can't be skipped."
                )
            if not held and not any(abs(target - a) < 1e-6 for a in allowed):
                reasons.append(
                    f"{cs.deg(target)} is not one of the campaign's plateaus or ambient."
                )
            low, high = control_settings.temperature_limits(control_settings.load())
            if not low <= target <= high:
                reasons.append(
                    f"{cs.deg(target)} is outside the chamber's range {low:g} … {high:g} °C."
                )
            rate = command.get("ramp_c_per_min")
            if rate and rate > spec["ramp_c_per_min"] + 1e-9:
                reasons.append(
                    f"A ramp of {rate:g} °C/min is faster than the specification's {spec['ramp_c_per_min']:g}."
                )
            if (
                command.get("start_cycling")
                and spec["vacuum"] in ("manual", "auto")
                and not ctx.rt.get("pump_confirmed")
            ):
                reasons.append(
                    "Thermal cycling needs the chamber pumped down: read on the gauge or confirmed "
                    "by the operator."
                )
        if name == "vent":
            if not command.get("approved"):
                reasons.append("Venting needs the operator's approval at a decision point.")
            temp = snap.values.get("temp") if snap is not None else None
            low, high = spec["vent_band_c"]
            if temp is None:
                reasons.append("Venting needs a live chamber temperature.")
            elif not low <= temp <= high:
                reasons.append(
                    f"Venting is locked while the chamber reads {temp:.1f} °C, outside {low:g} to {high:g} °C."
                )
        if reasons:
            text = command.get("label") or name
            ctx.blocked.append(
                {"time": cs.now_iso(), "actor": actor, "command": text, "reasons": reasons}
            )
            cs.record(
                ctx.id, GATEWAY, "Command blocked", f"{actor} asked to {text}. " + " ".join(reasons)
            )
        return reasons

    def _send_setpoint(
        self,
        ctx: Context,
        target: float,
        now: float,
        ramp: bool = True,
        start_cycling=False,
        hold: bool = False,
    ) -> bool:
        """Sends a setpoint job, ramped or held."""
        spec = ctx.spec
        snap = self._live(ctx, now)
        current = snap.values.get("temp") if snap is not None else None
        rate = spec["ramp_c_per_min"]
        ramp = ramp and not hold
        ramp_s = abs(current - target) / rate * 60.0 if ramp and current is not None else 0.0
        label = f"set {cs.deg(target)}" + (f" ramping at {rate:g} °C/min" if ramp_s >= 1 else "")
        if hold:
            label = f"hold {cs.deg(target)} (pressure rule)"
        command = {
            "name": "setpoint",
            "target_c": target,
            "ramp_c_per_min": rate if ramp_s >= 1 else None,
            "start_cycling": start_cycling,
            "label": label,
        }
        if hold:
            command["hold"] = "pressure"
        reasons = self.gateway(ctx, SUPERVISOR, command, now)
        if reasons:
            self._interrupt(
                ctx, now, "The gateway refused the step's command: " + " ".join(reasons)
            )
            return False
        hold_s = (spec["stabilise_timeout_min"] + spec["dwell_min"] + WATCHDOG_MARGIN_MIN) * 60.0
        settings = control_settings.load()
        intervals = plc_gate.manual_setpoint_intervals(
            target,
            current,
            control_settings.enable_flags(settings),
            rate if ramp_s >= 1 else None,
            hold_s,
        )
        try:
            self._client(ctx).send_job(
                plc_gate.PROG_TYPE_SETPOINT, intervals, control_settings.control_params(settings)
            )
        except Exception as exc:
            self._interrupt(ctx, now, f"Sending the setpoint failed: {exc}")
            return False
        ctx.rt.update(
            job_sent_at=now,
            job_sent_wall=ctx.wall if ctx.wall is not None else now,
            job_ramp_s=ramp_s,
            job_hold_s=hold_s,
            job_target=target,
            job_confirmed=False,
            off_setpoint_since=None,
        )
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Setpoint sent",
            f"{label}; the PLC holds it at most {hold_s / 3600:.1f} h unless renewed",
        )
        return True

    def _send_stop(self, ctx: Context, now: float, why: str) -> None:
        if self.gateway(ctx, SUPERVISOR, {"name": "stop", "label": "stop the chamber's job"}, now):
            return
        try:
            self._client(ctx).send_text_cmd("stop")
            cs.record(ctx.id, SUPERVISOR, "Chamber job stopped", why)
        except Exception as exc:
            cs.record(ctx.id, SUPERVISOR, "Stopping the chamber's job failed", str(exc))
        ctx.rt["job_sent_at"] = None

    def _apply(self, ctx: Context, hold: dict, now: float) -> None:
        kind, choice = hold["kind"], hold["choice"]
        if choice == "cancelled":
            return
        who = hold["decided_by"]
        self._resume_clock(ctx, now)
        if kind == "approval":
            if choice == "approve":
                ctx.state = cs.RUNNING
                ctx.fields["started_at"] = cs.now_iso()
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Campaign started",
                    f"{len(ctx.steps)} steps, approved by {who}",
                )
                ctx.rt["started_plc"] = now
                self._record_pid_table(ctx)
                self._configure_article(ctx)
                self._enter(ctx, 0, now)
            else:
                ctx.state = cs.DRAFT
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Approval declined",
                    f"{who} declined; the plan is back for changes",
                )
            return
        if kind == "pump_slow":
            ctx.state = cs.RUNNING
            if choice == "wait":
                ctx.rt["pump_deadline"] = now + PUMP_EXTEND_MIN * 60.0
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Waiting longer",
                    f"{PUMP_EXTEND_MIN:g} min more to pump down",
                )
                return
            if choice == "confirm":
                ctx.deviations.append(
                    {
                        "time": cs.now_iso(),
                        "kind": "pump-down",
                        "where": "pump-down",
                        "text": f"Cycling started above the start pressure, by {who}.",
                        "decision": f"start cycling anyway ({who})",
                    }
                )
                ctx.rt["pump_confirmed"] = True
                self._enter(ctx, ctx.rt["step_index"] + 1, now)
                return
            self._send_stop(ctx, now, f"{who} aborted the pump-down")
            ctx.end(cs.ABORTED, f"aborted by {who} before cycling")
            cs.record(
                ctx.id, SUPERVISOR, "Campaign aborted", "before the chamber was heated or cooled"
            )
            return
        if kind == "pump":
            if choice == "confirm":
                ctx.rt["pump_confirmed"] = True
                ctx.state = cs.RUNNING
                self._enter(ctx, ctx.rt["step_index"] + 1, now)
            else:
                ctx.end(cs.ABORTED, f"aborted by {who} before cycling")
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Campaign aborted",
                    "before the chamber was heated or cooled",
                )
            return
        if choice == "abort" or (kind == "interrupted" and choice == "ambient"):
            self._abort(ctx, now, f"{who} chose to abort")
            return
        ctx.state = cs.RUNNING
        if kind == "anomaly":
            ctx.rt["out_since"] = None
            if choice == "extend_dwell":
                ctx.rt["dwell_extra_min"] = (
                    ctx.rt.get("dwell_extra_min", 0.0) + ctx.spec["extend_dwell_min"]
                )
                cs.record(
                    ctx.id, SUPERVISOR, "Dwell extended", f"by {ctx.spec['extend_dwell_min']:g} min"
                )
            elif choice == "accept":
                ctx.rt["excursions_accepted"] = True
                if ctx.deviations:
                    ctx.deviations[-1]["decision"] = "accepted, logged as a deviation"
        elif kind == "oscillation":
            ctx.rt["osc_check_from"] = now
            ctx.rt["osc_checked_at"] = now
            if choice == "revert_pid":
                self._restore_pid_table(ctx, who, now)
            elif choice == "accept":
                ctx.rt["oscillation_accepted"] = True
                self._decide_deviation(ctx, "oscillation", "accepted, logged as a deviation")
        elif kind == "pressure":
            held = ctx.rt.get("pressure_hold")
            if held is None:
                return
            if choice == "wait":
                held.update(since=now, asked=False)
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Holding longer",
                    f"{who}: the temperature stays held until the pressure falls below the limit",
                )
            elif choice == "resume":
                text = (
                    f"Resumed above the pressure limit by {who}, "
                    f"{(now - held['since']) / 60:.0f} min into the hold."
                )
                self._decide_deviation(ctx, "pressure", f"resumed anyway ({who})")
                ctx.deviations.append(
                    {
                        "time": cs.now_iso(),
                        "kind": "pressure",
                        "where": (ctx.step or {}).get("label", ""),
                        "text": text,
                        "decision": f"resume anyway ({who})",
                    }
                )
                ctx.rt["pressure_override_until"] = now + PRESSURE_OVERRIDE_S
                self._end_pressure_hold(ctx, now, f"resumed by {who}")
        elif kind == "sensor":
            tcs = ctx.rt.setdefault("tc", {})
            key = ctx.rt.pop("sensor_hold", None)
            info = tcs.get(key) if key else None
            if info is None:
                return
            if choice == "exclude":
                info["status"] = "excluded"
                self._decide_deviation_of(ctx, key, f"excluded from the criterion by {who}")
                cs.record(
                    ctx.id,
                    who,
                    "Thermocouple excluded",
                    f"{info['name']} ({key}) leaves the stabilisation criterion: {info['reason']}",
                )
            elif choice == "recheck":
                info.update(status="recheck", recheck_at=now + SENSOR_RECHECK_S)
                cs.record(
                    ctx.id,
                    who,
                    "Thermocouple to check again",
                    f"{info['name']} ({key}) is tested again in {SENSOR_RECHECK_S / 60:g} min",
                )
        elif kind == "stabilise":
            if choice == "wait":
                ctx.rt["stabilise_deadline"] = now + ctx.spec["extend_stabilise_min"] * 60.0
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Waiting longer",
                    f"{ctx.spec['extend_stabilise_min']:g} min more to stabilise",
                )
            elif choice == "start_dwell":
                self._start_dwell(ctx, now, forced=True)
        elif kind == "interrupted":
            if choice == "resume":
                step = ctx.step
                target = step.get("target_c") if step else None
                if step and step["kind"] == "pump" and ctx.rt.get("pump_mode") == "gauge":
                    target = ctx.spec["ambient_c"]
                if target is not None:
                    self._send_setpoint(ctx, target, now, ramp=step["kind"] != "dwell")
            elif choice == "stop":
                self._send_stop(ctx, now, f"{who} ended the campaign")
                ctx.end(cs.ABORTED, f"ended by {who} after the chamber's job was interrupted")
        elif kind == "vent":
            reasons = self.gateway(
                ctx,
                SUPERVISOR,
                {"name": "vent", "approved": True, "label": "vent the chamber"},
                now,
            )
            if reasons:
                self._hold(
                    ctx,
                    "vent",
                    "Back at ambient. Vent the chamber?",
                    "The gateway refused: " + " ".join(reasons),
                )
                return
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Vent approved",
                "the gateway passed; the operator vents the chamber",
            )
            self._enter(ctx, ctx.rt["step_index"] + 1, now)

    def _stop_now(self, ctx: Context, request: dict, now: float) -> None:
        self._send_stop(ctx, now, f"stop requested by {request['by']}")
        conn = cs.connect()
        try:
            conn.execute(
                "UPDATE holds SET decided_at = ?, decided_by = ?, choice = 'cancelled', applied = 1 "
                "WHERE campaign_id = ? AND decided_at IS NULL",
                (cs.now_iso(), request["by"], ctx.id),
            )
            conn.commit()
        finally:
            conn.close()
        ctx.rt["aborted"] = True
        ctx.end(
            cs.ABORTED,
            f"stopped by {request['by']}"
            + (f": {request['reason']}" if request.get("reason") else ""),
        )
        cs.record(ctx.id, SUPERVISOR, "Campaign stopped", ctx.fields["end_reason"])

    def _enter(self, ctx: Context, index: int, now: float) -> None:
        rt = ctx.rt
        rt.update(
            step_index=index,
            step_started_at=now,
            stabilised_at=None,
            dwell_paused_s=0.0,
            dwell_extra_min=0.0,
            hold_paused_at=None,
            out_since=None,
            min_c=None,
            max_c=None,
            excursions_accepted=False,
            oscillation_accepted=False,
            osc_check_from=None,
            forced_dwell=False,
            stabilise_deadline=None,
            pressure_hold=None,
            pressure_paused_at=None,
            stab_status=None,
            dut_min_c=None,
            dut_max_c=None,
        )
        if index >= len(ctx.steps):
            self._complete(ctx, now)
            return
        step = ctx.steps[index]
        cs.record(
            ctx.id, SUPERVISOR, "Step started", f"{index + 1}/{len(ctx.steps)}: {step['label']}"
        )
        kind = step["kind"]
        if kind == "pump" and ctx.spec["vacuum"] == "auto" and self._pressure(ctx) is not None:
            self._start_pump_down(ctx, now)
        elif kind == "pump":
            self._hold(
                ctx,
                "pump",
                "Pump the chamber down and confirm.",
                "No vacuum gauge reading from the chamber: confirm when it has reached the start pressure.",
            )
        elif kind in ("ramp", "ambient"):
            first = kind == "ramp" and not any(s["kind"] == "ramp" for s in ctx.steps[:index])
            self._send_setpoint(ctx, step["target_c"], now, start_cycling=first)
        elif kind == "dwell":
            rt["stabilise_deadline"] = now + ctx.spec["stabilise_timeout_min"] * 60.0
        elif kind == "vent":
            snap = self._live(ctx, now)
            temp = snap.values.get("temp") if snap is not None else None
            self._hold(
                ctx,
                "vent",
                "Back at ambient. Vent the chamber?",
                (f"The chamber reads {temp:.1f} °C. " if temp is not None else "")
                + "Venting returns the chamber to atmosphere and ends the campaign.",
            )

    def _hold(self, ctx: Context, kind: str, title: str, detail: str, facts=None) -> None:
        if cs.open_hold_of(ctx.id) is not None:
            return
        hold = cs.open_hold(ctx.id, kind, title, detail)
        ctx.state = cs.HOLDING
        try:
            self.advisor(ctx.id, hold, self._facts(ctx, kind, facts))
        except Exception:
            log.exception("hold advisor, campaign %s", ctx.id)

    def _facts(self, ctx: Context, kind: str, extra=None) -> dict:
        """What the Analyst needs to recommend at a decision point."""
        snap = self._snapshot(ctx)
        values = snap.values or {}
        step = ctx.step
        facts = {
            "hold": kind,
            "article": ctx.spec.get("article"),
            "step": step["label"] if step else None,
            "target_c": step.get("target_c") if step else None,
            "band_c": ctx.spec.get("band_c"),
            "vent_band_c": ctx.spec.get("vent_band_c"),
            "connected": bool(snap.connected),
            "status": snap.status,
            "temp_c": values.get("temp"),
            "setpoint_c": values.get("temp_ref"),
        }
        if kind == "stabilise" and ctx.rt.get("step_started_at"):
            samples = self._samples(ctx, ctx.rt["step_started_at"])
            latest = samples[-1]["t"] if samples else 0.0
            earlier = [s for s in samples if s.get("temp") is not None and s["t"] <= latest - 300]
            if earlier:
                facts["temp_5min_ago_c"] = earlier[-1]["temp"]
        if _finite(values.get("temp_platen")) is not None:
            facts["platen_c"] = round(values["temp_platen"], 2)
        states = ctx.rt.get("tc") or {}
        article = [
            {
                "name": tc["name"],
                "temp_c": _finite(values.get(key)),
                "stabilisation_sensor": bool(tc.get("stabilisation")),
                "status": (states.get(key) or {}).get("status", "ok"),
            }
            for key, tc in self._thermocouples(ctx)
            if key in values
        ]
        if article:
            facts["test_article"] = article
        facts.update(extra or {})
        return facts

    def _pause_clock(self, ctx: Context, now: float) -> None:
        if ctx.rt.get("stabilised_at") and not ctx.rt.get("hold_paused_at"):
            ctx.rt["hold_paused_at"] = now

    def _resume_clock(self, ctx: Context, now: float) -> None:
        paused = ctx.rt.get("hold_paused_at")
        if paused:
            ctx.rt["dwell_paused_s"] = ctx.rt.get("dwell_paused_s", 0.0) + now - paused
            ctx.rt["hold_paused_at"] = None

    def _interrupt(self, ctx: Context, now: float, reason: str) -> None:
        ctx.rt["job_sent_at"] = None
        self._pause_clock(ctx, now)
        step = ctx.step
        ctx.deviations.append(
            {
                "time": cs.now_iso(),
                "kind": "interrupted",
                "where": step["label"] if step else "",
                "text": reason,
            }
        )
        self._hold(
            ctx,
            "interrupted",
            "The campaign's job on the chamber was interrupted. How should it continue?",
            reason,
        )

    def _abort(self, ctx: Context, now: float, why: str) -> None:
        ctx.rt["aborted"] = True
        ctx.state = cs.RUNNING
        cs.record(ctx.id, SUPERVISOR, "Cycling stopped", f"{why}; returning to ambient")
        ambient = next(i for i, s in enumerate(ctx.steps) if s["kind"] == "ambient")
        self._enter(ctx, ambient, now)

    def _complete(self, ctx: Context, now: float) -> None:
        if ctx.rt.get("job_sent_at"):
            self._send_stop(ctx, now, "the campaign's last step is done")
        aborted = ctx.rt.get("aborted")
        ctx.end(
            cs.ABORTED if aborted else cs.COMPLETED,
            "aborted, back at ambient" if aborted else "all steps done",
        )
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Campaign aborted" if aborted else "Campaign completed",
            ctx.fields["end_reason"],
        )

    def _start_dwell(self, ctx: Context, now: float, forced: bool = False) -> None:
        step = ctx.step
        ctx.rt.update(
            stabilised_at=now, min_c=None, max_c=None, out_since=None, forced_dwell=forced
        )
        if forced:
            ctx.deviations.append(
                {
                    "time": cs.now_iso(),
                    "kind": "not stabilised",
                    "where": step["label"],
                    "text": f"The dwell started without the temperature inside ±{ctx.spec['band_c']:g} °C.",
                }
            )
        status = ctx.rt.get("stab_status")
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Dwell started without stabilising" if forced else "Plateau stabilised",
            f"cycle {step['cycle']} {step['plateau']}, target {cs.deg(step['target_c'])}; "
            + (f"{status}; " if status and not forced else "")
            + f"{ctx.spec['dwell_min']:g} min dwell started",
        )

    def _supervise(self, ctx: Context, now: float) -> None:
        rt, spec = ctx.rt, ctx.spec
        snap = self._live(ctx, now)
        if snap is None:
            if rt.get("seen_online") and not rt.get("offline_since"):
                rt["offline_since"] = ctx.wall if ctx.wall is not None else now
                cs.record(
                    ctx.id, SUPERVISOR, "Chamber connection lost", "the PLC keeps its job; waiting"
                )
            return
        rt["seen_online"] = True
        if rt.get("offline_since"):
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Chamber connection back",
                f"after {(ctx.wall if ctx.wall is not None else now) - rt['offline_since']:.0f} s",
            )
            rt["offline_since"] = None
        temp, setpoint = snap.values.get("temp"), snap.values.get("temp_ref")
        if now - rt.get("last_trace_at", 0.0) >= TRACE_S:
            cs.add_trace(ctx.id, now, temp, setpoint)
            rt["last_trace_at"] = now
        if (
            rt.get("job_sent_at")
            and not rt.get("job_confirmed")
            and snap.status == plc_gate.STATUS_NAMES[plc_gate.STATUS_WORK]
            and snap.values_at >= rt.get("job_sent_wall", rt["job_sent_at"])
        ):
            rt["job_confirmed"] = True
        self._renew_watchdog(ctx, now)
        if ctx.state == cs.HOLDING:
            return
        if rt.get("job_sent_at") and not self._job_ok(ctx, snap, now):
            return
        step = ctx.step
        if step is None or temp is None:
            return
        kind = step["kind"]
        self._check_sensors(ctx, step, now)
        if ctx.state == cs.HOLDING:
            return
        if kind in ("ramp", "ambient", "dwell") and self._pressure_rule(ctx, step, temp, now):
            return
        if kind == "pump" and rt.get("pump_mode") == "gauge":
            self._pump_down(ctx, now)
        elif kind == "ramp":
            if rt.get("job_sent_at") is None:
                self._send_setpoint(ctx, step["target_c"], now)
            elif now >= rt["job_sent_at"] + rt.get("job_ramp_s", 0.0) and self._setpoint_at(
                snap, step["target_c"]
            ):
                self._enter(ctx, rt["step_index"] + 1, now)
        elif kind == "dwell":
            self._dwell(ctx, step, temp, now)
        elif kind == "ambient":
            low, high = spec["vent_band_c"]
            ramp_done = rt.get("job_sent_at") and now >= rt["job_sent_at"] + rt.get(
                "job_ramp_s", 0.0
            )
            if ramp_done and low <= temp <= high and abs(temp - step["target_c"]) <= spec["band_c"]:
                cs.record(ctx.id, SUPERVISOR, "Ambient reached", f"the chamber reads {temp:.1f} °C")
                self._enter(ctx, rt["step_index"] + 1, now)
            elif rt.get("job_sent_at") is None:
                self._send_setpoint(ctx, step["target_c"], now)

    def _dwell(self, ctx: Context, step: dict, temp: float, now: float) -> None:
        rt, spec = ctx.rt, ctx.spec
        off = abs(temp - step["target_c"])
        if rt.get("stabilised_at") is None:
            if self._unexcluded_invalid(ctx):
                return
            stable, status, sensors = self._stabilisation(ctx, step, temp, now)
            rt["stab_status"] = status
            if stable:
                self._start_dwell(ctx, now)
            elif now > (rt.get("stabilise_deadline") or now + 1):
                where = (
                    f"the test article reads {status}"
                    if sensors
                    else f"the chamber reads {temp:.1f} °C, {off:.1f} °C from "
                    f"{cs.deg(step['target_c'])}"
                )
                self._hold(
                    ctx,
                    "stabilise",
                    f"The {step['plateau']} plateau hasn't stabilised. How should the campaign continue?",
                    f"After {(now - rt['step_started_at']) / 60:.0f} min {where} "
                    f"(target {cs.deg(step['target_c'])}, band ±{spec['band_c']:g} °C).",
                    {"sensors": sensors} if sensors else None,
                )
            return
        rt["min_c"] = temp if rt.get("min_c") is None else min(rt["min_c"], temp)
        rt["max_c"] = temp if rt.get("max_c") is None else max(rt["max_c"], temp)
        duts = [v for _, v in self._stabilisation_readings(ctx)]
        if duts:
            low, high = min(duts), max(duts)
            rt["dut_min_c"] = low if rt.get("dut_min_c") is None else min(rt["dut_min_c"], low)
            rt["dut_max_c"] = high if rt.get("dut_max_c") is None else max(rt["dut_max_c"], high)
        if off > spec["band_c"] and not rt.get("excursions_accepted"):
            rt["out_since"] = rt.get("out_since") or now
            if now - rt["out_since"] >= spec["excursion_s"]:
                where = f"cycle {step['cycle']} {step['plateau']} plateau"
                ctx.deviations.append(
                    {
                        "time": cs.now_iso(),
                        "kind": "excursion",
                        "where": where,
                        "text": f"The chamber read {temp:.1f} °C, {temp - step['target_c']:+.1f} °C from the "
                        f"target, for {now - rt['out_since']:.0f} s.",
                    }
                )
                cs.record(
                    ctx.id, SUPERVISOR, "Anomaly flagged", f"{ctx.deviations[-1]['text']} ({where})"
                )
                self._pause_clock(ctx, now)
                self._hold(
                    ctx,
                    "anomaly",
                    f"The temperature left the band on the {step['plateau']} plateau. How should the campaign continue?",
                    f"{ctx.deviations[-1]['text']} The plateau is held and the dwell clock is paused.",
                )
                return
        elif off <= spec["band_c"]:
            rt["out_since"] = None
        if self._oscillating(ctx, step, now):
            return
        paused = rt.get("dwell_paused_s", 0.0)
        if rt.get("pressure_paused_at"):
            paused += now - rt["pressure_paused_at"]
        elapsed = now - rt["stabilised_at"] - paused
        if elapsed >= (spec["dwell_min"] + rt.get("dwell_extra_min", 0.0)) * 60.0:
            result = {
                "cycle": step["cycle"],
                "plateau": step["plateau"],
                "target_c": step["target_c"],
                "stabilised_at": cs.iso(rt["stabilised_at"]),
                "dwell_min": round(elapsed / 60.0, 1),
                "min_c": round(rt["min_c"], 2),
                "max_c": round(rt["max_c"], 2),
                "within_band": rt["min_c"] >= step["target_c"] - spec["band_c"]
                and rt["max_c"] <= step["target_c"] + spec["band_c"],
                "forced": bool(rt.get("forced_dwell")),
                "stabilised_after_min": round((rt["stabilised_at"] - rt["step_started_at"]) / 60.0),
            }
            if rt.get("dut_min_c") is not None:
                result["dut_min_c"] = round(rt["dut_min_c"], 2)
                result["dut_max_c"] = round(rt["dut_max_c"], 2)
            ctx.results.append(result)
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Dwell complete",
                f"cycle {step['cycle']} {step['plateau']}, {result['dwell_min']:g} min, "
                f"{result['min_c']:.1f} to {result['max_c']:.1f} °C",
            )
            self._enter(ctx, rt["step_index"] + 1, now)

    def _job_ok(self, ctx: Context, snap, now: float) -> bool:
        """Whether the campaign's job still runs on the chamber."""
        rt = ctx.rt
        working = snap.status == plc_gate.STATUS_NAMES[plc_gate.STATUS_WORK]
        if not rt.get("job_confirmed"):
            sent_wall = rt.get("job_sent_wall", rt["job_sent_at"])
            wall = ctx.wall if ctx.wall is not None else now
            if working and snap.values_at >= sent_wall:
                rt["job_confirmed"] = True
                return True
            if wall - sent_wall > START_TIMEOUT_S:
                self._interrupt(
                    ctx, now, f"The chamber never reported the job running (status {snap.status})."
                )
                return False
            return True
        if snap.status == "alarm":
            labels = control_settings.alarm_labels(control_settings.load())
            active = [
                labels.get(b, f"alarm bit {b}") for b in range(32) if snap.alarm_code & (1 << b)
            ]
            self._interrupt(
                ctx, now, "The chamber is in alarm" + (f": {', '.join(active)}." if active else ".")
            )
            return False
        if not working:
            self._interrupt(
                ctx,
                now,
                f"The chamber's job ended (status {snap.status}) before the campaign's step did.",
            )
            return False
        ramp_end = rt["job_sent_at"] + rt.get("job_ramp_s", 0.0)
        sp = snap.values.get("temp_ref")
        if (
            now > ramp_end + 5
            and sp is not None
            and abs(sp - rt["job_target"]) > SETPOINT_TOLERANCE_C
        ):
            gap = abs(sp - rt["job_target"])
            converging = gap < rt.get("off_setpoint_gap", gap + 1) - 0.05
            rt["off_setpoint_gap"] = gap
            if converging:
                rt["off_setpoint_since"] = None
                return True
            rt["off_setpoint_since"] = rt.get("off_setpoint_since") or now
            rt["off_setpoint_wall"] = rt.get("off_setpoint_wall") or ctx.wall or now
            wall_off = (ctx.wall or now) - rt["off_setpoint_wall"]
            if now - rt["off_setpoint_since"] >= TAKEOVER_S and wall_off >= TAKEOVER_WALL_S:
                self._interrupt(
                    ctx,
                    now,
                    f"The chamber's setpoint is {sp:.1f} °C, not the campaign's {rt['job_target']:.1f} °C: "
                    "another job has taken over.",
                )
                return False
        else:
            rt["off_setpoint_since"] = rt["off_setpoint_wall"] = None
            rt.pop("off_setpoint_gap", None)
        return True

    def _renew_watchdog(self, ctx: Context, now: float) -> None:
        rt = ctx.rt
        sent = rt.get("job_sent_at")
        if not sent or not rt.get("job_confirmed"):
            return
        ends = sent + rt.get("job_ramp_s", 0.0) + rt.get("job_hold_s", 0.0)
        if now > ends - RENEW_BEFORE_S and self._send_setpoint(
            ctx, rt["job_target"], now, ramp=False
        ):
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Watchdog renewed",
                f"the chamber keeps {cs.deg(rt['job_target'])}",
            )

    def _record_pid_table(self, ctx: Context) -> None:
        """Records the temperature PID table the campaign starts with."""
        table = control_settings.temperature_pid(control_settings.load())
        if table is None:
            ctx.rt["pid_table"] = None
            return
        plc = self._snapshot(ctx).settings or {}
        if all(k in plc for k in table["gains"]):
            table["gains"] = {k: float(plc[k]) for k in table["gains"]}
            table["source"] = "read from the PLC when the campaign started"
        else:
            table["source"] = "Deepvac Control's settings"
        ctx.rt["pid_table"] = table
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Operator PID table recorded",
            f"{table['label'] or table['name']}, {table['points']} bands, {table['source']}",
        )

    @staticmethod
    def _setpoint_at(snap, target: float) -> bool:
        """Whether the PLC setpoint has reached the target."""
        sp = snap.values.get("temp_ref") if snap is not None else None
        return sp is None or abs(sp - target) <= SETPOINT_TOLERANCE_C

    def _settings_pending(self, ctx: Context) -> bool:
        """Whether to wait for fresh PLC settings after an oscillation."""
        rt = ctx.rt
        snap = self._snapshot(ctx)
        values = snap.values or {}
        if not rt.get("pid_table") or values.get("temp_kp") is not None or snap.settings_at is None:
            return False
        wall = ctx.wall if ctx.wall is not None else time.time()
        asked = rt.get("osc_settings_asked")
        if asked is None:
            rt["osc_settings_asked"] = wall
            with contextlib.suppress(Exception):
                self._client(ctx).send_text_cmd("get_settings")
            rt["osc_checked_at"] = 0.0
            return True
        if snap.settings_at < asked and wall - asked < SETTINGS_WAIT_S:
            rt["osc_checked_at"] = 0.0
            return True
        rt.pop("osc_settings_asked", None)
        return False

    def _gains(self, ctx: Context, snap, target: float):
        """Returns the PID band, gains in use and operator's gains."""
        table = ctx.rt.get("pid_table")
        values = snap.values or {}
        in_use = None
        if all(values.get(k) is not None for k in ("temp_kp", "temp_ki", "temp_kd")):
            in_use = {"p": values["temp_kp"], "i": values["temp_ki"], "d": values["temp_kd"]}
        if not table:
            return None, in_use, None
        band = control_settings.pid_band(table, target)
        if values.get("temp_pid_idx") is not None:
            band = int(values["temp_pid_idx"])
        band = min(max(band, 0), table["points"] - 1)
        settings = snap.settings or {}
        keys = {k: f"{table['name']}{k}{band}" for k in ("p", "i", "d")}
        if in_use is None and all(key in settings for key in keys.values()):
            in_use = {k: float(settings[key]) for k, key in keys.items()}
        return band + 1, in_use, control_settings.band_gains(table, band)

    def _oscillating(self, ctx: Context, step: dict, now: float) -> bool:
        """Checks the dwell for oscillation and raises the decision point."""
        rt, spec = ctx.rt, ctx.spec
        if (
            rt.get("oscillation_accepted")
            or now - rt.get("osc_checked_at", 0.0) < OSCILLATION_CHECK_S
        ):
            return False
        rt["osc_checked_at"] = now
        client = self._client(ctx)
        if rt.get("pid_table"):
            with contextlib.suppress(Exception):
                client.send_text_cmd("get_settings")
        since = max(rt["stabilised_at"], rt.get("osc_check_from") or 0.0)
        samples = [s for s in self._samples(ctx, since) if s.get("temp") is not None]
        if len(samples) < MIN_OSCILLATION_SAMPLES:
            return False
        times = [s["t"] for s in samples]
        temps = [s["temp"] for s in samples]
        found = anomalies.detect_oscillation(times, temps, [step["target_c"]] * len(temps))
        if not found:
            rt.pop("osc_settings_asked", None)
            return False
        if self._settings_pending(ctx):
            return False
        episode = found[-1]
        inside = [v for t, v in zip(times, temps, strict=True) if episode.start <= t <= episode.end]
        p2p = max(inside) - min(inside)
        crossings = int(episode.value)
        window_min = anomalies.AnomalyOptions().oscillation_window_s / 60.0
        band, in_use, operator = self._gains(ctx, self._snapshot(ctx), step["target_c"])
        gains = ""
        if in_use and operator:
            gains = (
                f" PID band {band} in use: P {in_use['p']:g}, I {in_use['i']:g}, D {in_use['d']:g}; "
                f"the operator's table: P {operator['p']:g}, I {operator['i']:g}, D {operator['d']:g}."
            )
        where = f"cycle {step['cycle']} {step['plateau']} plateau"
        text = (
            f"The chamber swung {p2p:.1f} °C peak to peak around {cs.deg(step['target_c'])}, "
            f"{crossings} setpoint crossings within {window_min:g} min, "
            f"inside the ±{spec['band_c']:g} °C band."
        )
        ctx.deviations.append(
            {"time": cs.now_iso(), "kind": "oscillation", "where": where, "text": text + gains}
        )
        cs.record(ctx.id, SUPERVISOR, "Oscillation flagged", f"{text}{gains} ({where})")
        self._pause_clock(ctx, now)
        self._hold(
            ctx,
            "oscillation",
            f"The chamber oscillates on the {step['plateau']} plateau. "
            "How should the campaign continue?",
            f"{text}{gains} The plateau is held and the dwell clock is paused.",
            {
                "peak_to_peak_c": round(p2p, 2),
                "crossings": crossings,
                "window_min": window_min,
                "pid_band": band,
                "gains_in_use": in_use,
                "operator_gains": operator,
            },
        )
        return True

    def _restore_pid_table(self, ctx: Context, who: str, now: float) -> None:
        """Writes the operator's PID table back to the PLC."""
        if "pid_table" not in ctx.rt:
            self._record_pid_table(ctx)
        table = ctx.rt.get("pid_table")
        if not table:
            cs.record(
                ctx.id,
                who,
                "PID table not restored",
                "No operator PID table is known (none in Deepvac Control's settings): "
                "set it at the chamber.",
            )
            return
        command = {
            "name": "pid_table",
            "values": dict(table["gains"]),
            "decided_by": who,
            "label": "write the operator's PID table",
        }
        if self.gateway(ctx, who, command, now):
            return
        snap = self._snapshot(ctx)
        band, in_use, operator = self._gains(ctx, snap, (ctx.step or {}).get("target_c") or 0.0)
        try:
            client = self._client(ctx)
            client.send_settings_map(command["values"])
            client.send_text_cmd("get_settings")
        except Exception as exc:
            cs.record(ctx.id, who, "Writing the PID table failed", str(exc))
            return
        change = ""
        if in_use and operator:
            changed = [
                f"{k.upper()} {in_use[k]:g} → {operator[k]:g}"
                for k in ("p", "i", "d")
                if abs(in_use[k] - operator[k]) > 1e-6
            ]
            change = f"; band {band}: " + (", ".join(changed) or "unchanged")
        cs.record(
            ctx.id,
            who,
            "PID table restored",
            f"the operator's PID table ({table['source']}) written to the PLC{change}",
        )
        self._decide_deviation(ctx, "oscillation", f"PID table restored by {who}")

    def _decide_deviation(self, ctx: Context, kind: str, decision: str) -> None:
        for deviation in reversed(ctx.deviations):
            if deviation.get("kind") == kind:
                deviation["decision"] = decision
                return

    def _start_pump_down(self, ctx: Context, now: float) -> None:
        """Starts the pump-down on the gauge."""
        spec, rt = ctx.spec, ctx.rt
        unit = self._pressure_unit()
        threshold = cs.pressure_in(spec["vacuum_start_hpa"], unit)
        rt.update(
            pump_mode="gauge",
            pump_threshold=threshold,
            pump_unit=unit,
            pump_deadline=now + spec["pump_timeout_min"] * 60.0,
        )
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Pumping down",
            f"waiting for the gauge to read {_torr(threshold, unit)} or less; it reads "
            f"{_torr(self._pressure(ctx), unit)}. The chamber is held at "
            f"{cs.deg(spec['ambient_c'])} meanwhile.",
        )
        self._send_setpoint(ctx, spec["ambient_c"], now, ramp=False)

    def _pump_down(self, ctx: Context, now: float) -> None:
        rt = ctx.rt
        if rt.get("job_sent_at") is None:
            self._send_setpoint(ctx, ctx.spec["ambient_c"], now, ramp=False)
            return
        pressure = self._pressure(ctx)
        unit, threshold = rt["pump_unit"], rt["pump_threshold"]
        if pressure is not None and pressure <= threshold:
            rt["pump_confirmed"] = True
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Pumped down",
                f"the gauge reads {_torr(pressure, unit)}, at or below {_torr(threshold, unit)}, "
                f"after {(now - rt['step_started_at']) / 60:.0f} min",
            )
            self._enter(ctx, rt["step_index"] + 1, now)
        elif now > rt["pump_deadline"]:
            earlier = [s for s in self._samples(ctx, now - 1800) if s.get("pres")]
            facts = {
                "pressure": to_torr(pressure, unit),
                "pressure_30min_ago": to_torr(earlier[0]["pres"], unit) if earlier else None,
                "pressure_threshold": to_torr(threshold, unit),
                "pressure_unit": "Torr",
            }
            self._hold(
                ctx,
                "pump_slow",
                "The chamber hasn't reached the start pressure. How should the campaign continue?",
                f"After {(now - rt['step_started_at']) / 60:.0f} min the gauge reads "
                f"{_torr(pressure, unit) if pressure else 'nothing'}; cycling starts at "
                f"{_torr(threshold, unit)}.",
                facts,
            )

    def _thermocouples(self, ctx: Context) -> list[tuple[str, dict]]:
        """Returns the test procedure's thermocouples with their states."""
        return [(f"temp{i + 1}", tc) for i, tc in enumerate(ctx.spec.get("thermocouples") or [])]

    def _configure_article(self, ctx: Context) -> None:
        """Sends the test article's thermocouple models to the simulator."""
        tcs = self._thermocouples(ctx)
        if not tcs:
            return
        with contextlib.suppress(Exception):
            client = self._client(ctx)
            for i, (_, tc) in enumerate(tcs, start=1):
                m = tc.get("model")
                if m:
                    client.send_text_cmd(
                        f"sim_dut {i} {m['tau_min']:g} {m['offset_c']:g} {m['gradient_c']:g} "
                        f"{m['platen_weight']:g}"
                    )
                client.send_text_cmd(f"sim_tc {i} ok")

    def _recent(self, ctx: Context, since: float, keys) -> list[dict]:
        """The chamber's samples of `keys` newer than `since` on the PLC's clock, each with "t"."""
        sampler = getattr(self._client(ctx), "plc_samples", None)
        if sampler is not None:
            return [{**s, "t": s["plc_time"]} for s in sampler(since, 4000, list(keys))]
        return self._samples(ctx, since)

    def _sensor_reason(self, ctx: Context, key: str, samples: list[dict], now: float):
        """Returns why a thermocouple is invalid, or None."""
        rule = ctx.spec.get("sensor_rule") or cs.DEFAULT_PACK["sensor_rule"]
        latest = _finite(self._snapshot(ctx).values.get(key))
        if latest is None or not rule["open_low_c"] <= latest <= rule["open_high_c"]:
            reads = "nothing" if latest is None else f"{latest:g} °C"
            return f"reads {reads}: open circuit"
        values = [(s["t"], _finite(s.get(key))) for s in samples]
        values = [(t, v) for t, v in values if v is not None]
        frozen_s = rule["frozen_min"] * 60.0
        window = [(t, v) for t, v in values if t >= now - frozen_s]
        if (
            len(window) >= 5
            and window[-1][0] - window[0][0] >= 0.8 * frozen_s
            and max(v for _, v in window) - min(v for _, v in window) < 1e-6
        ):
            return f"stuck at {latest:.2f} °C for {rule['frozen_min']:g} min: frozen"
        recent = [v for t, v in values if t >= now - rule["spike_window_min"] * 60.0]
        jumps = sum(
            1 for a, b in zip(recent, recent[1:], strict=False) if abs(b - a) > rule["spike_c"]
        )
        if jumps >= rule["spikes"]:
            return (
                f"jumped over {rule['spike_c']:g} °C {jumps} times in "
                f"{rule['spike_window_min']:g} min: spiking"
            )
        return None

    def _check_sensors(self, ctx: Context, step: dict, now: float) -> None:
        """Checks the test article's thermocouples and marks invalid ones."""
        rt = ctx.rt
        tcs = self._thermocouples(ctx)
        values = self._snapshot(ctx).values or {}
        if not tcs or not any(key in values for key, _ in tcs):
            return
        if now - rt.get("tc_checked_at", 0.0) >= SENSOR_CHECK_S:
            rt["tc_checked_at"] = now
            rule = ctx.spec.get("sensor_rule") or cs.DEFAULT_PACK["sensor_rule"]
            span = max(rule["frozen_min"], rule["spike_window_min"]) * 60.0
            samples = self._recent(ctx, now - span, [k for k, _ in tcs])
            states = rt.setdefault("tc", {})
            for key, tc in tcs:
                info = states.setdefault(key, {"name": tc["name"], "status": "ok"})
                status = info["status"]
                if status == "excluded" or (
                    status == "recheck" and now < info.get("recheck_at", 0)
                ):
                    continue
                reason = self._sensor_reason(ctx, key, samples, now)
                if reason is None:
                    if status in ("invalid", "recheck") and rt.get("sensor_hold") != key:
                        info.update(status="ok", reason=None)
                        cs.record(
                            ctx.id,
                            SUPERVISOR,
                            "Thermocouple valid again",
                            f"{tc['name']} ({key}) reads {values.get(key):.1f} °C",
                        )
                    continue
                if status == "invalid":
                    continue
                info.update(status="invalid", reason=reason, since=cs.now_iso())
                text = f"{tc['name']} ({key}) {reason}; marked invalid."
                deviation = {
                    "time": cs.now_iso(),
                    "kind": "sensor",
                    "sensor": key,
                    "where": step.get("label", ""),
                    "text": text,
                }
                critical = bool(tc.get("stabilisation"))
                if not critical:
                    deviation["decision"] = "not a stabilisation sensor: left out of the results"
                ctx.deviations.append(deviation)
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Thermocouple invalid",
                    text
                    + (
                        ""
                        if critical
                        else " Not a stabilisation sensor: left out, the campaign continues."
                    ),
                )
        if step["kind"] in CYCLING:
            self._unexcluded_invalid(ctx, now)

    def _unexcluded_invalid(self, ctx: Context, now: float | None = None) -> bool:
        """Whether a stabilisation sensor is invalid and not excluded."""
        rt = ctx.rt
        states = rt.get("tc") or {}
        tcs = self._thermocouples(ctx)
        for key, tc in tcs:
            info = states.get(key)
            if not (tc.get("stabilisation") and info and info["status"] == "invalid"):
                continue
            if ctx.state != cs.HOLDING and cs.open_hold_of(ctx.id) is None:
                others = [
                    t["name"]
                    for k, t in tcs
                    if k != key
                    and t.get("stabilisation")
                    and (states.get(k) or {}).get("status", "ok") == "ok"
                ]
                rt["sensor_hold"] = key
                dwelling = bool(rt.get("stabilised_at"))
                self._pause_clock(ctx, now if now is not None else time.time())
                without = (
                    f"Without it the criterion uses {cs._and(others)}."
                    if others
                    else "Without it no stabilisation sensor is left: the criterion uses the "
                    "article's other valid thermocouples."
                )
                self._hold(
                    ctx,
                    "sensor",
                    f"{tc['name']} is invalid. Exclude it from the stabilisation criterion?",
                    f"{tc['name']} ({key}) {info['reason']}. It is one of the procedure's "
                    f"stabilisation sensors. {without}"
                    + (" The plateau is held and the dwell clock is paused." if dwelling else ""),
                    {
                        "sensor": tc["name"],
                        "reason": info["reason"],
                        "remaining_stabilisation_sensors": others,
                    },
                )
            return True
        return False

    def _decide_deviation_of(self, ctx: Context, sensor: str, decision: str) -> None:
        for deviation in reversed(ctx.deviations):
            if deviation.get("sensor") == sensor:
                deviation["decision"] = decision
                return

    def _criterion_sensors(self, ctx: Context) -> list[tuple[str, dict]]:
        """Returns the thermocouples used for the stabilisation criterion."""
        states = ctx.rt.get("tc") or {}
        values = self._snapshot(ctx).values or {}
        valid = [
            (k, tc)
            for k, tc in self._thermocouples(ctx)
            if k in values and (states.get(k) or {}).get("status", "ok") == "ok"
        ]
        return [(k, tc) for k, tc in valid if tc.get("stabilisation")] or valid

    def _stabilisation_readings(self, ctx: Context) -> list[tuple[str, float]]:
        values = self._snapshot(ctx).values or {}
        out = []
        for key, tc in self._criterion_sensors(ctx):
            value = _finite(values.get(key))
            if value is not None:
                out.append((tc["name"], value))
        return out

    def _stabilisation(self, ctx: Context, step: dict, temp: float, now: float):
        """Returns whether the plateau is stable, a status text and the sensor readings."""
        spec = ctx.spec
        band, target = spec["band_c"], step["target_c"]
        sensors = self._criterion_sensors(ctx)
        if not sensors:
            return abs(temp - target) <= band, "", []
        stab = spec.get("stabilisation") or cs.DEFAULT_PACK["stabilisation"]
        window = stab["window_min"] * 60.0
        samples = self._recent(ctx, now - window - 120.0, [k for k, _ in sensors])
        stable, out, parts = True, [], []
        for key, tc in sensors:
            series = [(s["t"], _finite(s.get(key))) for s in samples]
            series = [(t, v) for t, v in series if v is not None]
            latest = _mean(v for _, v in series[-3:])
            if latest is None:
                stable = False
                continue
            rate = None
            past = [(t, v) for t, v in series if t <= now - window]
            if past and series[-1][0] - past[-1][0] > 60.0:
                earlier = _mean(v for _, v in past[-3:])
                rate = (latest - earlier) / (series[-1][0] - past[-1][0]) * 3600.0
            inside = abs(latest - target) <= band
            steady = rate is not None and abs(rate) <= stab["rate_c_per_h"]
            stable = stable and inside and steady
            out.append(
                {
                    "name": tc["name"],
                    "temp_c": round(latest, 2),
                    "rate_c_per_h": None if rate is None else round(rate, 1),
                    "inside_band": inside,
                }
            )
            parts.append(
                f"{tc['name']} {latest:.1f} °C"
                + (f" ({rate:+.1f} °C/h)" if rate is not None else "")
            )
        return stable and bool(out), ", ".join(parts), out

    def _pressure_rule(self, ctx: Context, step: dict, temp: float, now: float) -> bool:
        """Applies the pressure rule; returns True while the step waits."""
        rt, spec = ctx.rt, ctx.spec
        rule = spec.get("pressure_rule")
        if not rule or spec["vacuum"] != "auto" or not rt.get("pump_confirmed"):
            return False
        held = rt.get("pressure_hold")
        pressure = self._pressure(ctx)
        if pressure is None:
            return bool(held and held["kind"] == "ramp")
        unit = self._pressure_unit()
        hold_at = cs.pressure_in(rule["hold_hpa"], unit)
        resume_at = cs.pressure_in(rule["resume_hpa"], unit)
        if held is None:
            if pressure <= hold_at or now < rt.get("pressure_override_until", 0.0):
                return False
            if step["kind"] == "dwell":
                if not rt.get("stabilised_at"):
                    return False
                rt["pressure_hold"] = {
                    "kind": "dwell",
                    "since": now,
                    "temp_c": round(temp, 1),
                    "pressure": pressure,
                }
                rt["pressure_paused_at"] = now
                cs.record(
                    ctx.id,
                    SUPERVISOR,
                    "Dwell clock paused (pressure rule)",
                    f"the gauge reads {_torr(pressure, unit)}, above the procedure's "
                    f"{_torr(hold_at, unit)}; the clock runs again at {_torr(resume_at, unit)}",
                )
                return False
            if step["target_c"] <= temp + 0.5:
                return False
            held_at = round(temp, 1)
            if not self._send_setpoint(ctx, held_at, now, hold=True):
                return True
            rt["pressure_hold"] = {
                "kind": "ramp",
                "since": now,
                "temp_c": held_at,
                "pressure": pressure,
            }
            ctx.deviations.append(
                {
                    "time": cs.now_iso(),
                    "kind": "pressure",
                    "where": step.get("label", ""),
                    "text": f"{_torr(pressure, unit)} while heating, over the "
                    f"{_torr(hold_at, unit)} limit: ramp held at {cs.deg(held_at)}.",
                    "decision": "held by the approved pressure rule",
                }
            )
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Ramp held (pressure rule)",
                f"the gauge reads {_torr(pressure, unit)}, above the procedure's "
                f"{_torr(hold_at, unit)}: the chamber holds {cs.deg(held_at)} until it reads "
                f"{_torr(resume_at, unit)}",
            )
            return True
        held["pressure"] = pressure
        if pressure <= resume_at:
            self._end_pressure_hold(
                ctx,
                now,
                f"the gauge reads {_torr(pressure, unit)}, at or below {_torr(resume_at, unit)}",
            )
            return held["kind"] == "ramp"
        minutes = (now - held["since"]) / 60.0
        if not held.get("asked") and minutes >= rule["max_hold_min"]:
            held["asked"] = True
            self._hold(
                ctx,
                "pressure",
                "The pressure stays above the limit. How should the campaign continue?",
                f"For {minutes:.0f} min the gauge has read above {_torr(resume_at, unit)}: "
                f"{_torr(pressure, unit)} now (hold limit {_torr(hold_at, unit)}). "
                + (
                    f"The chamber holds {cs.deg(held['temp_c'])}."
                    if held["kind"] == "ramp"
                    else "The dwell clock is paused."
                ),
                {
                    "pressure": to_torr(pressure, unit),
                    "hold_limit": to_torr(hold_at, unit),
                    "resume_limit": to_torr(resume_at, unit),
                    "pressure_unit": "Torr",
                    "held_min": round(minutes),
                    "while": held["kind"],
                },
            )
            return True
        return held["kind"] == "ramp"

    def _end_pressure_hold(self, ctx: Context, now: float, why: str) -> None:
        rt = ctx.rt
        held = rt.get("pressure_hold")
        rt["pressure_hold"] = None
        if not held:
            return
        minutes = (now - held["since"]) / 60.0
        if held["kind"] == "dwell":
            if rt.get("pressure_paused_at"):
                rt["dwell_paused_s"] = (
                    rt.get("dwell_paused_s", 0.0) + now - rt["pressure_paused_at"]
                )
            rt["pressure_paused_at"] = None
            cs.record(
                ctx.id,
                SUPERVISOR,
                "Dwell clock running again (pressure rule)",
                f"{why}, after {minutes:.0f} min",
            )
            return
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Ramp resumed (pressure rule)",
            f"{why}, after {minutes:.0f} min held at {cs.deg(held['temp_c'])}",
        )
        step = ctx.step
        if step and step.get("target_c") is not None:
            self._send_setpoint(ctx, step["target_c"], now)
