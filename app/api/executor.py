"""Runs approved agent requests: polls agent_requests_service and sends each
approved request to its chamber's PLC, once, recording the outcome."""

import logging
import threading

from app.api.chambers import ChamberLinks
from app.api.worker_tools import execute_request
from app.services import agent_requests_service as requests_service

log = logging.getLogger(__name__)


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
                requests_service.mark_done(request["id"], True, result)
                log.info("agent request %s executed: %s", request["id"], result)
            except Exception as exc:  # recorded on the request for the operator and the agent
                requests_service.mark_done(request["id"], False, f"failed: {exc}")
                log.warning("agent request %s failed: %s", request["id"], exc)
            done += 1
        return done

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_S):
            try:
                self.run_once()
            except Exception:
                log.exception("agent request executor")
