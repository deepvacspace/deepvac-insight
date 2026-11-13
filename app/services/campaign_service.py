"""Test campaigns: specification, lifecycle, decision points and signed log."""

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import app.common as common
from app.services.pressure import hpa_to_torr

DB_NAME = "deepvac_campaigns.sqlite3"
PACK_FILE = "standards_pack.json"

DRAFT = "draft"
APPROVAL = "approval"
RUNNING = "running"
HOLDING = "holding"
COMPLETED = "completed"
ABORTED = "aborted"
DECLINED = "declined"
CANCELLED = "cancelled"
STATES = (DRAFT, APPROVAL, RUNNING, HOLDING, COMPLETED, ABORTED, DECLINED, CANCELLED)
ACTIVE = (APPROVAL, RUNNING, HOLDING)
ENDED = (COMPLETED, ABORTED)
CLOSED = (COMPLETED, ABORTED, DECLINED, CANCELLED)

STAGES = ("requirement", "plan", "approval", "run", "report")
LEVELS = ("qualification", "acceptance")
ZERO_HASH = "0" * 64


def _tc(name, tau_min, offset_c, gradient_c, platen_weight, stabilisation=False):
    return {
        "name": name,
        "stabilisation": stabilisation,
        "model": {
            "tau_min": tau_min,
            "offset_c": offset_c,
            "gradient_c": gradient_c,
            "platen_weight": platen_weight,
        },
    }


GENERIC_THERMOCOUPLES = [
    _tc("Temperature 1", 20, 0.2, 0.3, 0.7, stabilisation=True),
    _tc("Temperature 2", 25, -0.2, -0.3, 0.75, stabilisation=True),
    _tc("Temperature 3", 15, 0.3, 0.4, 0.6, stabilisation=True),
    _tc("Temperature 4", 30, -0.1, -0.1, 0.8, stabilisation=True),
]

DEFAULT_PACK = {
    "pack_version": 3,
    "name": "ECSS-style demo pack",
    "standard": "ECSS-E-ST-10-03",
    "note": (
        "Illustrative values for the demo. A Deepvac engineer replaces them with the standard's values "
        "and clause numbers before any real test."
    ),
    "clause": "clause to confirm",
    "levels": {
        "qualification": {"margin_c": 10.0, "cycles": 4},
        "acceptance": {"margin_c": 5.0, "cycles": 2},
    },
    "single_ramp": {"margin_c": 10.0},
    "dwell_min": 240.0,
    "ramp_c_per_min": 2.0,
    "band_c": 3.0,
    "stabilisation": {"rate_c_per_h": 3.0, "window_min": 15.0},
    "pressure_rule": {"hold_hpa": 1e-4, "resume_hpa": 5e-5, "max_hold_min": 60.0},
    "sensor_rule": {
        "open_low_c": -200.0,
        "open_high_c": 400.0,
        "frozen_min": 5.0,
        "spike_c": 10.0,
        "spikes": 4,
        "spike_window_min": 10.0,
    },
    "stabilise_timeout_min": 180.0,
    "excursion_s": 120.0,
    "extend_dwell_min": 60.0,
    "extend_stabilise_min": 30.0,
    "vent_band_c": [10.0, 40.0],
    "ambient_c": 20.0,
    "articles": [
        {
            "name": "6U CubeSat",
            "op_min_c": -10.0,
            "op_max_c": 45.0,
        },
        {
            "name": "Reaction Wheel Assembly",
            "op_min_c": -20.0,
            "op_max_c": 50.0,
        },
        {
            "name": "Optical Camera Payload",
            "op_min_c": -15.0,
            "op_max_c": 40.0,
        },
        {
            "name": "Power Control and Distribution Unit",
            "op_min_c": -25.0,
            "op_max_c": 55.0,
        },
    ],
    "vacuum": "auto",
    "vacuum_start_hpa": 1e-5,
    "pump_timeout_min": 360.0,
    "rehearsal": {"dwell_min": 2.0, "stabilise_timeout_min": 15.0, "cycles": 1},
}

CYCLING, RAMP = "cycling", "ramp"
TEST_TYPES = (CYCLING, RAMP)
MAX_RAMP_C_PER_MIN = 20.0
MAX_CYCLES = 20
MAX_DWELL_MIN = 24 * 60.0
MIN_SPAN_C = 5.0
MAX_MARGIN_C = 30.0
OVERRIDABLE = ("cycles", "dwell_min", "ramp_c_per_min")
SETTLE_GUESS_MIN = 10.0

