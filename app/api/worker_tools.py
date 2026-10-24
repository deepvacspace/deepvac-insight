"""Worker tools: the chamber now, and requests to change it.

Reading (live status, PLC settings, programs) goes straight to the PLC.
Every action -- a setpoint, a program, a stop -- only creates a request in
agent_requests_service; it reaches the chamber after a person approves it in
the app (see executor.py). Limits are checked here before a request exists.
"""

import time

from app.api import control_settings
from app.api.analyst_tools import n
from app.api.chambers import ChamberError, ChamberLinks
from app.services import agent_requests_service as requests_service
from app.services import plc_gate, plc_programs, test_profiles_service

STALE_AFTER_S = 15.0  # live values older than this are reported as stale
MAX_RAMP_C_PER_MIN = 20.0
MAX_HOLD_MIN = 7 * 24 * 60


class WorkerTools:
    def __init__(self, links: ChamberLinks | None = None, requested_by: str = "worker agent"):
        self.links = links or ChamberLinks()
        self.requested_by = requested_by

    # ------------------------------------------------------------ reading

    def list_chambers(self) -> dict:
        return {
            "chambers": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "protocol": c["protocol"],
                    "address": f"{c['host']}:{c['port']}",
                    "controllable": c["protocol"] == "plc",
                }
                for c in self.links.chambers()
            ]
        }

    def get_live_status(self, chamber: str = "") -> dict:
        ch = self.links.resolve(chamber)
        snap = self.links.client(ch).snapshot()
        result = {"chamber": ch["name"], "connected": snap.connected}
        if not snap.connected or not snap.values:
            result["note"] = f"No live values from the PLC at {snap.address}" + (
                f": {snap.last_error}" if snap.last_error else "."
            )
            return result
        v = snap.values
        age = time.time() - snap.values_at if snap.values_at else None
        output = v.get("temp_u")
        mode = None
        if output is not None:
            mode = "heating" if output > 1.0 else ("cooling" if output < -1.0 else "idle")
        result.update(
            {
                "status": snap.status,
                "temperature_c": n(v.get("temp"), 4),
                "setpoint_c": n(v.get("temp_ref"), 4),
                "control_output_percent": n(output, 3),
                "mode": mode,
                "program_interval": f"{int(v['interval'])}/{int(v.get('intervals_count', 0))}"
                if v.get("interval")
                else None,
                "cycle": f"{int(v['cycle_cur'])}/{int(v.get('cycle_total', 0))}"
                if v.get("cycle_total")
                else None,
                "pid": {
                    "kp": n(v.get("temp_kp")),
                    "ki": n(v.get("temp_ki")),
                    "kd": n(v.get("temp_kd")),
                },
                "values_age_s": n(age, 3),
            }
        )
        if age is not None and age > STALE_AFTER_S:
            result["warning"] = f"Live values are {age:.0f} s old."
        if snap.alarm_code:
            labels = control_settings.alarm_labels(control_settings.load())
            bits = [
                labels.get(b, f"alarm bit {b}") for b in range(32) if snap.alarm_code & (1 << b)
            ]
            result["alarm"] = {"code": snap.alarm_code, "active": bits}
        if snap.user_messages:
            result["last_plc_message"] = snap.user_messages[-1][1]
        return result

    def get_settings(self, chamber: str = "") -> dict:
        ch = self.links.resolve(chamber)
        snap = self.links.client(ch).snapshot()
        if not snap.settings:
            return {"chamber": ch["name"], "note": "The PLC has not sent its settings yet."}
        labels = control_settings.setting_labels(control_settings.load())
        return {
            "chamber": ch["name"],
            "settings": {
                name: {"value": n(value), "label": labels.get(name, "")}
                for name, value in sorted(snap.settings.items())
            },
        }

    def list_programs(self) -> dict:
        enable = control_settings.enable_flags(control_settings.load())
        programs = []
        for p in test_profiles_service.list_profiles():
            item = {"name": p["name"], "description": p["description"]}
            try:
                intervals, cycle = plc_programs.program_for_profile(p, enable)
            except plc_programs.ProgramError as exc:
                item.update({"runnable": False, "note": str(exc)})
            else:
                item.update(
                    {
                        "intervals": len(intervals),
                        "setpoints_c": sorted(
                            {i.get("temp") for i in intervals if i.get("temp") is not None}
                        ),
                        "duration_h": n(
                            plc_programs.total_duration_s(intervals, cycle) / 3600.0, 4
                        ),
                        "cycles": int(cycle.get("count", 0)),
                        "runnable": True,
                    }
                )
            programs.append(item)
        return {"programs": programs}

    # ------------------------------------------------------------ requests

    def request_setpoint(
        self,
        temperature_c: float,
        reason: str,
        chamber: str = "",
        ramp_rate_c_per_min: float | None = None,
        hold_minutes: float | None = None,
    ) -> dict:
        ch = self.links.resolve(chamber)
        settings = control_settings.load()
        lo, hi = control_settings.temperature_limits(settings)
        if not lo <= temperature_c <= hi:
            raise ChamberError(
                f"{temperature_c} °C is outside the chamber's setpoint range {lo:g} … {hi:g} °C."
            )
        if ramp_rate_c_per_min is not None and not 0 < ramp_rate_c_per_min <= MAX_RAMP_C_PER_MIN:
            raise ChamberError(
                f"The ramp rate must be between 0 and {MAX_RAMP_C_PER_MIN:g} °C/min."
            )
        if hold_minutes is not None and not 0 < hold_minutes <= MAX_HOLD_MIN:
            raise ChamberError(f"The hold time must be between 0 and {MAX_HOLD_MIN} minutes.")
        if not reason or not reason.strip():
            raise ChamberError("Give the reason: the operator sees it when deciding.")
        parts = [f"Set {ch['name']} to {temperature_c:g} °C"]
        if ramp_rate_c_per_min:
            parts.append(f"ramping at {ramp_rate_c_per_min:g} °C/min")
        parts.append(f"hold {hold_minutes:g} min" if hold_minutes else "hold until stopped")
        params = {
            "temperature_c": temperature_c,
            "ramp_rate_c_per_min": ramp_rate_c_per_min,
            "hold_minutes": hold_minutes,
        }
        return self._request("setpoint", ch, params, ", ".join(parts), reason)

    def request_program(self, program: str, reason: str, chamber: str = "") -> dict:
        ch = self.links.resolve(chamber)
        profile = next(
            (
                p
                for p in test_profiles_service.list_profiles()
                if p["name"].lower() == program.strip().lower()
            ),
            None,
        )
        if profile is None:
            raise ChamberError(f"No program named '{program}'. Use list_programs.")
        settings = control_settings.load()
        try:
            intervals, _ = plc_programs.program_for_profile(
                profile, control_settings.enable_flags(settings)
            )
            plc_programs.check_limits(intervals, *control_settings.temperature_limits(settings))
        except plc_programs.ProgramError as exc:
            raise ChamberError(str(exc)) from exc
        if not reason or not reason.strip():
            raise ChamberError("Give the reason: the operator sees it when deciding.")
        summary = f"Run program '{profile['name']}' on {ch['name']}"
        return self._request("program", ch, {"program": profile["name"]}, summary, reason)

    def request_stop(self, reason: str, chamber: str = "") -> dict:
        ch = self.links.resolve(chamber)
        if not reason or not reason.strip():
            raise ChamberError("Give the reason: the operator sees it when deciding.")
        return self._request("stop", ch, {}, f"Stop the running job on {ch['name']}", reason)

    def get_request(self, request_id: int) -> dict:
        requests_service.expire_stale()
        request = requests_service.get_request(request_id)
        if request is None:
            raise ChamberError(f"No request {request_id}.")
        return self._public(request)

    def list_requests(self, status: str = "", limit: int = 10) -> dict:
        requests_service.expire_stale()
        if status and status not in requests_service.STATUSES:
            raise ChamberError(f"status must be one of {', '.join(requests_service.STATUSES)}")
        return {
            "requests": [
                self._public(r)
                for r in requests_service.list_requests(status or None, min(int(limit), 50))
            ]
        }

    def _request(self, kind, chamber, params, summary, reason) -> dict:
        request = requests_service.create_request(
            kind, chamber, params, summary, reason, self.requested_by
        )
        public = self._public(request)
        public["next"] = (
            "Nothing has been sent to the chamber. An operator must approve this request in DeepVac "
            "Insight (Agent Requests); check it with get_request."
        )
        return public

    @staticmethod
    def _public(r: dict) -> dict:
        return {
            key: r[key]
            for key in (
                "id",
                "status",
                "summary",
                "reason",
                "created_at",
                "decided_by",
                "decision_comment",
                "executed_at",
                "result",
            )
            if r.get(key) not in (None, "")
        }


