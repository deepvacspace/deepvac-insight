"""PLC connections for the agent API, one per chamber."""

import threading

from app.services import chambers_service
from app.services.plc_gate import PlcGateClient


class ChamberError(ValueError):
    pass


class ChamberLinks:
    def __init__(self, connect_timeout_s: float = 8.0):
        self._lock = threading.Lock()
        self._clients: dict[int, PlcGateClient] = {}
        self._connect_timeout_s = connect_timeout_s

    def chambers(self) -> list[dict]:
        return chambers_service.list_chambers()

    def resolve(self, reference) -> dict:
        """Finds a chamber by id or name."""
        chambers = self.chambers()
        text = str(reference or "").strip()
        if not text:
            plcs = [c for c in chambers if c["protocol"] == "plc"]
            if len(plcs) == 1:
                return plcs[0]
            raise ChamberError(
                "Say which chamber: " + ", ".join(f"{c['id']} {c['name']}" for c in chambers)
            )
        for chamber in chambers:
            if str(chamber["id"]) == text or chamber["name"].lower() == text.lower():
                return chamber
        raise ChamberError(
            f"No chamber '{text}'. Chambers: "
            + ", ".join(f"{c['id']} {c['name']}" for c in chambers)
        )

    def client(self, chamber: dict, wait: bool = True) -> PlcGateClient:
        if chamber["protocol"] != "plc":
            raise ChamberError(
                f"'{chamber['name']}' is a monitoring feed (protocol json): the agent API reads and controls "
                "chambers over the PLC gate only."
            )
        with self._lock:
            client = self._clients.get(chamber["id"])
            if client and (client.host, client.port) != (chamber["host"], int(chamber["port"])):
                client.stop()
                client = None
            if client is None:
                client = PlcGateClient(chamber["host"], int(chamber["port"]))
                client.start()
                self._clients[chamber["id"]] = client
        if wait:
            client.wait_connected(self._connect_timeout_s)
        return client

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.stop()
            self._clients.clear()