HOLD_OPTIONS = {
    "approval": [
        ("approve", "Approve and start", "The run supervisor starts the campaign on the chamber."),
        (
            "decline",
            "Decline",
            "Nothing is sent. The plan goes back to the Plan stage to be changed.",
        ),
    ],
    "pump": [
        (
            "confirm",
            "Pumped down: start cycling",
            "Confirm the chamber reached the start pressure; the first ramp starts.",
        ),
        ("abort", "Abort the campaign", "Nothing has been heated or cooled yet."),
    ],
    "pump_slow": [
        ("wait", "Wait longer", "Give the pumps more time to reach the start pressure."),
        (
            "confirm",
            "Start cycling anyway",
            "Start the first ramp above the start pressure; logged as a deviation in the report.",
        ),
        ("abort", "Abort the campaign", "Nothing has been heated or cooled yet."),
    ],
    "anomaly": [
        (
            "extend_dwell",
            "Extend the dwell",
            "Hold the plateau longer and see whether the temperature recovers.",
        ),
        (
            "accept",
            "Accept and continue",
            "Continue the dwell; the excursion is logged as a deviation in the report.",
        ),
        ("abort", "Abort and return to ambient", "Stop cycling; the chamber returns to ambient."),
    ],
    "oscillation": [
        (
            "revert_pid",
            "Revert to the operator's PID table",
            "A person makes this change: Insight writes the operator's PID gains back to the PLC. "
            "Agents can't change settings.",
        ),
        (
            "accept",
            "Keep the gains and continue",
            "Continue the dwell; the oscillation is logged as a deviation in the report.",
        ),
        ("abort", "Abort and return to ambient", "Stop cycling; the chamber returns to ambient."),
    ],
    "pressure": [
        (
            "wait",
            "Keep holding",
            "The temperature stays held until the pressure falls below the resume limit.",
        ),
        (
            "resume",
            "Resume anyway",
            "Continue above the pressure limit; logged as a deviation in the report.",
        ),
        ("abort", "Abort and return to ambient", "Stop cycling; the chamber returns to ambient."),
    ],
    "sensor": [
        (
            "exclude",
            "Exclude it and continue",
            "The stabilisation criterion uses the remaining sensors; logged as a deviation.",
        ),
        (
            "recheck",
            "Check it again",
            "The sensor is tested again in 2 min; if it is still invalid the decision point comes back.",
        ),
        ("abort", "Abort and return to ambient", "Stop cycling; the chamber returns to ambient."),
    ],
    "stabilise": [
        ("wait", "Wait longer", "Give the plateau more time to stabilise."),
        (
            "start_dwell",
            "Start the dwell anyway",
            "Start the dwell now; logged as a deviation in the report.",
        ),
        ("abort", "Abort and return to ambient", "Stop cycling; the chamber returns to ambient."),
    ],
    "interrupted": [
        ("resume", "Resend this step", "Send the current step to the chamber again."),
        ("ambient", "Return to ambient", "Abort the campaign and bring the chamber to ambient."),
        ("stop", "End the campaign now", "Stop the chamber's job; the chamber is left as it is."),
    ],
    "vent": [
        (
            "approve",
            "Approve venting",
            "The gateway checks the vent window again; then vent the chamber.",
        ),
    ],
}


class CampaignError(ValueError):
    pass


def db_path():
    return common.DATA_DIR / DB_NAME


def connect(path=None):
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            chamber_id INTEGER,
            chamber_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            requirement_json TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            state TEXT NOT NULL,
            runtime_json TEXT NOT NULL DEFAULT '{}',
            results_json TEXT NOT NULL DEFAULT '[]',
            deviations_json TEXT NOT NULL DEFAULT '[]',
            blocked_json TEXT NOT NULL DEFAULT '[]',
            started_at TEXT,
            ended_at TEXT,
            end_reason TEXT,
            summary TEXT,
            summary_source TEXT,
            report_files_json TEXT,
            report_issued_at TEXT,
            signed_off_by TEXT,
            signed_off_at TEXT,
            stop_request TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS holds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            options_json TEXT NOT NULL,
            recommendation_json TEXT,
            decided_at TEXT,
            decided_by TEXT,
            choice TEXT,
            comment TEXT,
            applied INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_holds_campaign ON holds(campaign_id, id);
        CREATE TABLE IF NOT EXISTS campaign_log (
            campaign_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            time TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            prev TEXT NOT NULL,
            hash TEXT NOT NULL,
            PRIMARY KEY (campaign_id, seq)
        );
        CREATE TABLE IF NOT EXISTS campaign_trace (
            campaign_id INTEGER NOT NULL,
            t REAL NOT NULL,
            temp REAL,
            setpoint REAL
        );
        CREATE INDEX IF NOT EXISTS idx_trace_campaign ON campaign_trace(campaign_id, t);
        CREATE TABLE IF NOT EXISTS tamper_demo (
            campaign_id INTEGER PRIMARY KEY,
            seq INTEGER NOT NULL,
            original TEXT NOT NULL
        );
        """
    )
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def iso(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat() if epoch_s else None


def epoch(iso_text):
    return datetime.fromisoformat(iso_text).timestamp() if iso_text else None


JSON_FIELDS = {
    "requirement_json": "requirement",
    "spec_json": "spec",
    "steps_json": "steps",
    "runtime_json": "runtime",
    "results_json": "results",
    "deviations_json": "deviations",
    "blocked_json": "blocked",
    "report_files_json": "report_files",
}


def _campaign(row):
    c = {}
    for key in row.keys():  # noqa: SIM118
        if key in JSON_FIELDS:
            c[JSON_FIELDS[key]] = json.loads(row[key]) if row[key] else None
        else:
            c[key] = row[key]
    return c


def _hold(row):
    h = dict(row)
    h["options"] = [
        {"key": k, "label": label, "help": help_text}
        for k, label, help_text in json.loads(h.pop("options_json"))
    ]
    rec = h.pop("recommendation_json")
    h["recommendation"] = json.loads(rec) if rec else None
    h["applied"] = bool(h["applied"])
    return h


def get_campaign(campaign_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (int(campaign_id),)).fetchone()
        return _campaign(row) if row else None
    finally:
        conn.close()


def require(campaign_id):
    campaign = get_campaign(campaign_id)
    if campaign is None:
        raise CampaignError(f"No campaign {campaign_id}.")
    return campaign


def latest_campaign():
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 1").fetchone()
        return _campaign(row) if row else None
    finally:
        conn.close()


def list_campaigns(limit=50, states=None):
    conn = connect()
    try:
        if states:
            rows = conn.execute(
                f"SELECT * FROM campaigns WHERE state IN ({','.join('?' * len(states))}) "
                "ORDER BY id DESC LIMIT ?",
                (*states, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM campaigns ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_campaign(r) for r in rows]
    finally:
        conn.close()


def update(campaign_id, expected_state=None, **fields):
    """Updates a campaign's fields; returns True when the row changed."""
    columns = {v: k for k, v in JSON_FIELDS.items()}
    sets, values = [], []
    for name, value in fields.items():
        if name in columns:
            sets.append(f"{columns[name]} = ?")
            values.append(json.dumps(value, ensure_ascii=False))
        else:
            sets.append(f"{name} = ?")
            values.append(value)
    sets.append("updated_at = ?")
    values.append(now_iso())
    sql = f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?"
    values.append(int(campaign_id))
    if expected_state is not None:
        sql += " AND state = ?"
        values.append(expected_state)
    conn = connect()
    try:
        cur = conn.execute(sql, values)
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


