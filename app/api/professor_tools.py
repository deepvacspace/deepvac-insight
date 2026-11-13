"""Professor tools for test campaigns."""

import time

from app.api import control_settings
from app.api.chambers import ChamberLinks
from app.services import campaign_report
from app.services import campaign_service as cs

WAIT_DEFAULT_S = 120
WAIT_MAX_S = 180
WAIT_POLL_S = 1.0
ACTOR = "Professor"


class ProfessorTools:
    def __init__(self, links: ChamberLinks | None = None, created_by: str = "Professor agent"):
        self.links = links or ChamberLinks()
        self.created_by = created_by

    def get_standards_pack(self) -> dict:
        pack = cs.load_pack()
        low, high = control_settings.temperature_limits(control_settings.load())
        return {
            "pack": pack,
            "chamber_setpoint_range_c": [low, high],
            "chambers": [
                {"id": c["id"], "name": c["name"]}
                for c in self.links.chambers()
                if c["protocol"] == "plc"
            ],
        }

    def draft_campaign(
        self,
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
        test_type: str = "cycling",
        direction: str = "up",
    ) -> dict:
        ch = self.links.resolve(chamber)
        if ch["protocol"] != "plc":
            raise cs.CampaignError(
                f"'{ch['name']}' is a monitoring feed: campaigns run on PLC chambers."
            )
        limits = control_settings.temperature_limits(control_settings.load())
        campaign = cs.draft(
            {
                "article": article,
                "op_min_c": op_min_c,
                "op_max_c": op_max_c,
                "level": level,
                "rehearsal": rehearsal,
                "notes": notes,
                "test_type": test_type,
                "direction": direction,
            },
            ch,
            self.created_by,
            limits,
            {
                "cycles": cycles,
                "dwell_min": dwell_min,
                "ramp_c_per_min": ramp_c_per_min,
                "reason": reason,
                "by": ACTOR,
            },
            actor=ACTOR,
        )
        out = cs.public(campaign)
        out["next"] = (
            "This is a draft: nothing is sent. Show the user the specification (every value with its source); "
            "submit_campaign opens the approval decision point in Deepvac Insight."
        )
        return out

    def submit_campaign(self, campaign_id: int) -> dict:
        out = cs.public(cs.submit(campaign_id, actor=ACTOR))
        out["next"] = (
            "An operator approves or declines it in Deepvac Insight (AI Agents page). Nothing moves until then. "
            "Insight tells the chat when a decision point opens or the campaign ends; wait_for_campaign waits a few "
            "minutes for a change."
        )
        return out

    def get_campaign(self, campaign_id: int = 0) -> dict:
        campaign = cs.require(campaign_id) if campaign_id else cs.latest_campaign()
        if campaign is None:
            raise cs.CampaignError("No campaigns yet.")
        out = cs.public(campaign)
        now = cs.chamber_now(campaign["runtime"])
        points = cs.trace(campaign["id"], now - 30 * 60)
        if points:
            out["last_30_min"] = [
                {
                    "minutes_ago": round((now - p["t"]) / 60.0, 1),
                    "temperature_c": round(p["temp"], 2) if p["temp"] is not None else None,
                    "setpoint_c": round(p["setpoint"], 2) if p["setpoint"] is not None else None,
                }
                for p in points[-24:]
            ]
        return out

    def list_campaigns(self, limit: int = 10) -> dict:
        return {
            "campaigns": [
                cs.public(c, detail=False) for c in cs.list_campaigns(min(int(limit), 50))
            ]
        }

    def wait_for_campaign(self, campaign_id: int, timeout_s: float = WAIT_DEFAULT_S) -> dict:
        """Waits until the campaign's state or decision point changes."""
        timeout_s = max(1.0, min(float(timeout_s), WAIT_MAX_S))
        start = cs.news_key(cs.require(campaign_id))
        started = time.monotonic()
        while time.monotonic() - started < timeout_s:
            time.sleep(WAIT_POLL_S)
            if cs.news_key(cs.require(campaign_id)) != start:
                break
        out = self.get_campaign(campaign_id)
        out["waited_s"] = round(time.monotonic() - started)
        if out["news"] == start:
            out["next"] = (
                "Nothing changed yet. Don't wait again: Deepvac Insight tells the chat when a decision point opens "
                "or the campaign ends."
            )
        return out

    def recommend(self, campaign_id: int, option: str, rationale: str) -> dict:
        hold = cs.recommend(campaign_id, option, rationale, by=ACTOR)
        return {
            "campaign": campaign_id,
            "hold_point": hold["title"],
            "recommended": option,
            "next": "The operator sees your recommendation in Deepvac Insight and decides.",
        }

    def issue_report(self, campaign_id: int, summary: str) -> dict:
        files = campaign_report.issue(campaign_id, summary, by=ACTOR)
        return {
            "campaign": campaign_id,
            "pdf": files["pdf"],
            "link": files["pdf_link"],
            "markdown_link": files["markdown_link"],
            "chain_ok": files["chain_ok"],
            "signed": files["signed"],
            "next": "Give the user the report as a markdown link. An operator signs it off in Deepvac Insight.",
        }

    def get_report_facts(self, campaign_id: int) -> dict:
        campaign = cs.require(campaign_id)
        if campaign["state"] not in cs.ENDED:
            raise cs.CampaignError(
                f"Campaign {campaign_id} is {campaign['state']}: it hasn't ended."
            )
        return campaign_report.facts(campaign)

    def cancel_campaign(self, campaign_id: int, reason: str) -> dict:
        if not (reason or "").strip():
            raise cs.CampaignError("Give the reason.")
        return cs.public(cs.cancel(campaign_id, ACTOR, reason), detail=False)
