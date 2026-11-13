"""Bridge between the AI Agents page and Insight."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtGui import QDesktopServices

from app.api import control_settings
from app.services import ai_mode, campaign_report, chambers_service
from app.services import campaign_service as cs
from app.services.pressure import chamber_unit, to_torr

log = logging.getLogger(__name__)

CHART_POINTS = 480
CHART_KEYS = (
    "temp",
    "temp_ref",
    "pres",
    "pres_ref",
    "temp1",
    "temp2",
    "temp3",
    "temp4",
    "temp_platen",
    "status_condition",
)
TC_RANGE_C = (-200.0, 400.0)
LOG_TAIL = 40
ROGUE = {
    "analyst_output": (
        "Analyst",
        {"name": "force_output", "label": "force the heater output on"},
    ),
    "worker_vent_cold": (
        "Worker",
        {"name": "vent", "label": "vent the chamber now"},
    ),
    "worker_ramp_fast": (
        "Worker",
        {"name": "setpoint", "target_c": None, "ramp_c_per_min": 10.0, "label": None},
    ),
    "worker_no_vacuum": (
        "Worker",
        {
            "name": "setpoint",
            "target_c": None,
            "start_cycling": True,
            "skip_vacuum": True,
            "label": "start cycling without the pump-down",
        },
    ),
    "analyst_pid": (
        "Analyst",
        {
            "name": "pid_table",
            "values": {"p1p3": 5.0, "p1i3": 200.0},
            "label": "change the PID gains of band 4",
        },
    ),
    "worker_out_of_range": (
        "Worker",
        {"name": "setpoint", "target_c": 150.0, "label": "set +150 °C"},
    ),
    "planner_self_approve": (
        "Planner",
        {"name": "decide", "label": "approve its own plan at the decision point"},
    ),
    "worker_alarm": (
        "Worker",
        {"name": "settings", "values": {"alarm_cfg_1": 0}, "label": "turn off alarm alarm_cfg_1"},
    ),
    "worker_job": (
        "Worker",
        {"name": "setpoint", "target_c": 80.0, "label": "set +80 °C"},
    ),
    "operator_alarm": (
        None,
        {"name": "settings", "values": {"alarm_cfg_2": 0}, "label": "edit alarm alarm_cfg_2"},
    ),
}


def _finite(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _tc(value):
    """Returns a thermocouple reading, or None when out of range."""
    value = _finite(value)
    return value if value is not None and TC_RANGE_C[0] <= value <= TC_RANGE_C[1] else None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class DemoBridge(QObject):
    """Slots for the page."""

    def __init__(self, host=None, links=None, user_name=None, parent=None):
        super().__init__(parent)
        self._host = host
        self._links = links
        self._user = user_name
        self._writing = set()

    def links(self):
        return self._links or getattr(self._host, "plc_links", None)

    def user(self) -> str:
        if self._user:
            return self._user
        user = getattr(self._host, "current_user", None) or {}
        return user.get("name") or ""

    def supervisor(self):
        api = getattr(self._host, "agent_api", None)
        return getattr(api, "supervisor", None) if api is not None else None

    def _chamber(self, chamber_id):
        links = self.links()
        if links is None:
            raise cs.CampaignError("No chamber connections.")
        return links.resolve(int(chamber_id))

    def _client(self, chamber_id):
        return self.links().client(self._chamber(chamber_id), wait=False)

    @staticmethod
    def _guard(fn, *args):
        """Runs a slot and returns its result or error as JSON."""
        try:
            result = fn(*args)
            return _json({"ok": True, **(result or {})})
        except (cs.CampaignError, campaign_report.ReportError, ValueError, OSError) as exc:
            return _json({"ok": False, "error": str(exc)})
        except Exception as exc:
            log.exception("AI Agents page")
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    @Slot(result=str)
    def catalog(self):
        pack = cs.load_pack()
        return _json(
            {
                "articles": pack.get("articles") or [],
                "levels": list(pack["levels"]),
                "level_values": pack["levels"],
                "ramp_margin_c": (pack.get("single_ramp") or {}).get("margin_c", 10.0),
                "test_types": list(cs.TEST_TYPES),
                "user": self.user(),
                "mode": ai_mode.mode(),
            }
        )

    @Slot(int, int, result=str)
    def state(self, chamber_id, campaign_id=0):
        """Returns the chamber and campaign state."""
        try:
            return self._state(chamber_id, campaign_id)
        except Exception as exc:
            log.exception("AI Agents page state")
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    @Slot(str)
    def jsError(self, text):  # noqa: N802
        """An error in the page's script, into Insight's log."""
        log.warning("AI Agents page script: %s", text[:2000])

    @Slot(result=str)
    def campaigns(self):
        """Every campaign, newest first, for the page's history."""
        return _json(
            [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "chamber": c["chamber_name"],
                    "chamber_id": c["chamber_id"],
                    "state": c["state"],
                    "created_at": c["created_at"],
                    "report_issued_at": c.get("report_issued_at"),
                    "signed_off_by": c.get("signed_off_by"),
                }
                for c in cs.list_campaigns(200)
            ]
        )

    def _state(self, chamber_id, campaign_id=0):
        chambers = [c for c in chambers_service.list_chambers() if c.get("protocol") == "plc"]
        demo = getattr(self._host, "demo", None)
        if not chamber_id and demo is not None and demo.chamber_id:
            chamber_id = demo.chamber_id
        out = {
            "demo": demo.status() if demo is not None else None,
            "auto_play": bool(getattr(getattr(self._host, "autoplay", None), "on", False)),
            "mode": ai_mode.mode(),
            "local_model": getattr(getattr(self._host, "local_model", None), "status", ""),
            "user": self.user(),
            "agent_api": self.supervisor() is not None,
            "chambers": [{"id": c["id"], "name": c["name"]} for c in chambers],
            "chamber": None,
            "campaign": None,
            "series": None,
        }
        if not chambers:
            return _json(out)
        if not any(c["id"] == chamber_id for c in chambers):
            chamber_id = chambers[0]["id"]
        out["chamber_id"] = chamber_id
        client = self._client(chamber_id)
        snap = client.snapshot()
        values = snap.values or {}
        live = snap.connected and snap.values_at and time.time() - snap.values_at <= 15
        unit = chamber_unit()
        out["chamber"] = {
            "connected": bool(live),
            "status": snap.status if live else "offline",
            "temp": _finite(values.get("temp")),
            "setpoint": _finite(values.get("temp_ref")),
            "pres": _finite(to_torr(_finite(values.get("pres")), unit)),
            "pres_unit": "Torr",
            "duts": [_tc(values.get(f"temp{i}")) for i in range(1, 5)],
            "platen": _finite(values.get("temp_platen")),
            "rate": round(snap.plc_rate or 1.0, 1),
            "simulated": (snap.plc_rate or 1.0) > 1.5,
        }
        current = self._campaign_on(chamber_id)
        campaign = (cs.get_campaign(campaign_id) if campaign_id else None) or current
        start = None
        if campaign is not None:
            out["campaign"] = self._campaign(campaign)
            is_current = current is not None and campaign["id"] == current["id"]
            out["campaign"]["current"] = is_current
            rt = campaign["runtime"] or {}
            start = rt.get("started_plc")
            if not is_current or campaign["state"] in cs.ENDED:
                out["series"] = self._trace_series(cs.trace(campaign["id"]), start)
                return _json(out)
            if start is None:
                trace = cs.trace(campaign["id"])
                start = trace[0]["t"] if trace else None
        now = snap.plc_time or time.time()
        if start is None or start > now:
            start = now - 2 * 3600
        series = self._series(client, start)
        if series is not None and to_torr(1.0, unit) != 1.0:
            factor = to_torr(1.0, unit)
            for key in ("pres", "pres_setpoint"):
                series[key] = [None if v is None else v * factor for v in series[key]]
        out["series"] = series
        return _json(out)

    def _campaign_on(self, chamber_id):
        mine = [c for c in cs.list_campaigns(30) if c["chamber_id"] == chamber_id]
        active = [c for c in mine if c["state"] in cs.ACTIVE or c["state"] in (cs.DRAFT,)]
        return (active or mine or [None])[0]

    def _campaign(self, campaign):
        cid = campaign["id"]
        public = cs.public(campaign)
        entries = cs.log_entries(cid)
        verify = cs.verify_chain(cid)
        public.update(
            {
                "spec": campaign["spec"],
                "results": campaign["results"] or [],
                "deviations": campaign["deviations"] or [],
                "blocked": campaign["blocked"] or [],
                "summary": campaign.get("summary"),
                "summary_source": campaign.get("summary_source"),
                "report_files": campaign.get("report_files"),
                "steps_full": campaign["steps"],
                "step_index": (campaign["runtime"] or {}).get("step_index", -1),
                "log": [
                    {
                        "seq": e["seq"],
                        "time": e["time"],
                        "actor": e["actor"],
                        "action": e["action"],
                        "detail": e["detail"],
                        "hash": e["hash"],
                    }
                    for e in entries[-LOG_TAIL:]
                ],
                "log_count": len(entries),
                "verify": verify,
                "tampered": cs.tampered(cid),
                "signed_off_by": campaign.get("signed_off_by"),
                "signed_off_at": campaign.get("signed_off_at"),
                "chamber_name": campaign["chamber_name"],
                "notes": (campaign.get("requirement") or {}).get("notes", ""),
                "writing_report": cid in self._writing,
                "thermocouples": self._thermocouples(campaign),
                "pressure_rule": self._pressure_rule(campaign),
            }
        )
        return public

    @staticmethod
    def _thermocouples(campaign):
        """Returns the campaign's thermocouples with their status."""
        states = (campaign["runtime"] or {}).get("tc") or {}
        return [
            {
                "key": f"temp{i + 1}",
                "name": tc["name"],
                "stabilisation": bool(tc.get("stabilisation")),
                "status": (states.get(f"temp{i + 1}") or {}).get("status", "ok"),
                "reason": (states.get(f"temp{i + 1}") or {}).get("reason"),
            }
            for i, tc in enumerate(campaign["spec"].get("thermocouples") or [])
        ]

    @staticmethod
    def _pressure_rule(campaign):
        """Returns the pressure rule's limits and current hold."""
        spec = campaign["spec"]
        rule = spec.get("pressure_rule")
        if not rule or spec.get("vacuum") != "auto":
            return None
        held = (campaign["runtime"] or {}).get("pressure_hold")
        return {
            "hold_torr": cs.hpa_to_torr(rule["hold_hpa"]),
            "resume_torr": cs.hpa_to_torr(rule["resume_hpa"]),
            "held": {"kind": held["kind"], "temp_c": held["temp_c"]} if held else None,
        }

    @staticmethod
    def _trace_series(trace, start):
        if start is None:
            start = trace[0]["t"] if trace else 0.0
        rows = trace[:: max(1, len(trace) // CHART_POINTS)]
        return {
            "t_h": [round((p["t"] - start) / 3600.0, 4) for p in rows],
            "temp": [_finite(p["temp"]) for p in rows],
            "setpoint": [_finite(p["setpoint"]) for p in rows],
            "pres": [None] * len(rows),
            "pres_setpoint": [None] * len(rows),
            "duts": [[None] * len(rows) for _ in range(4)],
            "platen": [None] * len(rows),
        }

    def _series(self, client, start):
        sampler = getattr(client, "plc_samples", None)
        if sampler is None:
            return None
        rows = sampler(start, CHART_POINTS, CHART_KEYS)
        series = {
            "t_h": [],
            "temp": [],
            "setpoint": [],
            "pres": [],
            "pres_setpoint": [],
            "duts": [[], [], [], []],
            "platen": [],
        }
        for row in rows:
            series["t_h"].append(round((row["plc_time"] - start) / 3600.0, 4))
            series["temp"].append(_finite(row.get("temp")))
            series["setpoint"].append(_finite(row.get("temp_ref")))
            series["pres"].append(_finite(row.get("pres")))
            series["pres_setpoint"].append(_finite(row.get("pres_ref")))
            series["platen"].append(_finite(row.get("temp_platen")))
            for i in range(4):
                series["duts"][i].append(_tc(row.get(f"temp{i + 1}")))
        return series

    @Slot(str, result=str)
    def draft(self, requirement_json):
        def run():
            r = json.loads(requirement_json)
            chamber = self._chamber(r["chamber_id"])
            limits = control_settings.temperature_limits(control_settings.load())
            campaign = cs.draft(
                {
                    "article": r.get("article", ""),
                    "op_min_c": float(r["op_min_c"]),
                    "op_max_c": float(r["op_max_c"]),
                    "level": r.get("level") or "qualification",
                    "rehearsal": bool(r.get("rehearsal")),
                    "test_type": r.get("test_type") or cs.CYCLING,
                    "direction": r.get("direction") or "up",
                    "notes": r.get("notes") or "",
                    "margin_c": r.get("margin_c"),
                    "cycles": r.get("cycles"),
                },
                chamber,
                self.user() or "Operator",
                limits,
                actor="Planner",
            )
            chat = getattr(getattr(self._host, "agent_chat", None), "panel", None)
            if chat is not None:
                chat.follow_campaign(campaign["id"])
            return {"campaign": campaign["id"]}

        return self._guard(run)

    @Slot(int, result=str)
    def submit(self, campaign_id):
        return self._guard(lambda: (cs.submit(campaign_id, "Planner"), None)[1])

    @Slot(int, str, result=str)
    def decide(self, hold_id, choice):
        return self._guard(lambda: (cs.decide_hold(hold_id, choice, self.user()), None)[1])

    @Slot(int, str, result=str)
    def decideAs(self, hold_id, choice):  # noqa: N802
        return self._guard(lambda: (cs.decide_hold(hold_id, choice, "Auto-play (booth)"), None)[1])

    @Slot(int, result=str)
    def cancel(self, campaign_id):
        return self._guard(
            lambda: (cs.cancel(campaign_id, self.user(), "from the AI Agents page"), None)[1]
        )

    @Slot(int, result=str)
    def stop(self, campaign_id):
        return self._guard(
            lambda: (cs.request_stop(campaign_id, self.user(), "from the AI Agents page"), None)[1]
        )

    @Slot(int, result=str)
    def resetDemo(self, chamber_id):  # noqa: N802
        """Ends what runs on the chamber and clears the demo fault, for the next run."""

        def run():
            for c in cs.list_campaigns(30):
                if c["chamber_id"] != chamber_id:
                    continue
                if c["state"] in (cs.DRAFT, cs.APPROVAL):
                    cs.cancel(c["id"], self.user() or "Operator", "demo reset")
                elif c["state"] in (cs.RUNNING, cs.HOLDING):
                    cs.request_stop(c["id"], self.user() or "Operator", "demo reset")
                cs.undo_tamper(c["id"])
            self._client(chamber_id).send_text_cmd("sim_reset")

        return self._guard(run)

    @Slot(int, result=str)
    def fault(self, chamber_id):
        def run():
            self._client(chamber_id).send_text_cmd("sim_fault")
            campaign = self._campaign_on(chamber_id)
            if campaign is not None and campaign["state"] in cs.ACTIVE:
                cs.record(
                    campaign["id"],
                    "PLC",
                    "PID settings changed",
                    "the gains of the band in use changed at the chamber: P 2, I 50, D 1",
                )

        return self._guard(run)

    @Slot(int, result=str)
    def faultClear(self, chamber_id):  # noqa: N802
        return self._guard(lambda: self._client(chamber_id).send_text_cmd("sim_fault_clear"))

    @Slot(int, float, result=str)
    def speed(self, chamber_id, value):
        return self._guard(
            lambda: self._client(chamber_id).send_text_cmd(f"sim_speed {float(value):g}")
        )

    @Slot(int, str, result=str)
    def rogue(self, campaign_id, kind):
        def run():
            supervisor = self.supervisor()
            if supervisor is None:
                raise cs.CampaignError("Turn the Agent API on: the run supervisor checks commands.")
            if kind not in ROGUE:
                raise ValueError(f"kind must be one of {', '.join(ROGUE)}")
            actor, command = ROGUE[kind]
            command = dict(command)
            if command.get("name") == "setpoint" and command.get("target_c") is None:
                hot = cs.require(campaign_id)["spec"]["hot_c"]
                command["target_c"] = hot
                if not command.get("label"):
                    command["label"] = f"set {cs.deg(hot)} at {command['ramp_c_per_min']:g} °C/min"
            actor = actor or f"{self.user() or 'Operator'} (Edit level)"
            supervisor.submit_attempt(campaign_id, actor, command)
            return {"actor": actor, "command": command["label"]}

        return self._guard(run)

    @Slot(int, int, str, result=str)
    def sensorFault(self, chamber_id, index, fault):  # noqa: N802
        """Sets a thermocouple fault at the simulator."""

        def run():
            if fault not in ("open", "frozen", "spikes", "ok"):
                raise ValueError("fault must be open, frozen, spikes or ok")
            if not 1 <= int(index) <= 4:
                raise ValueError("thermocouple 1 to 4")
            self._client(chamber_id).send_text_cmd(f"sim_tc {int(index)} {fault}")

        return self._guard(run)

    @Slot(int, result=str)
    def outgas(self, chamber_id):
        """Releases a gas burst at the simulator."""
        return self._guard(lambda: self._client(chamber_id).send_text_cmd("sim_outgas"))

    @Slot(int, result=str)
    def tamper(self, campaign_id):
        return self._guard(lambda: {"seq": cs.tamper_demo(campaign_id)})

    @Slot(int, result=str)
    def untamper(self, campaign_id):
        return self._guard(lambda: {"undone": cs.undo_tamper(campaign_id)})

    @Slot(int, result=str)
    def issueReport(self, campaign_id):  # noqa: N802
        """Starts writing the campaign report with the AI."""

        def run():
            campaign = cs.require(campaign_id)
            if campaign["state"] not in cs.ENDED:
                raise cs.CampaignError("The campaign hasn't ended.")
            if campaign.get("report_issued_at") or campaign_id in self._writing:
                raise cs.CampaignError("The report is already issued or being written.")
            chat = getattr(getattr(self._host, "agent_chat", None), "panel", None)
            if not ai_mode.is_offline() and chat is not None:
                chat.follow_campaign(campaign_id)
                asked = chat.ask(
                    f"Campaign {campaign_id} has ended. Issue its as-run report: get_report_facts, "
                    "then issue_report with a short summary."
                )
                if asked:
                    return {"asked": "Professor"}
            self._writing.add(campaign_id)

            def write():
                try:
                    campaign_report.issue_with_ai(campaign_id, by=self.user() or "Reporter")
                finally:
                    self._writing.discard(campaign_id)

            threading.Thread(target=write, daemon=True).start()
            return {"writing": "local model" if ai_mode.is_offline() else "template"}

        return self._guard(run)

    @Slot(int, result=str)
    def templateReport(self, campaign_id):  # noqa: N802
        def run():
            if cs.require(campaign_id).get("report_issued_at"):
                raise cs.CampaignError("The report is already issued.")
            return {"files": campaign_report.issue(campaign_id, "", by=self.user() or "Reporter")}

        return self._guard(run)

    @Slot(int, result=str)
    def signOff(self, campaign_id):  # noqa: N802
        """Signs off the issued report."""
        return self._guard(lambda: {"files": campaign_report.sign_off(campaign_id, self.user())})

    @Slot(str, result=str)
    def openFile(self, path):  # noqa: N802
        def run():
            if not Path(path).is_file():
                raise ValueError(f"{path} doesn't exist.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        return self._guard(run)

    @Slot(str, result=str)
    def setMode(self, mode):  # noqa: N802
        def run():
            if self._host is not None and hasattr(self._host, "set_ai_mode"):
                self._host.set_ai_mode(mode)
            else:
                ai_mode.set_mode(mode)
            return {"mode": ai_mode.mode()}

        return self._guard(run)

    @Slot(str, result=str)
    def demoStart(self, mode):  # noqa: N802
        def run():
            if mode not in ai_mode.MODES:
                raise ValueError(f"mode must be one of {', '.join(ai_mode.MODES)}")
            self._host.start_demo(mode)
            return {"starting": True}

        return self._guard(run)

    @Slot(result=str)
    def demoStop(self):  # noqa: N802
        def run():
            self._host.autoplay.set_on(False)
            self._host.demo.stop()

        return self._guard(run)

    @Slot(bool, result=str)
    def setAutoPlay(self, on):  # noqa: N802
        """Turns auto-play on or off."""

        def run():
            self._host.autoplay.set_on(on)
            return {"auto_play": self._host.autoplay.on}

        return self._guard(run)

    @Slot(result=str)
    def startAgentApi(self):  # noqa: N802
        def run():
            if self._host is not None and self.supervisor() is None:
                self._host._toggle_agent_api(True, quiet=True)
            return {"agent_api": self.supervisor() is not None}

        return self._guard(run)

    @Slot(str, result=str)
    def ask(self, text):
        def run():
            chat = getattr(self._host, "agent_chat", None)
            if chat is None:
                raise ValueError("The chat isn't available.")
            if not chat.panel.isVisible():
                chat.toggle()
            return {"sent": chat.panel.ask(text)}

        return self._guard(run)