RENAMED_ARTICLES = {"6U CubeSat, Earth observation demo": "6U CubeSat"}
OLD_DEMO_ARTICLES = {
    "6U CubeSat",
    "6U CubeSat, Earth observation demo",
    "Star tracker unit",
    "Electric thruster module",
}


def load_pack():
    """Loads data/standards_pack.json, writing the demo pack on first use."""
    path = common.DATA_DIR / PACK_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_PACK, indent=2, ensure_ascii=False), encoding="utf-8")
        return copy.deepcopy(DEFAULT_PACK)
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CampaignError(f"{path} is not valid JSON: {exc}") from exc
    merged = copy.deepcopy(DEFAULT_PACK)
    merged.update(pack)
    for article in merged.get("articles") or []:
        if article.get("name") in RENAMED_ARTICLES:
            article["name"] = RENAMED_ARTICLES[article["name"]]
    if pack.get("pack_version", 1) < DEFAULT_PACK["pack_version"]:
        names = {a.get("name") for a in pack.get("articles") or []}
        if names <= OLD_DEMO_ARTICLES:
            merged["articles"] = copy.deepcopy(DEFAULT_PACK["articles"])
        if float(pack.get("stabilise_timeout_min", 0)) < DEFAULT_PACK["stabilise_timeout_min"]:
            merged["stabilise_timeout_min"] = DEFAULT_PACK["stabilise_timeout_min"]
        for article in merged.get("articles") or []:
            article.pop("thermocouples", None)
        merged["pack_version"] = DEFAULT_PACK["pack_version"]
    return merged


def article_of(pack, name):
    """The pack's test article of that name, or None."""
    return next((a for a in pack.get("articles") or [] if a.get("name") == name), None)


def thermocouples_of(pack, name=None):
    """Returns the test-article temperatures of the pack, the same for every article."""
    return copy.deepcopy(pack.get("thermocouples") or GENERIC_THERMOCOUPLES)


