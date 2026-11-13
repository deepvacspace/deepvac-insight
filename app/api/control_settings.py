"""The chamber configuration imported from Deepvac Control's settings."""

import json
import re

import app.common as common

FILE_NAME = "control_settings.json"

DEFAULT_CONTROL_PARAMS = [
    {"name": "duration", "type": 256, "label": "Duration", "value": 3600},
    {
        "name": "temp",
        "type": 0,
        "label": "Temperature",
        "min": -75,
        "max": 150,
        "prec": 1,
        "value": 25,
    },
    {"name": "flags", "type": 257, "label": "Flags"},
]
DEFAULT_ENABLE_FLAGS = 0b011


def load() -> dict:
    path = common.DATA_DIR / FILE_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def control_params(settings: dict) -> list[dict]:
    return settings.get("control_params") or DEFAULT_CONTROL_PARAMS


def temperature_limits(settings: dict) -> tuple[float, float]:
    for param in control_params(settings):
        if param.get("name") == "temp":
            return float(param.get("min", -75)), float(param.get("max", 150))
    return -75.0, 150.0


def enable_flags(settings: dict) -> int:
    """Returns the channel-enable bits of an interval."""
    flags = (settings.get("default_program") or {}).get("flags")
    return int(flags) & 0x07 if flags is not None else DEFAULT_ENABLE_FLAGS


_UNIT = re.compile(r"^(?P<label>.*?),\s*(?P<unit>[^,]{1,8})$")


def state_meta(settings: dict) -> dict[str, dict]:
    """Returns the label and unit of each state."""
    meta = {}
    for state in settings.get("states") or []:
        label = state.get("label") or state.get("name", "")
        match = _UNIT.match(label)
        meta[state["name"]] = {
            "label": match["label"] if match else label,
            "unit": match["unit"] if match else "",
        }
    return meta


def alarm_labels(settings: dict) -> dict[int, str]:
    return {
        int(a["index"]): a.get("label", "") for a in settings.get("alarms") or [] if "index" in a
    }


def setting_labels(settings: dict) -> dict[str, str]:
    return {
        s["name"]: s.get("label", "")
        for s in (settings.get("plc_settings") or {}).get("general") or []
        if "name" in s
    }


def temperature_pid(settings: dict) -> dict | None:
    """Returns the temperature PID table of the PLC settings."""
    tables = (settings.get("plc_settings") or {}).get("pids") or []
    labelled = [t for t in tables if "temp" in str(t.get("label", "")).lower()]
    chosen = labelled[-1] if labelled else (tables[0] if tables else None)
    if not chosen or not chosen.get("name") or not chosen.get("points"):
        return None
    name, points = str(chosen["name"]), int(chosen["points"])
    gains = {}
    for key in ("p", "i", "d"):
        values = chosen.get(key) or []
        for band in range(min(points, len(values))):
            gains[f"{name}{key}{band}"] = float(values[band])
    if len(gains) != 3 * points:
        return None
    return {
        "name": name,
        "label": chosen.get("label", ""),
        "min": float(chosen.get("min", 0.0)),
        "max": float(chosen.get("max", 0.0)),
        "points": points,
        "gains": gains,
    }


def pid_band(table: dict, setpoint: float) -> int:
    """0-based band of the table the PLC uses at this setpoint."""
    width = (table["max"] - table["min"]) / table["points"]
    if width <= 0:
        return 0
    return int(min(max((setpoint - table["min"]) // width, 0), table["points"] - 1))


def band_gains(table: dict, band: int) -> dict[str, float]:
    """{"p", "i", "d"} of one band of the table."""
    return {k: table["gains"][f"{table['name']}{k}{band}"] for k in ("p", "i", "d")}
