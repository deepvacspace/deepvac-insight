"""Runs approved agent requests: polls agent_requests_service and sends each
approved request to its chamber's PLC, once, recording the outcome. Then
follows the job an executed setpoint or program started until it ends
(finished, stopped, alarm, replaced by another job), so the chat can tell
the crew what became of it."""

import logging
import threading
import time

from app.api.chambers import ChamberLinks
from app.api.worker_tools import execute_request
from app.services import agent_requests_service as requests_service
from app.services.plc_gate import STATUS_NAMES

log = logging.getLogger(__name__)

JOB_KINDS = ("setpoint", "program")
START_TIMEOUT_S = 60.0  # a job the PLC hasn't reported running by then is given up on
CLOCK_SLACK_S = 2.0  # PLC state values arrive about once a second


class RequestExecutor:
    POLL_S = 1.0

    def __init__(self, links: ChamberLinks):
        self.links = links
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="agent-request-executor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_once(self) -> int:
        requests_service.expire_stale()
        done = 0
        for request in requests_service.approved_requests():
            if not requests_service.claim(request["id"]):
                continue  # another executor took it
            try:
                result = execute_request(self.links, request)
                job = requests_service.JOB_STARTING if request["kind"] in JOB_KINDS else None
                requests_service.mark_done(request["id"], True, result, job)
                log.info("agent request %s executed: %s", request["id"], result)
            except Exception as exc:  # recorded on the request for the operator and the agent
                requests_service.mark_done(request["id"], False, f"failed: {exc}")
                log.warning("agent request %s failed: %s", request["id"], exc)
            done += 1
        self.follow_jobs()
        return done

    def follow_jobs(self, now: float | None = None) -> None:
        now = now or time.time()
        jobs = requests_service.open_jobs()
        for request in jobs:
            try:
                self._follow(request, jobs, now)
            except Exception:
                log.exception("following the job of agent request %s", request["id"])

    def _follow(self, request: dict, jobs: list[dict], now: float) -> None:
        rid = request["id"]
        sent_at = requests_service.epoch(request["executed_at"])
        # a later agent job on the same chamber takes over (the PLC may go from work to work)
        later = [j for j in jobs if j["id"] > rid and j["chamber_id"] == request["chamber_id"]]
        if later:
            requests_service.set_job_state(
                rid,
                requests_service.JOB_REPLACED,
                ended_at=requests_service.epoch(later[0]["executed_at"]),
            )
            return
        chamber = self.links.resolve(request["chamber_id"])
        snap = self.links.client(chamber, wait=False).snapshot()
        if not snap.connected or snap.values_at is None or snap.values_at < sent_at:
            return  # nothing from the PLC since the job was sent
        working = snap.status == STATUS_NAMES[2]
        if request["job_state"] == requests_service.JOB_STARTING:
            started = snap.job_started_at
            if working:
                begun = started if started and started >= sent_at - CLOCK_SLACK_S else sent_at
                requests_service.set_job_state(rid, requests_service.JOB_RUNNING, started_at=begun)
            elif snap.job_ended_at and snap.job_ended_at >= sent_at - CLOCK_SLACK_S:
                # ran and ended between two polls
                requests_service.set_job_state(
                    rid, self._ended(snap.status), started, snap.job_ended_at
                )
            elif now - sent_at > START_TIMEOUT_S:
                requests_service.set_job_state(rid, requests_service.JOB_UNKNOWN, ended_at=now)
            return
        begun = requests_service.epoch(request["job_started_at"]) or sent_at
        if snap.job_started_at and snap.job_started_at > begun + CLOCK_SLACK_S:
            # another job (the operator's) started after this one
            requests_service.set_job_state(
                rid, requests_service.JOB_REPLACED, ended_at=snap.job_started_at
            )
        elif not working and snap.job_ended_at and snap.job_ended_at >= begun - CLOCK_SLACK_S:
            requests_service.set_job_state(
                rid, self._ended(snap.status), ended_at=snap.job_ended_at
            )

    @staticmethod
    def _ended(status: str) -> str:
        if status == STATUS_NAMES[3]:
            return requests_service.JOB_FINISHED
        if status == STATUS_NAMES[4]:
            return requests_service.JOB_ALARM
        return requests_service.JOB_STOPPED

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_S):
            try:
                self.run_once()
            except Exception:
                log.exception("agent request executor")