def sci(value):
    """1e-05 → "1×10⁻⁵", 7.5e-06 → "7.5×10⁻⁶"."""
    mantissa, exponent = f"{float(value):.1e}".split("e")
    sup = str(int(exponent)).translate(str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹"))
    mantissa = mantissa.rstrip("0").rstrip(".")
    return f"{mantissa}×10{sup}"


TORR_PER_HPA = 0.750062


def pressure_in(hpa, unit):
    """Converts a pressure from hPa to a PLC unit."""
    unit = (unit or "Torr").strip().lower()
    if unit == "torr":
        return hpa * TORR_PER_HPA
    if unit == "pa":
        return hpa * 100.0
    return hpa


def chamber_now(runtime, wall=None):
    """Returns the chamber clock now."""
    import time as _time

    wall = wall or _time.time()
    clock = (runtime or {}).get("clock")
    if not clock:
        return wall
    from app.services import plc_gate

    return plc_gate.plc_clock_now(
        clock["plc"], clock["wall"], clock.get("rate", 1.0), wall, max_gap_s=5.0
    )


def deg(value):
    r = round(float(value), 1)
    return f"{r:+g} °C" if r else "0 °C"


def _requested(value, default, kind):
    """Returns a requested number, else the default."""
    if value in (None, ""):
        return kind(default)
    try:
        return kind(float(value))
    except (TypeError, ValueError) as exc:
        raise CampaignError(f"{value!r} is not a number.") from exc


def derive_spec(requirement, pack, limits, overrides=None):
    """Test specification of a requirement within the chamber's limits, with optional overrides."""
    level = requirement.get("level") or "qualification"
    if level not in pack["levels"]:
        raise CampaignError(f"level must be one of {', '.join(pack['levels'])}.")
    op_min, op_max = float(requirement["op_min_c"]), float(requirement["op_max_c"])
    if op_min >= op_max:
        raise CampaignError("The lowest predicted temperature must be below the highest.")
    if op_max - op_min < MIN_SPAN_C:
        raise CampaignError(f"The predictions should be at least {MIN_SPAN_C:g} °C apart.")
    clause = f"{pack['standard']}, {pack['clause']}"
    lv = pack["levels"][level]
    ramp = (requirement.get("test_type") or CYCLING) == RAMP
    if ramp:
        default_margin = float((pack.get("single_ramp") or {}).get("margin_c", lv["margin_c"]))
        default_from = f"{clause}, single ramp"
    else:
        default_margin = float(lv["margin_c"])
        default_from = f"{clause}, {level} level"
    margin = _requested(requirement.get("margin_c"), default_margin, float)
    if not 0 <= margin <= MAX_MARGIN_C:
        raise CampaignError(f"The margin must be between 0 and {MAX_MARGIN_C:g} °C.")
    margin_from = (
        default_from
        if margin == default_margin
        else f"test requirements; the default is {default_margin:g} °C"
    )
    default_cycles = int(lv["cycles"])
    cycles = default_cycles if ramp else _requested(requirement.get("cycles"), default_cycles, int)
    cycles_from = (
        f"{clause}, {level} level"
        if cycles == default_cycles
        else f"Test requirements; the {level} level's default is {default_cycles}"
    )
    spec = {
        "article": requirement["article"],
        "level": level,
        "op_min_c": op_min,
        "op_max_c": op_max,
        "margin_c": margin,
        "hot_c": op_max + margin,
        "cold_c": op_min - margin,
        "cycles": cycles,
        "dwell_min": float(pack["dwell_min"]),
        "ramp_c_per_min": float(pack["ramp_c_per_min"]),
        "band_c": float(pack["band_c"]),
        "stabilise_timeout_min": float(pack["stabilise_timeout_min"]),
        "excursion_s": float(pack["excursion_s"]),
        "extend_dwell_min": float(pack["extend_dwell_min"]),
        "extend_stabilise_min": float(pack["extend_stabilise_min"]),
        "vent_band_c": [float(v) for v in pack["vent_band_c"]],
        "ambient_c": float(pack["ambient_c"]),
        "test_type": requirement.get("test_type") or CYCLING,
        "direction": requirement.get("direction") or "up",
        "vacuum": pack.get("vacuum", "auto"),
        "vacuum_start_hpa": float(pack.get("vacuum_start_hpa", 1e-5)),
        "pump_timeout_min": float(pack.get("pump_timeout_min", 360.0)),
        "thermocouples": thermocouples_of(pack, requirement["article"]),
        "stabilisation": dict(DEFAULT_PACK["stabilisation"], **(pack.get("stabilisation") or {})),
        "pressure_rule": dict(DEFAULT_PACK["pressure_rule"], **(pack.get("pressure_rule") or {})),
        "sensor_rule": dict(DEFAULT_PACK["sensor_rule"], **(pack.get("sensor_rule") or {})),
        "rehearsal": bool(requirement.get("rehearsal")),
        "pack": {"name": pack["name"], "standard": pack["standard"], "note": pack["note"]},
        "sources": {
            "hot_c": f"Highest predicted {deg(op_max)} plus a {margin:g} °C margin ({margin_from})",
            "cold_c": f"Lowest predicted {deg(op_min)} minus a {margin:g} °C margin ({margin_from})",
            "cycles": cycles_from,
            "dwell_min": clause,
            "band_c": clause,
            "ramp_c_per_min": f"Chamber limit and {clause}",
            "stabilise_timeout_min": "Deepvac chamber rule",
            "excursion_s": "Deepvac chamber rule",
            "vent_band_c": "Deepvac chamber rule",
            "vacuum_start_hpa": clause,
            "pump_timeout_min": "Deepvac chamber rule",
            "thermocouples": "Deepvac chamber configuration",
            "stabilisation": f"Test procedure and {clause}",
            "pressure_rule": "Test procedure: approved with the plan",
            "sensor_rule": "Deepvac chamber rule",
        },
        "overrides": [],
    }
    if spec["test_type"] not in TEST_TYPES:
        raise CampaignError(f"test_type must be one of {', '.join(TEST_TYPES)}.")
    if spec["direction"] not in ("up", "down"):
        raise CampaignError('direction must be "up" or "down".')
    if spec["test_type"] == RAMP:
        spec["cycles"] = 1
        spec["sources"]["cycles"] = "Single ramp: one plateau"
    if spec["rehearsal"]:
        for name, value in pack["rehearsal"].items():
            spec[name] = type(spec[name])(value)
            spec["sources"][name] = "Rehearsal: shortened to try the campaign out, not a test value"
    overrides = dict(overrides or {})
    reason = (overrides.pop("reason", "") or "").strip()
    by = overrides.pop("by", "") or "the planner"
    for name, value in overrides.items():
        if value is None:
            continue
        if name not in OVERRIDABLE:
            raise CampaignError(
                f"Only {', '.join(OVERRIDABLE)} can be changed from the standards pack."
            )
        if not reason:
            raise CampaignError(f"Give the reason for changing {name} from the standards pack.")
        old = spec[name]
        spec[name] = type(old)(value)
        spec["sources"][name] = f"Changed by {by} from {old:g}: {reason}"
        spec["overrides"].append({"field": name, "from": old, "to": spec[name], "reason": reason})
    if not 1 <= spec["cycles"] <= MAX_CYCLES:
        raise CampaignError(f"cycles must be between 1 and {MAX_CYCLES}.")
    if not 0 < spec["dwell_min"] <= MAX_DWELL_MIN:
        raise CampaignError(f"The dwell must be between 0 and {MAX_DWELL_MIN:g} minutes.")
    if not 0 < spec["ramp_c_per_min"] <= MAX_RAMP_C_PER_MIN:
        raise CampaignError(f"The ramp must be between 0 and {MAX_RAMP_C_PER_MIN:g} °C/min.")
    low, high = limits
    for name in ("hot_c", "cold_c", "ambient_c"):
        if not low <= spec[name] <= high:
            raise CampaignError(
                f"The {name.split('_')[0]} plateau {deg(spec[name])} is outside the chamber's setpoint range "
                f"{low:g} … {high:g} °C."
            )
    return spec


def build_steps(spec):
    steps = []
    if spec["vacuum"] == "manual":
        steps.append({"kind": "pump", "label": "Pump down (the operator confirms)"})
    elif spec["vacuum"] == "auto":
        steps.append(
            {
                "kind": "pump",
                "label": f"Pump down to {sci(hpa_to_torr(spec.get('vacuum_start_hpa', 1e-5)))} Torr",
            }
        )
    for c in range(1, spec["cycles"] + 1):
        for plateau, target in plateaus(spec):
            steps.append(
                {
                    "kind": "ramp",
                    "target_c": target,
                    "plateau": plateau,
                    "cycle": c,
                    "label": f"Cycle {c}: ramp to {deg(target)}",
                }
            )
            steps.append(
                {
                    "kind": "dwell",
                    "target_c": target,
                    "plateau": plateau,
                    "cycle": c,
                    "label": f"Cycle {c}: {plateau} dwell",
                }
            )
    steps.append({"kind": "ambient", "target_c": spec["ambient_c"], "label": "Return to ambient"})
    if spec["vacuum"] in ("manual", "auto"):
        steps.append({"kind": "vent", "label": "Vent the chamber (operator approval)"})
    return steps


def spec_rows(spec):
    """Returns the rows of a specification."""
    s = spec["sources"]
    vac = {
        "manual": "Pump-down and venting confirmed by the operator at decision points (no vacuum gauge connected)",
        "auto": (
            f"Cycling starts once the chamber's gauge reads {sci(hpa_to_torr(spec.get('vacuum_start_hpa', 1e-5)))} Torr "
            f"or less (at most {spec.get('pump_timeout_min', 360.0):g} min, then a decision point); without a "
            "gauge the operator confirms. Venting approved by the operator"
        ),
    }.get(spec["vacuum"], "Not part of this campaign")
    ramp = spec.get("test_type") == RAMP
    shown = {plateau for plateau, _ in plateaus(spec)}
    test = f"Single ramp {spec.get('direction', 'up')}, for accuracy" if ramp else "Thermal cycling"
    rows = [
        ("Test article", spec["article"], "Customer requirement"),
        ("Test type", test, "Customer requirement"),
    ]
    if not ramp:
        rows.insert(1, ("Test level", spec["level"].capitalize(), "Customer requirement"))
    if "hot" in shown:
        rows.append(("Hot plateau", deg(spec["hot_c"]), s["hot_c"]))
    if "cold" in shown:
        rows.append(("Cold plateau", deg(spec["cold_c"]), s["cold_c"]))
    if not ramp:
        rows.append(("Thermal cycles", str(spec["cycles"]), s["cycles"]))
    tcs = spec.get("thermocouples")
    if tcs:
        stab = spec.get("stabilisation") or DEFAULT_PACK["stabilisation"]
        names = stabilisation_names(spec)
        which = "Every valid test-article temperature" if len(names) == len(tcs) else _and(names)
        stabilisation = (
            f"{which} within ±{spec['band_c']:g} °C of the plateau, changing less than "
            f"{stab['rate_c_per_h']:g} °C/h over {stab['window_min']:g} min: then the dwell starts"
        )
        stab_from = s.get("stabilisation", s["band_c"])
    else:
        stabilisation = f"Chamber temperature within ±{spec['band_c']:g} °C"
        stab_from = s["band_c"]
    rows += [
        ("Dwell per plateau", f"{spec['dwell_min']:g} min", s["dwell_min"]),
        ("Stabilisation", stabilisation, stab_from),
        ("Ramp rate", f"At most {spec['ramp_c_per_min']:g} °C/min", s["ramp_c_per_min"]),
        (
            "Stabilisation timeout",
            f"{spec['stabilise_timeout_min']:g} min, then a decision point",
            s["stabilise_timeout_min"],
        ),
        (
            "Excursion",
            f"Out of the band for {spec['excursion_s']:g} s during a dwell: a decision point",
            s["excursion_s"],
        ),
        (
            "Vent window",
            f"{spec['vent_band_c'][0]:g} to {spec['vent_band_c'][1]:g} °C",
            s["vent_band_c"],
        ),
        (
            "Vacuum",
            vac,
            s.get("vacuum_start_hpa", "Deepvac chamber configuration")
            if spec["vacuum"] == "auto"
            else "Deepvac chamber configuration",
        ),
    ]
    rule = spec.get("pressure_rule")
    if rule and spec["vacuum"] == "auto":
        rows.append(
            (
                "Pressure rule",
                f"Above {sci(hpa_to_torr(rule['hold_hpa']))} Torr a heating ramp is held (a dwell's "
                f"clock paused) until the gauge reads {sci(hpa_to_torr(rule['resume_hpa']))} Torr; "
                f"held over {rule['max_hold_min']:g} min, a decision point",
                s.get("pressure_rule", "Test procedure"),
            )
        )
    sensor = spec.get("sensor_rule")
    if tcs and sensor:
        rows.append(
            (
                "Sensor validity",
                f"Open (outside {sensor['open_low_c']:g} … {sensor['open_high_c']:g} °C), frozen "
                f"(unchanged for {sensor['frozen_min']:g} min) or spiking ({sensor['spikes']} jumps "
                f"over {sensor['spike_c']:g} °C in {sensor['spike_window_min']:g} min): marked "
                "invalid. Only a person excludes a temperature from the stabilisation criterion",
                s.get("sensor_rule", "Deepvac chamber rule"),
            )
        )
    rows.append(
        (
            "Sensors",
            (
                f"Shroud (the PLC's control sensor), platen and {len(tcs)} temperatures on the "
                "test article"
                if tcs
                else "Chamber temperature (PLC)"
            ),
            "Deepvac chamber configuration",
        )
    )
    return rows


def _and(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names) or "The chamber temperature"
    return ", ".join(names[:-1]) + " and " + names[-1]


def stabilisation_names(spec):
    """Returns the names of the stabilisation sensors."""
    return [t["name"] for t in spec.get("thermocouples") or [] if t.get("stabilisation")]


def plateaus(spec):
    """Returns the plateaus of one cycle."""
    if spec.get("test_type") == RAMP:
        if spec.get("direction") == "down":
            return [("cold", spec["cold_c"])]
        return [("hot", spec["hot_c"])]
    return [("hot", spec["hot_c"]), ("cold", spec["cold_c"])]


def spec_line(spec):
    if spec.get("test_type") == RAMP:
        ((plateau, target),) = plateaus(spec)
        return (
            f"single ramp {'up' if plateau == 'hot' else 'down'} to {deg(target)}, "
            f"{spec['dwell_min']:g} min dwell, ramp ≤ {spec['ramp_c_per_min']:g} °C/min"
        )
    return (
        f"{spec['cycles']} cycles, {deg(spec['cold_c'])} to {deg(spec['hot_c'])}, "
        f"{spec['dwell_min']:g} min dwells, ramp ≤ {spec['ramp_c_per_min']:g} °C/min"
    )


def estimate_hours(spec):
    minutes = 0.0
    temp = spec["ambient_c"]
    for _ in range(spec["cycles"]):
        for _plateau, target in plateaus(spec):
            minutes += abs(target - temp) / spec["ramp_c_per_min"] + SETTLE_GUESS_MIN
            minutes += spec["dwell_min"]
            temp = target
    minutes += abs(spec["ambient_c"] - temp) / spec["ramp_c_per_min"] + SETTLE_GUESS_MIN
    return round(minutes / 60.0, 2)


def draft(requirement, chamber, created_by, limits, overrides=None, actor="Planner"):
    """Creates a campaign in the plan stage on a chambers_service row."""
    article = (requirement.get("article") or "").strip()
    if not article:
        raise CampaignError("Name the test article.")
    requirement = {
        "article": article,
        "op_min_c": float(requirement["op_min_c"]),
        "op_max_c": float(requirement["op_max_c"]),
        "level": requirement.get("level") or "qualification",
        "rehearsal": bool(requirement.get("rehearsal")),
        "test_type": requirement.get("test_type") or CYCLING,
        "direction": requirement.get("direction") or "up",
        "notes": (requirement.get("notes") or "").strip(),
        "margin_c": requirement.get("margin_c"),
        "cycles": requirement.get("cycles"),
    }
    overrides = dict(overrides or {})
    overrides.setdefault("by", created_by)
    spec = derive_spec(requirement, load_pack(), limits, overrides)
    steps = build_steps(spec)
    now = now_iso()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO campaigns (created_at, created_by, chamber_id, chamber_name, title, requirement_json, "
            "spec_json, steps_json, state, runtime_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                created_by,
                chamber["id"],
                chamber["name"],
                article,
                json.dumps(requirement, ensure_ascii=False),
                json.dumps(spec, ensure_ascii=False),
                json.dumps(steps, ensure_ascii=False),
                DRAFT,
                json.dumps({"step_index": -1}),
                now,
            ),
        )
        conn.commit()
        campaign_id = cur.lastrowid
    finally:
        conn.close()
    record(
        campaign_id,
        actor,
        "Specification drafted",
        f"{article}{'' if spec['test_type'] == RAMP else ', ' + spec['level']}"
        f"{' (rehearsal)' if spec['rehearsal'] else ''}: {spec_line(spec)}"
        + "".join(f"; {o['field']} changed: {o['reason']}" for o in spec["overrides"]),
    )
    return get_campaign(campaign_id)


