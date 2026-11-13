"""Starts the demo in one step: simulator, chamber, Agent API and AI."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import app.common as common
from app.services import ai_mode, chambers_service

log = logging.getLogger(__name__)

CONFIG_FILE = "demo.json"
DEFAULTS = {
    "simulator_exe": "",
    "port": 4321,
    "speed": 300,
    "period": "50ms",
    "chamber_name": "Deepvac Chamber",
    "n8n_url": "http://localhost:5678/healthz",
}
STEPS = (
    ("config", "Chamber configuration"),
    ("simulator", "Chamber"),
    ("chamber", "Chamber registered"),
    ("agent_api", "Agent API (run supervisor)"),
    ("ai", "AI"),
    ("connected", "Connected"),
    ("awake", "PC kept awake"),
)
CONNECT_TIMEOUT_S = 20.0

_KEEP_AWAKE = 0x80000000 | 0x00000001 | 0x00000002
_ALLOW_SLEEP = 0x80000000


def load_config() -> dict:
    config = dict(DEFAULTS)
    with contextlib.suppress(OSError, ValueError):
        config.update(json.loads((common.DATA_DIR / CONFIG_FILE).read_text(encoding="utf-8")))
    return config


def simulator_exe(config=None) -> Path | None:
    config = config or load_config()
    candidates = []
    if config.get("simulator_exe"):
        candidates.append(Path(config["simulator_exe"]))
    if os.environ.get("DEEPVAC_SIMULATOR"):
        candidates.append(Path(os.environ["DEEPVAC_SIMULATOR"]))
    root = Path(__file__).resolve().parents[2]
    candidates += [
        root / "simulator" / "chambersim.exe",
        root.parent / "simulator" / "chambersim.exe",
        Path.home() / "Desktop" / "deepvac" / "simulator" / "chambersim.exe",
    ]
    return next((p for p in candidates if p.is_file()), None)


def control_settings_json() -> Path | None:
    """Deepvac Control's own settings.json."""
    root = Path(__file__).resolve().parents[2]
    candidates = [
        root.parent / "control2-client" / "settings.json",
        root.parent.parent / "control2-client" / "settings.json",
        Path.home() / "Desktop" / "control2-client" / "settings.json",
    ]
    return next((p for p in candidates if p.is_file()), None)


def _load_control(path: Path) -> dict:
    text = path.read_bytes().decode("utf-8", errors="replace")
    return json.loads(text.replace("�", "°"))


def _state_names(settings: dict) -> set:
    return {s.get("name") for s in settings.get("states") or []}


def sync_chamber_config() -> str:
    """Updates Insight's chamber configuration from Deepvac Control's."""
    mine_path = common.DATA_DIR / "control_settings.json"
    control = control_settings_json()
    mine = {}
    with contextlib.suppress(OSError, ValueError):
        mine = json.loads(mine_path.read_text(encoding="utf-8"))
    if control is None:
        if not mine:
            raise RuntimeError("no chamber configuration (data/control_settings.json)")
        return "Insight's"
    theirs = _load_control(control)
    missing = _state_names(theirs) - _state_names(mine)
    if mine and not missing:
        return "Insight's, the same as Deepvac Control's"
    if mine:
        backup = mine_path.with_name(
            f"control_settings.before_demo_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
        backup.write_text(mine_path.read_text(encoding="utf-8"), encoding="utf-8")
    mine_path.parent.mkdir(parents=True, exist_ok=True)
    mine_path.write_text(json.dumps(theirs, indent=2, ensure_ascii=False), encoding="utf-8")
    added = ", ".join(sorted(n for n in missing if n)[:6])
    return (
        f"updated from Deepvac Control ({added}…), the old one kept"
        if mine
        else "from Deepvac Control"
    )


DEMO_STATES = (
    (
        {
            "name": "temp_platen",
            "label": "Platen, °C",
            "color": "#0f766e",
            "pen": 1,
            "visible": False,
            "width": 2,
        },
        ("temp4", "temp3", "temp2", "temp1", "temp"),
    ),
)


def ensure_demo_states() -> list[str]:
    """Adds the demo's extra states to the chamber configuration."""
    path = common.DATA_DIR / "control_settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    states = settings.setdefault("states", [])
    names = [s.get("name") for s in states]
    added = []
    for state, after in DEMO_STATES:
        if state["name"] in names:
            continue
        where = next((names.index(n) + 1 for n in after if n in names), len(states))
        states.insert(where, dict(state))
        names.insert(where, state["name"])
        added.append(state["name"])
    if added:
        path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return added


def chamber_settings() -> Path | None:
    """Returns the chamber configuration file for the simulator."""
    candidates = [common.DATA_DIR / "control_settings.json"]
    root = Path(__file__).resolve().parents[2]
    candidates += [
        root.parent / "control2-client" / "settings.json",
        root.parent.parent / "control2-client" / "settings.json",
        Path.home() / "Desktop" / "control2-client" / "settings.json",
    ]
    return next((p for p in candidates if p.is_file()), None)


def port_open(port, host="127.0.0.1", timeout=0.3) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=timeout):
        return True
    return False


def n8n_answers(url, timeout=2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (OSError, urllib.error.URLError):
        return False


def keep_awake(on: bool) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.kernel32.SetThreadExecutionState(_KEEP_AWAKE if on else _ALLOW_SLEEP))