def execute_request(links: ChamberLinks, request: dict) -> str:
    """Sends one approved request to its chamber's PLC; returns what was sent. Raises on failure."""
    chamber = links.resolve(request["chamber_id"])
    client = links.client(chamber)
    settings = control_settings.load()
    params_spec = control_settings.control_params(settings)
    p = request["params"]
    if request["kind"] == "stop":
        client.send_text_cmd("stop")
        return "stop sent"
    if request["kind"] == "setpoint":
        snap = client.snapshot()
        current = snap.values.get("temp")
        intervals = plc_gate.manual_setpoint_intervals(
            p["temperature_c"],
            current,
            control_settings.enable_flags(settings),
            p.get("ramp_rate_c_per_min"),
            p["hold_minutes"] * 60.0 if p.get("hold_minutes") else None,
        )
        client.send_job(plc_gate.PROG_TYPE_SETPOINT, intervals, params_spec)
        return f"setpoint job sent ({len(intervals)} interval(s))"
    if request["kind"] == "program":
        profile = next(
            (x for x in test_profiles_service.list_profiles() if x["name"] == p["program"]), None
        )
        if profile is None:
            raise ChamberError(f"Program '{p['program']}' is gone.")
        intervals, cycle = plc_programs.program_for_profile(
            profile, control_settings.enable_flags(settings)
        )
        client.send_job(plc_gate.PROG_TYPE_PROG, intervals, params_spec, cycle)
        return f"program job sent ({len(intervals)} intervals)"
    raise ChamberError(f"Unknown request kind {request['kind']}")