def submit(campaign_id, actor="Planner"):
    """Moves a plan to approval and opens the approval decision point."""
    campaign = require(campaign_id)
    if campaign["state"] != DRAFT:
        raise CampaignError(f"Campaign {campaign_id} is {campaign['state']}, not a draft.")
    busy = [
        c
        for c in list_campaigns(20, ACTIVE)
        if c["chamber_id"] == campaign["chamber_id"] and c["id"] != campaign["id"]
    ]
    if busy:
        raise CampaignError(
            f"Campaign {busy[0]['id']} is already {busy[0]['state']} on {campaign['chamber_name']}."
        )
    if not update(campaign_id, DRAFT, state=APPROVAL):
        raise CampaignError(f"Campaign {campaign_id} changed meanwhile.")
    spec = campaign["spec"]
    record(campaign_id, actor, "Submitted for approval", spec_line(spec))
    open_hold(
        campaign_id,
        "approval",
        "Approve the test specification and start the campaign?",
        f"You're approving {spec_line(spec)} on the {spec['article']}, on {campaign['chamber_name']}; "
        f"about {estimate_hours(spec):g} h.",
    )
    return require(campaign_id)


def cancel(campaign_id, who, reason=""):
    """Withdraws a campaign that hasn't started."""
    campaign = require(campaign_id)
    if campaign["state"] not in (DRAFT, APPROVAL):
        raise CampaignError(
            f"Campaign {campaign_id} is {campaign['state']}: only a draft or a campaign waiting for approval "
            "can be cancelled. An operator stops a running one in the AI Agents page."
        )
    if not update(
        campaign_id, campaign["state"], state=CANCELLED, ended_at=now_iso(), end_reason=reason
    ):
        raise CampaignError(f"Campaign {campaign_id} changed meanwhile.")
    conn = connect()
    try:
        conn.execute(
            "UPDATE holds SET decided_at = ?, decided_by = ?, choice = 'cancelled', applied = 1 "
            "WHERE campaign_id = ? AND decided_at IS NULL",
            (now_iso(), who, int(campaign_id)),
        )
        conn.commit()
    finally:
        conn.close()
    record(campaign_id, who, "Campaign cancelled", reason)
    return require(campaign_id)