class DemoLauncher:
    """Starts and stops the demo."""

    def __init__(self, host):
        self.host = host
        self.process: subprocess.Popen | None = None
        self.chamber_id: int | None = None
        self.mode = None
        self.running = False
        self.starting = False
        self.steps = {
            key: {"label": label, "state": "pending", "detail": ""} for key, label in STEPS
        }
        self._lock = threading.Lock()

    def _set(self, key, state, detail=""):
        self.steps[key].update(state=state, detail=detail)

    def status(self) -> dict:
        return {
            "running": self.running,
            "starting": self.starting,
            "mode": self.mode,
            "chamber_id": self.chamber_id,
            "steps": [{"key": k, **v} for k, v in self.steps.items()],
        }

    def start(self, mode: str, gui_call=None) -> None:
        """Starts the demo in the given AI mode."""
        with self._lock:
            if self.starting:
                return
            self.starting = True
        self.mode = mode
        for key in self.steps:
            self._set(key, "pending")
        threading.Thread(
            target=self._start, args=(mode, gui_call or (lambda fn: fn())), daemon=True
        ).start()

    def _start(self, mode, gui_call) -> None:
        try:
            config = load_config()
            port = int(config["port"])
            synced = sync_chamber_config()
            added = ensure_demo_states()
            self._set("config", "ok", synced + (f"; added {', '.join(added)}" if added else ""))
            self._start_simulator(config, port)
            self._register_chamber(config, port)
            done = threading.Event()
            gui_call(lambda: (self._gui_steps(mode), done.set()))
            done.wait(30)
            self._wait_connected(config)
            self._check_ai(mode, config)
            self._set("awake", "ok" if keep_awake(True) else "warn", "no sleep, screen on")
            self.running = True
        except Exception as exc:
            log.exception("demo start")
            for step in self.steps.values():
                if step["state"] == "pending":
                    step.update(state="fail", detail=str(exc))
                    break
        finally:
            self.starting = False

    def _start_simulator(self, config, port) -> None:
        if port_open(port):
            self._set("simulator", "ok", f"port {port}, already running")
            return
        exe, settings = simulator_exe(config), chamber_settings()
        if exe is None:
            raise RuntimeError("chambersim.exe not found (data/demo.json simulator_exe)")
        if settings is None:
            raise RuntimeError("no chamber configuration (data/control_settings.json)")
        self.process = subprocess.Popen(
            [
                str(exe),
                "-settings",
                str(settings),
                "-listen",
                f"127.0.0.1:{port}",
                "-speed",
                str(config["speed"]),
                "-period",
                str(config["period"]),
                "-start-temp",
                "20",
                "-ambient",
                "20",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not port_open(port):
            if self.process.poll() is not None:
                raise RuntimeError(f"the simulator stopped (exit {self.process.returncode})")
            time.sleep(0.2)
        if not port_open(port):
            raise RuntimeError(f"the simulator doesn't answer on port {port}")
        self._set("simulator", "ok", f"port {port}")

    def _register_chamber(self, config, port) -> None:
        for chamber in chambers_service.list_chambers():
            if (
                chamber["protocol"] == "plc"
                and chamber["host"] in ("127.0.0.1", "localhost")
                and int(chamber["port"]) == port
            ):
                self.chamber_id = chamber["id"]
                self._set("chamber", "ok", chamber["name"])
                return
        chambers_service.add_chamber(config["chamber_name"], "127.0.0.1", port, protocol="plc")
        chamber = next(
            c for c in chambers_service.list_chambers() if c["name"] == config["chamber_name"]
        )
        self.chamber_id = chamber["id"]
        self._set("chamber", "ok", f"{chamber['name']} added")

    def _gui_steps(self, mode) -> None:
        host = self.host
        if getattr(host, "agent_api", None) is None:
            host._toggle_agent_api(True, quiet=True)
        if getattr(host, "agent_api", None) is None:
            self._set("agent_api", "fail", "didn't start (port 8765 in use?)")
        else:
            self._set("agent_api", "ok", "the run supervisor checks every command")
        host.set_ai_mode(mode)

    def _wait_connected(self, config) -> None:
        links = self.host.plc_links
        chamber = links.resolve(self.chamber_id)
        client = links.client(chamber, wait=False)
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while time.monotonic() < deadline:
            snap = client.snapshot()
            if snap.connected and snap.values and snap.settings:
                client.send_text_cmd(f"sim_speed {config['speed']}")
                self._set(
                    "connected", "ok", f"{chamber['name']}: {snap.values.get('temp', 0):.1f} °C"
                )
                return
            time.sleep(0.3)
        raise RuntimeError("the chamber didn't connect")

    def _check_ai(self, mode, config) -> None:
        if mode == ai_mode.OFFLINE:
            self._set("ai", "pending", "starting the local model…")
            model = self.host.local_model
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and model.status in ("stopped", "starting"):
                time.sleep(0.5)
            if model.status in ("ready", "running elsewhere"):
                self._set("ai", "ok", "offline: local model on this PC")
            else:
                self._set("ai", "warn", f"local model {model.status}: the rules recommend")
        elif n8n_answers(config["n8n_url"]):
            self._set("ai", "ok", "online: the Deepvac Agents on n8n")
        else:
            self._set(
                "ai", "warn", "n8n doesn't answer: no chat; the rules recommend, templates report"
            )

    def stop(self) -> None:
        if self.process is not None:
            with contextlib.suppress(OSError):
                self.process.kill()
            self.process = None
        keep_awake(False)
        self.running = False
        for key in self.steps:
            self._set(key, "pending")
