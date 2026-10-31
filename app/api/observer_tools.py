"""Observer tools: read an event with its context, report on it, and propose a
stop or setpoint as an agent request."""

from app.api.chambers import ChamberLinks
from app.api.worker_tools import WorkerTools, public_request
from app.services import agent_requests_service as requests_service
from app.services import campaign_service as cs
from app.services import observer_service as obs

TRACE_MIN = 30
TRACE_POINTS = 60


class ObserverTools:
    def __init__(self, links: ChamberLinks | None = None, requested_by: str = "observer agent"):
        self.links = links or ChamberLinks()
        self.worker = WorkerTools(self.links, requested_by)

    def get_event(self, event_id: int) -> dict:
        event = obs.require(event_id)
        result = {"event": obs.public(event)}
        chamber = self._chamber(event)
        if chamber is not None:
            try:
                result["chamber_now"] = self.worker.get_live_status(str(chamber["id"]))
            except (ValueError, RuntimeError, ConnectionError) as exc:
                result["chamber_now"] = {"note": str(exc)}
            result["last_30_min"] = self._trace(chamber)
            earlier = [
                {
                    "id": e["id"],
                    "at": e["created_at"][:19],
                    "severity": e["severity"],
                    "title": e["title"],
                }
                for e in obs.list_events(11, chamber_id=chamber["id"], hours=24)
                if e["id"] != event["id"]
            ][:10]
            if earlier:
                result["earlier_on_this_chamber_24h"] = earlier
        facts = event["facts"]
        if facts.get("campaign_id"):
            campaign = cs.get_campaign(facts["campaign_id"])
            if campaign:
                result["campaign"] = cs.public(campaign, detail=False)
        if facts.get("request_id"):
            request = requests_service.get_request(facts["request_id"])
            if request:
                result["request"] = public_request(request)
        result["next"] = (
            "Judge it from these facts, then report_event: a short summary, and notify=true with the "
            "message only when a person should act or know now."
        )
        return result

    def list_events(
        self, hours: float = 24, min_severity: str = "info", chamber: str = "", limit: int = 20
    ) -> dict:
        if min_severity not in obs.SEVERITIES:
            raise obs.ObserverError(f"min_severity must be one of {', '.join(obs.SEVERITIES)}.")
        chamber_id = self.links.resolve(chamber)["id"] if chamber else None
        events = obs.list_events(
            max(1, min(int(limit), 50)), min_severity, chamber_id, max(0.1, float(hours))
        )
        return {
            "events": [
                {
                    k: e[k]
                    for k in (
                        "id",
                        "created_at",
                        "chamber_name",
                        "kind",
                        "severity",
                        "title",
                        "summary",
                    )
                    if e.get(k)
                }
                | {"acknowledged": bool(e["acknowledged_at"])}
                for e in events
            ]
        }

    def get_live_status(self, chamber: str = "") -> dict:
        return self.worker.get_live_status(chamber)

    def report_event(self, event_id: int, summary: str, notify: bool, message: str = "") -> dict:
        event = obs.report(event_id, summary, notify, message)
        result = obs.public(event)
        result["next"] = (
            "Recorded in DeepVac Insight. The workflow notifies people with your message."
            if notify
            else "Recorded in DeepVac Insight; nobody is notified."
        )
        return result

    def propose_stop(self, event_id: int, reason: str) -> dict:
        event, chamber = self._proposal_target(event_id)
        request = self.worker.request_stop(self._reason(event, reason), str(chamber["id"]))
        return self._proposed(event, request)

    def propose_setpoint(
        self,
        event_id: int,
        temperature_c: float,
        reason: str,
        ramp_c_per_min: float | None = None,
        hold_minutes: float | None = None,
    ) -> dict:
        event, chamber = self._proposal_target(event_id)
        request = self.worker.request_setpoint(
            temperature_c,
            self._reason(event, reason),
            str(chamber["id"]),
            ramp_c_per_min,
            hold_minutes,
        )
        return self._proposed(event, request)

    def _chamber(self, event):
        if event["chamber_id"] is None:
            return None
        try:
            return self.links.resolve(event["chamber_id"])
        except ValueError:
            return None

    def _trace(self, chamber) -> dict:
        try:
            client = self.links.client(chamber, wait=False)
        except ValueError as exc:
            return {"note": str(exc)}
        if not hasattr(client, "history"):
            return {"note": "No samples kept for this chamber."}
        samples = client.history()
        if not samples:
            return {"note": "No samples from the chamber since Insight connected."}
        last = samples[-1]["timestamp"]
        recent = [s for s in samples if s["timestamp"] >= last - TRACE_MIN * 60]
        step = max(1, len(recent) // TRACE_POINTS)
        points = recent[::step]
        if points[-1] is not recent[-1]:
            points.append(recent[-1])

        def r(value, digits):
            return round(value, digits) if value is not None else None

        return {
            "until": obs.iso(last),
            "points": [
                {
                    "min_ago": round((last - s["timestamp"]) / 60.0, 1),
                    "temp_c": r(s.get("temp"), 2),
                    "setpoint_c": r(s.get("temp_ref"), 2),
                    "output_percent": r(s.get("temp_u"), 1),
                }
                for s in points
            ],
        }

    def _proposal_target(self, event_id):
        event = obs.require(event_id)
        chamber = self._chamber(event)
        if chamber is None:
            raise obs.ObserverError(f"Event {event_id} is not about a chamber the API controls.")
        running = [
            c["id"] for c in cs.list_campaigns(20, cs.ACTIVE) if c["chamber_id"] == chamber["id"]
        ]
        if running:
            raise obs.ObserverError(
                f"Campaign {running[0]} runs on {chamber['name']}: its run supervisor and the operator's "
                "hold-point decisions handle the chamber. Say so in your report instead."
            )
        return event, chamber

    @staticmethod
    def _reason(event, reason):
        if not reason or not reason.strip():
            raise obs.ObserverError("Give the reason: the operator sees it when deciding.")
        return f"Observer, event {event['id']} ({event['title']}): {reason.strip()}"

    @staticmethod
    def _proposed(event, request):
        obs.add_facts(event["id"], proposed_request=request["id"])
        return request