def end_unfinished(campaign_id, who, reason):
    """Ends an unfinished campaign at once: cancelled before it started, aborted after."""
    campaign = require(campaign_id)
    state = campaign["state"]
    if state in CLOSED:
        return campaign
    ended = CANCELLED if state in (DRAFT, APPROVAL) else ABORTED
    if not update(campaign_id, state, state=ended, ended_at=now_iso(), end_reason=reason):
        return require(campaign_id)
    conn = connect()
    try:
        conn.execute(
            "UPDATE holds SET decided_at = ?, decided_by = ?, choice = 'cancelled', applied = 1 "
            "WHERE campaign_id = ? AND decided_at IS NULL",
            (now_iso(), who, int(campaign_id)),
        )
        conn.commit()
    finally:
        conn.close()
    record(
        campaign_id,
        who,
        "Campaign cancelled" if ended == CANCELLED else "Campaign aborted",
        reason,
    )
    return require(campaign_id)


def request_stop(campaign_id, user_name, reason=""):
    """Asks the supervisor to stop a running campaign."""
    if not user_name:
        raise CampaignError("Only a signed-in user can stop a campaign.")
    campaign = require(campaign_id)
    if campaign["state"] not in (RUNNING, HOLDING):
        raise CampaignError(f"Campaign {campaign_id} is not running.")
    request = {"by": user_name, "reason": reason, "at": now_iso()}
    update(campaign_id, stop_request=json.dumps(request, ensure_ascii=False))
    record(campaign_id, user_name, "Stop requested", reason or "stop the campaign now")


def open_hold(campaign_id, kind, title, detail=""):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO holds (campaign_id, kind, opened_at, title, detail, options_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(campaign_id),
                kind,
                now_iso(),
                title,
                detail,
                json.dumps(HOLD_OPTIONS[kind], ensure_ascii=False),
            ),
        )
        conn.commit()
        hold_id = cur.lastrowid
    finally:
        conn.close()
    record(campaign_id, "Run supervisor", "Decision point raised", f"{title} {detail}".strip())
    return get_hold(hold_id)


def get_hold(hold_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM holds WHERE id = ?", (int(hold_id),)).fetchone()
        return _hold(row) if row else None
    finally:
        conn.close()


def holds(campaign_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM holds WHERE campaign_id = ? ORDER BY id", (int(campaign_id),)
        ).fetchall()
        return [_hold(r) for r in rows]
    finally:
        conn.close()


def open_hold_of(campaign_id):
    """The decision point waiting for the operator, if any."""
    return next((h for h in holds(campaign_id) if h["decided_at"] is None), None)


def open_holds_count():
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM holds h JOIN campaigns c ON c.id = h.campaign_id "
            f"WHERE h.decided_at IS NULL AND c.state IN ({','.join('?' * len(ACTIVE))})",
            ACTIVE,
        ).fetchone()[0]
    finally:
        conn.close()


