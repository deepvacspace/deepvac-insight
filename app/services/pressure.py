"""Pressure unit conversions to Torr."""

TORR_PER = {
    "torr": 1.0,
    "mbar": 0.750062,
    "hpa": 0.750062,
    "pa": 0.00750062,
    "bar": 750.062,
    "kpa": 7.50062,
    "atm": 760.0,
    "psi": 51.7149,
    "mtorr": 0.001,
}


def to_torr(value, unit="Torr"):
    """Converts a pressure to Torr."""
    if value is None:
        return None
    return float(value) * TORR_PER.get(str(unit or "Torr").strip().lower(), 1.0)


def hpa_to_torr(hpa):
    return None if hpa is None else float(hpa) * TORR_PER["hpa"]


def chamber_unit():
    """Returns the chamber's pressure unit."""
    try:
        from app.api import control_settings

        meta = control_settings.state_meta(control_settings.load()).get("pres") or {}
        return meta.get("unit") or "Torr"
    except Exception:
        return "Torr"
