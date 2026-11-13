"""Observer monitor: records chamber events and sends them to the Observer workflow."""

import logging
import threading
import time
from dataclasses import dataclass, field

from app.api import control_settings
from app.api.chambers import ChamberLinks
from app.core.analysis import anomalies
from app.services import agent_requests_service as requests_service
from app.services import campaign_service as cs
from app.services import observer_service as obs
from app.services import plc_gate

log = logging.getLogger(__name__)

STALE_S = 15.0
SETPOINT_STEADY_C = 0.2
DRIFT_CLEAR = 0.5
ANOMALY_EVERY_S = 60.0
MIN_ANOMALY_SAMPLES = 60
REQUEST_SLACK_S = 120.0


@dataclass
class ChamberWatch:
    """State the monitor keeps for one chamber between passes."""

    started: bool = False
    seen_online: bool = False
    offline_since: float | None = None
    offline_event: int | None = None
    in_alarm: bool = False
    last_job_end: float | None = None
    setpoint: float | None = None
    setpoint_since: float | None = None
    drift_since: float | None = None
    drift_reported: bool = False
    next_anomaly_check: float = 0.0
    anomaly_until: dict = field(default_factory=dict)


class ObserverMonitor:
    POLL_S = 2.0

    def __init__(self, links: ChamberLinks, token: str | None = None, deliver=None):
        self.links = links
        self._token = token
        self._deliver = deliver or obs.deliver
        self._watch: dict[int, ChamberWatch] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="observer-monitor", daemon=True)
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
                log.exception("observer monitor")

    def run_once(self, now: float | None = None) -> None:
        now = now or time.time()
        config = obs.load_config()
        if config["watch"]:
            for chamber in self.links.chambers():
                if chamber["protocol"] != "plc":
                    continue
                try:
                    self.observe(chamber, config, now)
                except Exception:
                    log.exception("observing chamber %s", chamber["name"])
            self._holds(config, now)
            self._requests(config, now)
        if config["push"]:
            self.push(config, now)

    def _record(self, config, kind, severity, title, facts, chamber, key):
        push = config["push"] and obs.at_least(severity, config["min_severity"])
        event = obs.record(kind, severity, title, facts, chamber, key, push)
        if event:
            log.info("observer event %s: %s", event["id"], title)
        return event

    def push(self, config, now: float) -> None:
        if self._token is None:
            from app.api.auth import api_token

            self._token = api_token()
        for event in obs.due_pushes(now):
            error = self._deliver(event, config["webhook_url"], self._token)
            obs.mark_pushed(event["id"], not error, error, now)
            if error:
                log.warning("observer event %s not sent: %s", event["id"], error)

    def observe(self, chamber: dict, config: dict, now: float) -> None:
        w = self._watch.setdefault(chamber["id"], ChamberWatch())
        client = self.links.client(chamber, wait=False)
        snap = client.snapshot()
        heard = (
            snap.connected
            and bool(snap.values)
            and snap.values_at is not None
            and now - snap.values_at <= STALE_S
        )
        self._connection(w, chamber, config, snap, heard, now)
        if not heard:
            return
        if not w.started:
            w.started = True
            w.last_job_end = snap.job_ended_at
        self._alarm(w, chamber, config, snap, now)
        self._job(w, chamber, config, snap)
        self._drift(w, chamber, config, snap, now)
        if now >= w.next_anomaly_check:
            w.next_anomaly_check = now + ANOMALY_EVERY_S
            self._anomalies(w, chamber, config, client, snap, now)

    def _connection(self, w, chamber, config, snap, heard, now):
        if heard:
            if w.offline_event is not None:
                self._record(
                    config,
                    obs.ONLINE,
                    obs.INFO,
                    f"{chamber['name']} is back online",
                    {
                        "offline_since": obs.iso(w.offline_since),
                        "offline_min": round((now - w.offline_since) / 60.0, 1),
                    },
                    chamber,
                    f"online:{chamber['id']}:{int(w.offline_since)}",
                )
            w.seen_online, w.offline_since, w.offline_event = True, None, None
            return
        if not w.seen_online:
            return
        w.offline_since = w.offline_since or (snap.values_at or now)
        if w.offline_event is None and now - w.offline_since >= config["offline_s"]:
            event = self._record(
                config,
                obs.OFFLINE,
                obs.WARNING,
                f"{chamber['name']} not heard from for {int(now - w.offline_since)} s",
                {
                    "last_values_at": obs.iso(snap.values_at),
                    "connected": snap.connected,
                    "error": snap.last_error or None,
                    "campaign_id": self._campaign_on(chamber),
                },
                chamber,
                f"offline:{chamber['id']}:{int(w.offline_since)}",
            )
            w.offline_event = event["id"] if event else 0

    def _alarm(self, w, chamber, config, snap, now):
        active = bool(snap.alarm_code) or snap.status == "alarm"
        if active and not w.in_alarm:
            labels = control_settings.alarm_labels(control_settings.load())
            bits = [
                labels.get(b, f"alarm bit {b}") for b in range(32) if snap.alarm_code & (1 << b)
            ]
            self._record(
                config,
                obs.ALARM,
                obs.CRITICAL,
                f"Alarm on {chamber['name']}" + (f": {', '.join(bits)}" if bits else ""),
                {
                    **self._live(snap),
                    "alarm_code": snap.alarm_code,
                    "active_alarms": bits,
                    "campaign_id": self._campaign_on(chamber),
                },
                chamber,
                f"alarm:{chamber['id']}:{int(snap.alarm_at or now)}",
            )
        elif w.in_alarm and not active:
            self._record(
                config,
                obs.ALARM_CLEARED,
                obs.INFO,
                f"The alarm on {chamber['name']} cleared",
                self._live(snap),
                chamber,
                f"alarm_cleared:{chamber['id']}:{int(now)}",
            )
        w.in_alarm = active

    def _job(self, w, chamber, config, snap):
        ended = snap.job_ended_at
        if not ended or (w.last_job_end is not None and ended <= w.last_job_end):
            return
        w.last_job_end = ended
        status = snap.status
        how = {"finish": "finished", "alarm": "ended in alarm"}.get(status, "was stopped")
        started = snap.job_started_at
        facts = {
            **self._live(snap),
            "ended_at": obs.iso(ended),
            "started_at": obs.iso(started) if started and started < ended else None,
            "duration_min": round((ended - started) / 60.0, 1)
            if started and started < ended
            else None,
            "campaign_id": self._campaign_on(chamber),
            "request_id": self._request_job(chamber, started, ended),
        }
        self._record(
            config,
            obs.JOB_ENDED,
            obs.WARNING if status == "alarm" else obs.INFO,
            f"The job on {chamber['name']} {how}",
            {k: v for k, v in facts.items() if v is not None},
            chamber,
            f"job:{chamber['id']}:{int(ended)}",
        )

    @staticmethod
    def _plc_now(snap, now):
        """Returns the PLC clock now."""
        plc_time = getattr(snap, "plc_time", None)
        if plc_time is None or snap.values_at is None:
            return now
        return plc_gate.plc_clock_now(
            plc_time, snap.values_at, getattr(snap, "plc_rate", 1.0) or 1.0, now
        )

    def _drift(self, w, chamber, config, snap, wall):
        now = self._plc_now(snap, wall)
        rate = getattr(snap, "plc_rate", 1.0) or 1.0
        temp, setpoint = snap.values.get("temp"), snap.values.get("temp_ref")
        if snap.status != "work" or temp is None or setpoint is None:
            w.setpoint = w.setpoint_since = w.drift_since = None
            w.drift_reported = False
            return
        if w.setpoint is None or abs(setpoint - w.setpoint) > SETPOINT_STEADY_C:
            w.setpoint, w.setpoint_since = setpoint, now
            w.drift_since, w.drift_reported = None, False
            return
        error = temp - setpoint
        threshold = float(config["drift_c"])
        if abs(error) <= threshold * DRIFT_CLEAR:
            w.drift_since, w.drift_reported = None, False
            return
        settled = now - w.setpoint_since >= float(config["settled_min"]) * 60.0
        if not settled or abs(error) <= threshold:
            return
        w.drift_since = w.drift_since or now
        if w.drift_reported or now - w.drift_since < float(config["drift_min"]) * 60.0:
            return
        w.drift_reported = True
        self._record(
            config,
            obs.DRIFT,
            obs.WARNING,
            f"{chamber['name']} is {abs(error):.1f} °C {'above' if error > 0 else 'below'} "
            f"its setpoint of {setpoint:+.1f} °C",
            {
                **self._live(snap),
                "error_c": round(error, 2),
                "threshold_c": threshold,
                "off_for_min": round((now - w.drift_since) / 60.0, 1),
                "setpoint_steady_since": obs.iso(wall - (now - w.setpoint_since) / rate),
                "campaign_id": self._campaign_on(chamber),
            },
            chamber,
            f"drift:{chamber['id']}:{int(w.drift_since)}",
        )

    def _anomalies(self, w, chamber, config, client, snap, now):
        if snap.status != "work" or not hasattr(client, "history"):
            return
        window_s = float(config["anomaly_window_min"]) * 60.0
        if hasattr(client, "plc_history"):
            recent = [
                {**s, "timestamp": s["plc_time"]}
                for s in client.plc_history(self._plc_now(snap, now) - window_s)
            ]
        else:
            recent = client.history(now - window_s)
        samples = [s for s in recent if s.get("temp") is not None and s.get("temp_ref") is not None]
        if len(samples) < MIN_ANOMALY_SAMPLES:
            return
        times = [s["timestamp"] for s in samples]
        temps = [s["temp"] for s in samples]
        refs = [s["temp_ref"] for s in samples]
        found = (
            anomalies.detect_oscillation(times, temps, refs)
            + anomalies.detect_spikes(times, temps, "temp")
            + anomalies.detect_flatlines(times, temps, "temp")
        )
        for a in found:
            if a.start <= w.anomaly_until.get(a.kind, 0.0):
                w.anomaly_until[a.kind] = max(w.anomaly_until[a.kind], a.end)
                continue
            w.anomaly_until[a.kind] = a.end
            self._record(
                config,
                obs.ANOMALY,
                a.severity if a.severity in obs.SEVERITIES else obs.WARNING,
                f"{chamber['name']}: {a.detail}",
                {
                    **self._live(snap),
                    "anomaly": a.kind,
                    "from": obs.iso(a.start),
                    "to": obs.iso(a.end),
                    "value": a.to_json()["value"],
                    "campaign_id": self._campaign_on(chamber),
                },
                chamber,
                f"anomaly:{chamber['id']}:{a.kind}:{int(a.start)}",
            )

    def _holds(self, config, now):
        wait_s = float(config["waiting_min"]) * 60.0
        for campaign in cs.list_campaigns(20, cs.ACTIVE):
            hold = cs.open_hold_of(campaign["id"])
            if hold is None:
                continue
            if hold["kind"] == "approval" and now - (cs.epoch(hold["opened_at"]) or now) < wait_s:
                continue
            chamber = {"id": campaign["chamber_id"], "name": campaign["chamber_name"]}
            self._record(
                config,
                obs.HOLD,
                obs.WARNING,
                f"Campaign {campaign['id']} ({campaign['title']}) waits for a decision: {hold['title']}",
                {
                    "campaign_id": campaign["id"],
                    "hold_id": hold["id"],
                    "hold": hold["kind"],
                    "detail": hold["detail"] or None,
                    "options": [o["label"] for o in hold["options"]],
                    "recommended": (hold["recommendation"] or {}).get("option"),
                    "opened_at": hold["opened_at"],
                },
                chamber,
                f"hold:{hold['id']}",
            )

    def _requests(self, config, now):
        wait_s = float(config["waiting_min"]) * 60.0
        requests_service.expire_stale()
        for request in requests_service.list_requests(requests_service.PENDING, 50):
            created = requests_service.epoch(request["created_at"]) or now
            if now - created < wait_s:
                continue
            self._record(
                config,
                obs.REQUEST_WAITING,
                obs.WARNING,
                f"Request {request['id']} waits for approval: {request['summary']}",
                {
                    "request_id": request["id"],
                    "reason": request["reason"],
                    "requested_by": request["requested_by"],
                    "requested_at": request["created_at"],
                },
                {"id": request["chamber_id"], "name": request["chamber_name"]},
                f"request:{request['id']}",
            )

    @staticmethod
    def _live(snap) -> dict:
        v = snap.values
        live = {
            "status": snap.status,
            "temperature_c": round(v["temp"], 2) if v.get("temp") is not None else None,
            "setpoint_c": round(v["temp_ref"], 2) if v.get("temp_ref") is not None else None,
            "output_percent": round(v["temp_u"], 1) if v.get("temp_u") is not None else None,
        }
        return {k: x for k, x in live.items() if x is not None}

    @staticmethod
    def _campaign_on(chamber) -> int | None:
        return next(
            (c["id"] for c in cs.list_campaigns(20, cs.ACTIVE) if c["chamber_id"] == chamber["id"]),
            None,
        )

    @staticmethod
    def _request_job(chamber, started, ended) -> int | None:
        """The agent request that started this job, if any."""
        if not started:
            return None
        for request in requests_service.list_requests(requests_service.EXECUTED, 20):
            sent = requests_service.epoch(request["executed_at"])
            if (
                request["chamber_id"] == chamber["id"]
                and sent
                and started - REQUEST_SLACK_S <= sent <= ended
            ):
                return request["id"]
        return None