def recommend(campaign_id, option, rationale, by="Professor", hold_id=None):
    """Records a recommendation on the open decision point."""
    hold = open_hold_of(campaign_id)
    if hold is None:
        raise CampaignError(f"Campaign {campaign_id} has no decision point waiting for a decision.")
    if hold_id is not None and hold["id"] != hold_id:
        raise CampaignError(
            f"Decision point {hold_id} of campaign {campaign_id} is no longer open."
        )
    keys = [o["key"] for o in hold["options"]]
    if option not in keys:
        raise CampaignError(f"option must be one of {', '.join(keys)}.")
    if not (rationale or "").strip():
        raise CampaignError("Give the rationale: the operator reads it before deciding.")
    rec = {"option": option, "rationale": rationale.strip()[:600], "by": by, "at": now_iso()}
    conn = connect()
    try:
        conn.execute(
            "UPDATE holds SET recommendation_json = ? WHERE id = ? AND decided_at IS NULL",
            (json.dumps(rec, ensure_ascii=False), hold["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    label = next(o["label"] for o in hold["options"] if o["key"] == option)
    record(campaign_id, by, "Recommendation", f"{hold['title']} {label}: {rec['rationale']}")
    return get_hold(hold["id"])


def decide_hold(hold_id, choice, user_name, comment=""):
    """Records a person's choice at a decision point."""
    if not user_name:
        raise CampaignError("Only a signed-in user can decide at a decision point.")
    hold = get_hold(hold_id)
    if hold is None:
        raise CampaignError(f"No decision point {hold_id}.")
    keys = [o["key"] for o in hold["options"]]
    if choice not in keys:
        raise CampaignError(f"choice must be one of {', '.join(keys)}.")
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE holds SET decided_at = ?, decided_by = ?, choice = ?, comment = ? "
            "WHERE id = ? AND decided_at IS NULL",
            (now_iso(), user_name, choice, comment or "", int(hold_id)),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise CampaignError("Someone already decided at this decision point.")
    finally:
        conn.close()
    label = next(o["label"] for o in hold["options"] if o["key"] == choice)
    rec = hold["recommendation"]
    record(
        hold["campaign_id"],
        user_name,
        "Decision made",
        f"{hold['title']} {label}"
        + (f" ({comment})" if comment else "")
        + (
            f"; {rec['by']} recommended {next(o['label'] for o in hold['options'] if o['key'] == rec['option'])}"
            if rec
            else ""
        ),
    )
    return get_hold(hold_id)


def decided_unapplied(campaign_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM holds WHERE campaign_id = ? AND decided_at IS NOT NULL AND applied = 0 ORDER BY id",
            (int(campaign_id),),
        ).fetchall()
        return [_hold(r) for r in rows]
    finally:
        conn.close()


def mark_applied(hold_id):
    """Marks a decision applied; returns True for the caller that claimed it."""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE holds SET applied = 1 WHERE id = ? AND applied = 0", (int(hold_id),)
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def decisions(campaign_id):
    """Decided decision points with time, choice and recommendation."""
    out = []
    for h in holds(campaign_id):
        if not h["decided_at"] or h["choice"] == "cancelled":
            continue
        labels = {o["key"]: o["label"] for o in h["options"]}
        rec = h["recommendation"]
        out.append(
            {
                "time": h["decided_at"],
                "hold": h["title"],
                "kind": h["kind"],
                "choice": labels.get(h["choice"], h["choice"]),
                "by": h["decided_by"],
                "comment": h["comment"] or "",
                "recommended": f"{labels.get(rec['option'], rec['option'])} ({rec['by']})"
                if rec
                else "",
            }
        )
    return out


def _canon(campaign_id, seq, time, actor, action, detail, prev):
    return json.dumps(
        [int(campaign_id), seq, time, actor, action, detail, prev],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def record(campaign_id, actor, action, detail=""):
    """Appends an entry to the campaign's chained log."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute(
            "SELECT seq, hash FROM campaign_log WHERE campaign_id = ? ORDER BY seq DESC LIMIT 1",
            (int(campaign_id),),
        ).fetchone()
        seq = last["seq"] + 1 if last else 1
        prev = last["hash"] if last else ZERO_HASH
        time = now_iso()
        digest = hashlib.sha256(
            _canon(campaign_id, seq, time, actor, action, detail, prev).encode("utf-8")
        ).hexdigest()
        conn.execute(
            "INSERT INTO campaign_log (campaign_id, seq, time, actor, action, detail, prev, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(campaign_id), seq, time, actor, action, detail, prev, digest),
        )
        conn.commit()
        return {"seq": seq, "hash": digest}
    finally:
        conn.close()


def log_entries(campaign_id):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM campaign_log WHERE campaign_id = ? ORDER BY seq", (int(campaign_id),)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_chain(campaign_id):
    """Checks the log's hash chain."""
    prev = ZERO_HASH
    entries = log_entries(campaign_id)
    for e in entries:
        digest = hashlib.sha256(
            _canon(
                campaign_id, e["seq"], e["time"], e["actor"], e["action"], e["detail"], prev
            ).encode("utf-8")
        ).hexdigest()
        if e["prev"] != prev or digest != e["hash"]:
            return {"ok": False, "entries": len(entries), "broken_at": e["seq"]}
        prev = e["hash"]
    return {"ok": True, "entries": len(entries), "last_hash": prev}


def sign(text):
    """Signs text with the device key."""
    from app.services import licensing_client

    private_key, public_raw = licensing_client.get_or_create_device_keypair()
    return private_key.sign(text.encode("utf-8")).hex(), public_raw.hex()


def add_trace(campaign_id, t, temp, setpoint):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO campaign_trace (campaign_id, t, temp, setpoint) VALUES (?, ?, ?, ?)",
            (int(campaign_id), float(t), temp, setpoint),
        )
        conn.commit()
    finally:
        conn.close()


def trace(campaign_id, since=None):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT t, temp, setpoint FROM campaign_trace WHERE campaign_id = ? AND t >= ? ORDER BY t",
            (int(campaign_id), float(since or 0)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stage_of(campaign):
    state = campaign["state"]
    if state == DRAFT:
        return "plan"
    if state == APPROVAL:
        return "approval"
    if state in (RUNNING, HOLDING):
        return "run"
    if state in ENDED:
        return "report"
    return "requirement"


def step_progress(campaign, now=None):
    """The current step and its progress."""
    runtime = campaign["runtime"] or {}
    now = now or chamber_now(runtime)
    index = runtime.get("step_index", -1)
    steps = campaign["steps"]
    if not 0 <= index < len(steps):
        return None
    step = dict(steps[index])
    step["index"] = index + 1
    step["of"] = len(steps)
    held = runtime.get("pressure_hold")
    if step["kind"] == "dwell":
        stabilised = runtime.get("stabilised_at")
        if stabilised is None:
            step["status"] = "stabilising" + (
                f": {runtime['stab_status']}" if runtime.get("stab_status") else ""
            )
        else:
            paused = runtime.get("dwell_paused_s", 0.0)
            if runtime.get("hold_paused_at"):
                paused += now - runtime["hold_paused_at"]
            elapsed = (now - stabilised - paused) / 60.0
            total = campaign["spec"]["dwell_min"] + runtime.get("dwell_extra_min", 0.0)
            step["status"] = f"dwell {elapsed:.0f} of {total:g} min"
            step["dwell_elapsed_min"] = round(elapsed, 1)
            step["dwell_total_min"] = total
            if held:
                step["status"] += ", paused (pressure)"
    elif held and step["kind"] in ("ramp", "ambient"):
        step["status"] = f"held at {deg(held['temp_c'])} (pressure)"
    elif step["kind"] in ("ramp", "ambient"):
        sent = runtime.get("job_sent_at")
        ramp_s = runtime.get("job_ramp_s", 0.0)
        if sent and ramp_s and now - sent < ramp_s:
            step["status"] = f"ramping, {(now - sent) / ramp_s * 100:.0f} %"
        elif sent and step["kind"] == "ambient":
            step["status"] = "settling at ambient"
        else:
            step["status"] = "settling" if step["kind"] == "ambient" else "starting"
    return step


def news_key(campaign):
    """Key that changes when the campaign's state or decision point changes."""
    hold = open_hold_of(campaign["id"])
    return (
        f"{campaign['state']}|{hold['id'] if hold else ''}|{campaign.get('report_issued_at') or ''}"
    )


def public(campaign, detail=True):
    """Campaign as agents and the chat see it."""
    spec = campaign["spec"]
    hold = open_hold_of(campaign["id"])
    out = {
        "id": campaign["id"],
        "title": campaign["title"],
        "chamber": campaign["chamber_name"],
        "state": campaign["state"],
        "stage": stage_of(campaign),
        "specification": spec_line(spec),
        "rehearsal": spec["rehearsal"],
        "created_by": campaign["created_by"],
        "created_at": campaign["created_at"],
        "news": news_key(campaign),
    }
    for key in ("started_at", "ended_at", "end_reason", "report_issued_at", "signed_off_by"):
        if campaign.get(key):
            out[key] = campaign[key]
    step = step_progress(campaign)
    if step:
        out["step"] = {
            k: step[k] for k in ("index", "of", "label", "status") if step.get(k) is not None
        }
    if hold:
        out["hold_point"] = {
            "id": hold["id"],
            "kind": hold["kind"],
            "title": hold["title"],
            "detail": hold["detail"],
            "options": [{"key": o["key"], "label": o["label"]} for o in hold["options"]],
            "recommendation": hold["recommendation"],
        }
    if not detail:
        return out
    out["estimated_hours"] = estimate_hours(spec)
    out["spec_rows"] = [{"item": i, "value": v, "source": s} for i, v, s in spec_rows(spec)]
    out["pack_note"] = spec["pack"]["note"]
    out["steps"] = [s["label"] for s in campaign["steps"]]
    last = trace(campaign["id"], (campaign["runtime"] or {}).get("last_trace_at", 0) - 1)
    if last:
        out["chamber_now"] = {"temperature_c": last[-1]["temp"], "setpoint_c": last[-1]["setpoint"]}
    out["results"] = campaign["results"]
    out["deviations"] = campaign["deviations"]
    out["decisions"] = decisions(campaign["id"])
    out["blocked_commands"] = campaign["blocked"]
    if campaign.get("report_files"):
        out["report"] = campaign["report_files"]
    return out


def tamper_demo(campaign_id):
    """Alters one signed log entry for the tampering demo."""
    entries = log_entries(campaign_id)
    if len(entries) < 3:
        raise CampaignError("The log needs a few entries before it can be tampered with.")
    entry = entries[len(entries) // 2]
    conn = connect()
    try:
        if conn.execute(
            "SELECT 1 FROM tamper_demo WHERE campaign_id = ?", (int(campaign_id),)
        ).fetchone():
            raise CampaignError("The log is already tampered with: undo it first.")
        conn.execute(
            "INSERT INTO tamper_demo (campaign_id, seq, original) VALUES (?, ?, ?)",
            (int(campaign_id), entry["seq"], entry["detail"]),
        )
        conn.execute(
            "UPDATE campaign_log SET detail = ? WHERE campaign_id = ? AND seq = ?",
            (entry["detail"] + " (edited)", int(campaign_id), entry["seq"]),
        )
        conn.commit()
    finally:
        conn.close()
    return entry["seq"]


def undo_tamper(campaign_id):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT seq, original FROM tamper_demo WHERE campaign_id = ?", (int(campaign_id),)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE campaign_log SET detail = ? WHERE campaign_id = ? AND seq = ?",
            (row["original"], int(campaign_id), row["seq"]),
        )
        conn.execute("DELETE FROM tamper_demo WHERE campaign_id = ?", (int(campaign_id),))
        conn.commit()
        return True
    finally:
        conn.close()


def tampered(campaign_id):
    conn = connect()
    try:
        return (
            conn.execute(
                "SELECT 1 FROM tamper_demo WHERE campaign_id = ?", (int(campaign_id),)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()
