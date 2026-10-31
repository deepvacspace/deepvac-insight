"""Run supervisor: runs approved test campaigns on their chamber step by step,
applies the operator's decisions at hold points and checks every command
against the gateway rules."""

import json
import logging
import threading
import time

from app.api import control_settings
from app.api.chambers import ChamberLinks
from app.services import campaign_service as cs
from app.services import plc_gate

log = logging.getLogger(__name__)

SUPERVISOR = "Run supervisor"
GATEWAY = "Gateway"
TRACE_S = 15.0
STALE_S = 15.0
START_TIMEOUT_S = 60.0
WATCHDOG_MARGIN_MIN = 120.0
RENEW_BEFORE_S = 30 * 60.0
SETPOINT_TOLERANCE_C = 0.5
TAKEOVER_S = 30.0


class Context:
    """One campaign during one supervisor pass; saved at the end of it."""

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

    def __init__(self, links: ChamberLinks):
        self.links = links
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

    def tick(self, campaign: dict, now: float) -> None:
        ctx = Context(campaign)
        if campaign.get("stop_request") and ctx.state in (cs.RUNNING, cs.HOLDING):
            self._stop_now(ctx, json.loads(campaign["stop_request"]), now)
        else:
            for hold in cs.decided_unapplied(ctx.id):
                cs.mark_applied(hold["id"])
                if ctx.state in cs.ACTIVE:
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

    def _live(self, ctx: Context, now: float):
        """The snapshot when the chamber is connected with fresh values, else None."""
        snap = self._snapshot(ctx)
        if snap.connected and snap.values_at and now - snap.values_at <= STALE_S:
            return snap
        return None

    def gateway(self, ctx: Context, actor: str, command: dict, now: float) -> list[str]:
        """Reasons the command is refused; empty when it may be sent."""
        reasons = []
        name = command["name"]
        spec = ctx.spec
        if actor != SUPERVISOR:
            reasons.append("Only the run supervisor may send commands to the chamber.")
        if ctx.state not in (cs.RUNNING, cs.HOLDING):
            reasons.append("The campaign is not approved and running.")
        snap = self._live(ctx, now)
        if name in ("setpoint", "stop") and snap is None:
            reasons.append("The chamber is not connected.")
        if name == "setpoint":
            if snap is not None and snap.status == "alarm":
                reasons.append("The chamber is in alarm: acknowledge it at the chamber first.")
            target = float(command["target_c"])
            allowed = (spec["hot_c"], spec["cold_c"], spec["ambient_c"])
            if not any(abs(target - a) < 1e-6 for a in allowed):
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
                and spec["vacuum"] == "manual"
                and not ctx.rt.get("pump_confirmed")
            ):
                reasons.append("Thermal cycling needs the pump-down confirmed by the operator.")
        if name == "vent":
            if not command.get("approved"):
                reasons.append("Venting needs the operator's approval at a hold point.")
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
        self, ctx: Context, target: float, now: float, ramp: bool = True, start_cycling=False
    ) -> bool:
        spec = ctx.spec
        snap = self._live(ctx, now)
        current = snap.values.get("temp") if snap is not None else None
        rate = spec["ramp_c_per_min"]
        ramp_s = abs(current - target) / rate * 60.0 if ramp and current is not None else 0.0
        label = f"set {cs.deg(target)}" + (f" ramping at {rate:g} °C/min" if ramp_s >= 1 else "")
        command = {
            "name": "setpoint",
            "target_c": target,
            "ramp_c_per_min": rate if ramp_s >= 1 else None,
            "start_cycling": start_cycling,
            "label": label,
        }
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
                self._enter(ctx, 0, now)
            else:
                ctx.end(cs.DECLINED, f"declined by {who}")
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
            forced_dwell=False,
            stabilise_deadline=None,
        )
        if index >= len(ctx.steps):
            self._complete(ctx, now)
            return
        step = ctx.steps[index]
        cs.record(
            ctx.id, SUPERVISOR, "Step started", f"{index + 1}/{len(ctx.steps)}: {step['label']}"
        )
        kind = step["kind"]
        if kind == "pump":
            self._hold(
                ctx,
                "pump",
                "Pump the chamber down and confirm.",
                "DeepVac has no vacuum gauge connected: confirm when the chamber has reached the start pressure.",
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

    def _hold(self, ctx: Context, kind: str, title: str, detail: str) -> None:
        if cs.open_hold_of(ctx.id) is not None:
            return
        cs.open_hold(ctx.id, kind, title, detail)
        ctx.state = cs.HOLDING

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
        cs.record(
            ctx.id,
            SUPERVISOR,
            "Dwell started without stabilising" if forced else "Plateau stabilised",
            f"cycle {step['cycle']} {step['plateau']}, target {cs.deg(step['target_c'])}; "
            f"{ctx.spec['dwell_min']:g} min dwell started",
        )

    def _supervise(self, ctx: Context, now: float) -> None:
        rt, spec = ctx.rt, ctx.spec
        snap = self._live(ctx, now)
        if snap is None:
            if rt.get("seen_online") and not rt.get("offline_since"):
                rt["offline_since"] = now
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
                f"after {now - rt['offline_since']:.0f} s",
            )
            rt["offline_since"] = None
        temp, setpoint = snap.values.get("temp"), snap.values.get("temp_ref")
        if now - rt.get("last_trace_at", 0.0) >= TRACE_S:
            cs.add_trace(ctx.id, now, temp, setpoint)
            rt["last_trace_at"] = now
        self._renew_watchdog(ctx, now)
        if ctx.state == cs.HOLDING:
            return
        if rt.get("job_sent_at") and not self._job_ok(ctx, snap, now):
            return
        step = ctx.step
        if step is None or temp is None:
            return
        kind = step["kind"]
        if kind == "ramp":
            if rt.get("job_sent_at") is None:
                self._send_setpoint(ctx, step["target_c"], now)
            elif now >= rt["job_sent_at"] + rt.get("job_ramp_s", 0.0):
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
            if off <= spec["band_c"]:
                self._start_dwell(ctx, now)
            elif now > (rt.get("stabilise_deadline") or now + 1):
                self._hold(
                    ctx,
                    "stabilise",
                    f"The {step['plateau']} plateau hasn't stabilised. How should the campaign continue?",
                    f"After {(now - rt['step_started_at']) / 60:.0f} min the chamber reads {temp:.1f} °C, "
                    f"{off:.1f} °C from {cs.deg(step['target_c'])} (band ±{spec['band_c']:g} °C).",
                )
            return
        rt["min_c"] = temp if rt.get("min_c") is None else min(rt["min_c"], temp)
        rt["max_c"] = temp if rt.get("max_c") is None else max(rt["max_c"], temp)
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
        paused = rt.get("dwell_paused_s", 0.0)
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
            }
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
        """Whether the campaign's job still runs on the chamber; opens the interrupted hold if not."""
        rt = ctx.rt
        working = snap.status == plc_gate.STATUS_NAMES[plc_gate.STATUS_WORK]
        if not rt.get("job_confirmed"):
            if working and snap.values_at >= rt["job_sent_at"]:
                rt["job_confirmed"] = True
                return True
            if now - rt["job_sent_at"] > START_TIMEOUT_S:
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
            rt["off_setpoint_since"] = rt.get("off_setpoint_since") or now
            if now - rt["off_setpoint_since"] >= TAKEOVER_S:
                self._interrupt(
                    ctx,
                    now,
                    f"The chamber's setpoint is {sp:.1f} °C, not the campaign's {rt['job_target']:.1f} °C: "
                    "another job has taken over.",
                )
                return False
        else:
            rt["off_setpoint_since"] = None
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
