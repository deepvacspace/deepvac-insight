"""Unattended loop of demo campaigns."""

from __future__ import annotations

import logging
import random
import time

from PySide6.QtCore import QObject, QTimer

from app.services import ai_mode, campaign_report
from app.services import campaign_service as cs

log = logging.getLogger(__name__)

ACTOR = "Auto-play"
HOLD_WAIT_S = 6.0
NEXT_RUN_S = 20.0
STEP_S = 3.0


class AutoPlay(QObject):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.on = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.step)
        self._next = 0.0
        self._hold_seen: dict[int, float] = {}
        self._faulted: set[int] = set()
        self._reported: set[int] = set()
        self._ended: dict[int, float] = {}

    def set_on(self, on: bool) -> None:
        self.on = bool(on)
        if self.on:
            self._next = 0.0
            self._timer.start(1000)
        else:
            self._timer.stop()

    def _chamber_id(self):
        demo = getattr(self.host, "demo", None)
        return demo.chamber_id if demo is not None else None

    def _client(self):
        links = self.host.plc_links
        return links.client(links.resolve(self._chamber_id()), wait=False)

    def _campaign(self, chamber_id):
        mine = [c for c in cs.list_campaigns(30) if c["chamber_id"] == chamber_id]
        active = [c for c in mine if c["state"] in cs.ACTIVE or c["state"] == cs.DRAFT]
        return (active or mine or [None])[0]

    def _ready(self) -> bool:
        if self._chamber_id() is None or getattr(self.host, "agent_api", None) is None:
            return False
        snap = self._client().snapshot()
        return bool(snap.connected and snap.values_at and time.time() - snap.values_at < 15)

    @staticmethod
    def fault_step(campaign) -> str:
        """Returns the dwell where the demo fault is injected."""
        dwells = [s["label"] for s in campaign["steps"] if s["kind"] == "dwell"]
        second_hot = [
            s["label"]
            for s in campaign["steps"]
            if s["kind"] == "dwell" and s.get("cycle") == 2 and s.get("plateau") == "hot"
        ]
        return (second_hot or dwells or [""])[0]

    def step(self, now=None) -> str | None:
        """Does the next auto-play action and returns it."""
        now = now or time.time()
        if not self.on or now < self._next:
            return None
        try:
            if not self._ready():
                return None
            return self._act(now)
        except Exception:
            log.exception("auto-play")
            self._next = now + STEP_S
            return None

    def _act(self, now) -> str | None:
        chamber_id = self._chamber_id()
        campaign = self._campaign(chamber_id)
        state = campaign["state"] if campaign else None
        if (
            campaign is None
            or state in (cs.CANCELLED, cs.DECLINED)
            or (
                state in cs.ENDED
                and campaign.get("report_issued_at")
                and now - self._ended.setdefault(campaign["id"], now) > NEXT_RUN_S
            )
        ):
            return self._new_campaign(chamber_id, now, reset=campaign is not None)
        if state == cs.DRAFT:
            cs.submit(campaign["id"], "Planner")
            self._next = now + STEP_S
            return "submitted"
        hold = cs.open_hold_of(campaign["id"])
        if hold is not None:
            seen = self._hold_seen.setdefault(hold["id"], now)
            if now - seen < HOLD_WAIT_S:
                return None
            rec = hold["recommendation"]
            choice = rec["option"] if rec else hold["options"][0]["key"]
            cs.decide_hold(hold["id"], choice, ACTOR)
            self._next = now + STEP_S
            return f"decided {hold['kind']}: {choice}"
        if state in (cs.RUNNING, cs.HOLDING) and campaign["id"] not in self._faulted:
            step = cs.step_progress(campaign)
            if (
                step
                and step["label"] == self.fault_step(campaign)
                and (campaign["runtime"] or {}).get("stabilised_at")
            ):
                self._faulted.add(campaign["id"])
                self._client().send_text_cmd("sim_fault")
                cs.record(
                    campaign["id"],
                    "PLC",
                    "PID settings changed",
                    "the gains of the band in use changed at the chamber: P 2, I 50, D 1",
                )
                return "fault injected"
        if (
            state in cs.ENDED
            and not campaign.get("report_issued_at")
            and campaign["id"] not in self._reported
        ):
            self._reported.add(campaign["id"])
            if ai_mode.is_offline():
                import threading

                threading.Thread(
                    target=campaign_report.issue_with_ai, args=(campaign["id"], ACTOR), daemon=True
                ).start()
            else:
                campaign_report.issue(campaign["id"], "", by=ACTOR)
            self._next = now + STEP_S
            return "report"
        return None

    def _new_campaign(self, chamber_id, now, reset) -> str:
        from app.api import control_settings

        if reset:
            self._client().send_text_cmd("sim_reset")
        pack = cs.load_pack()
        article = random.choice(
            pack.get("articles") or [{"name": "Test article", "op_min_c": -10, "op_max_c": 45}]
        )
        chamber = self.host.plc_links.resolve(chamber_id)
        limits = control_settings.temperature_limits(control_settings.load())
        campaign = cs.draft(
            {
                "article": article["name"],
                "op_min_c": article["op_min_c"],
                "op_max_c": article["op_max_c"],
                "level": "qualification",
                "test_type": cs.CYCLING,
            },  # fmt: skip
            chamber,
            ACTOR,
            limits,
            actor="Planner",
        )
        chat = getattr(getattr(self.host, "agent_chat", None), "panel", None)
        if chat is not None:
            chat.follow_campaign(campaign["id"])
        self._next = now + STEP_S
        return f"drafted {campaign['id']}"
