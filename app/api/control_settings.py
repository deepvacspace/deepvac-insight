"""The chamber's configuration, imported from control2-client's settings.json
(scripts/import_control_data.py writes data/control_settings.json).

Gives labels and units of states, the program parameters a PLC job is made
of, setpoint limits, alarm labels and PLC setting labels.
"""

import json
import re

import app.common as common

FILE_NAME = "control_settings.json"

# when no settings were imported: the parameters every DeepVac PLC program has
DEFAULT_CONTROL_PARAMS = [
    {"name": "duration", "type": 256},
    {"name": "temp", "type": 0, "min": -75, "max": 150},
    {"name": "flags", "type": 257},
]
DEFAULT_ENABLE_FLAGS = 0b011  # channel-enable bits of default_program.flags


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
    """Channel-enable bits (0-2) of an interval, from the chamber's default program."""
    flags = (settings.get("default_program") or {}).get("flags")
    return int(flags) & 0x07 if flags is not None else DEFAULT_ENABLE_FLAGS


_UNIT = re.compile(r"^(?P<label>.*?),\s*(?P<unit>[^,]{1,8})$")


def state_meta(settings: dict) -> dict[str, dict]:
    """{state name: {label, unit}} from the "states" section ("Chamber temperature, °C")."""
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
