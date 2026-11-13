"""Recommendations at campaign decision points."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request

import app.common as common
from app.services import ai_mode
from app.services import campaign_service as cs

log = logging.getLogger(__name__)

CONFIG_FILE = "hold_advisor.json"
DEFAULTS = {"model": "local", "url": "http://127.0.0.1:8089/v1", "timeout_s": 45.0}
MODELS = ("local", "rules")
RULES_BY = "Analyst (rules)"
LOCAL_BY = "Analyst (local model)"
MODEL_KINDS = (
    "oscillation",
    "anomaly",
    "stabilise",
    "interrupted",
    "pump_slow",
    "pressure",
    "sensor",
)


def config_path():
    return common.DATA_DIR / CONFIG_FILE


def load_config() -> dict:
    config = dict(DEFAULTS)
    with contextlib.suppress(OSError, ValueError):
        config.update(json.loads(config_path().read_text(encoding="utf-8")))
    mode = ai_mode.load()
    config["model"] = "local" if mode["mode"] == ai_mode.OFFLINE else "rules"
    config["url"] = mode["local_url"]
    if os.environ.get("DEEPVAC_HOLD_ADVISOR"):
        config["model"] = os.environ["DEEPVAC_HOLD_ADVISOR"]
    if config.get("model") not in MODELS:
        config["model"] = "rules"
    return config


def _gains_text(g):
    return f"P {g['p']:g}, I {g['i']:g}, D {g['d']:g}"


def _gains_differ(a, b):
    return any(abs(float(a[k]) - float(b[k])) > 1e-6 for k in ("p", "i", "d"))


def rule(kind: str, facts: dict) -> tuple[str, str] | None:
    """Returns the rules' recommendation for a decision point, or None."""
    band = facts.get("band_c")
    temp = facts.get("temp_c")
    target = facts.get("target_c")
    if kind == "oscillation":
        in_use, operator = facts.get("gains_in_use"), facts.get("operator_gains")
        swing = (
            f"The chamber swings {facts['peak_to_peak_c']:.1f} °C peak to peak with "
            f"{facts['crossings']} setpoint crossings"
            if facts.get("peak_to_peak_c") is not None
            else "The chamber oscillates around the setpoint"
        )
        if in_use and operator and _gains_differ(in_use, operator):
            return (
                "revert_pid",
                f"{swing}. PID band {facts.get('pid_band', '?')} runs {_gains_text(in_use)} instead of the "
                f"operator's {_gains_text(operator)}: controller tuning. Restore the operator's table.",
            )
        if band is not None and facts.get("peak_to_peak_c", 0) > 2 * band:
            return "abort", f"{swing}, wider than the ±{band:g} °C band allows. Return to ambient."
        return (
            "accept",
            f"{swing}, inside the ±{band:g} °C band"
            + (", with the operator's own gains" if in_use and operator else "")
            + ": tuning isn't the obvious cause. Continue and log it; check the sensor and heater after the test.",
        )
    if kind == "anomaly":
        if temp is None or target is None or band is None:
            return None
        off = temp - target
        if abs(off) > 3 * band:
            return (
                "abort",
                f"The chamber reads {temp:.1f} °C, {off:+.1f} °C from the target: far beyond the ±{band:g} °C "
                "band. Return to ambient and find the cause before going on.",
            )
        return (
            "extend_dwell",
            f"The chamber reads {temp:.1f} °C, {off:+.1f} °C from the target (band ±{band:g} °C). Holding the "
            "plateau longer shows whether it recovers; the excursion stays logged.",
        )
    if kind == "stabilise" and facts.get("sensors") and target is not None and band is not None:
        sensors = facts["sensors"]
        worst = max(sensors, key=lambda s: abs(s["temp_c"] - target))
        off = abs(worst["temp_c"] - target)
        rate = worst.get("rate_c_per_h")
        towards = rate is not None and (target - worst["temp_c"]) * rate > 0 and abs(rate) > 0.5
        reading = f"{worst['name']} reads {worst['temp_c']:.1f} °C, {off:.1f} °C from the target"
        if towards:
            return (
                "wait",
                f"{reading} and still approaching it at {rate:+.1f} °C/h: the test article is slower "
                "than the shroud. Give it more time.",
            )
        if off <= 2 * band:
            return (
                "start_dwell",
                f"{reading}, settled just outside the ±{band:g} °C band. Start the dwell; it is "
                "logged as a deviation.",
            )
        return (
            "abort",
            f"{reading} and isn't approaching: check the test article's mounting and the "
            "thermocouples before going on.",
        )
    if kind == "pressure":
        p, limit = facts.get("pressure"), facts.get("hold_limit")
        resume, held = facts.get("resume_limit"), facts.get("held_min")
        if not p or not limit:
            return None
        if p > 10 * limit:
            return (
                "abort",
                f"The gauge reads {p:.1e} Torr, ten times the {limit:.1e} limit, after {held} min "
                "held: more than outgassing. Return to ambient and check for a leak.",
            )
        return (
            "wait",
            f"The gauge reads {p:.1e} Torr after {held} min held, above the {resume:.1e} resume "
            "limit but within ten times the hold limit: outgassing still coming off. Keep holding.",
        )
    if kind == "sensor":
        remaining = facts.get("remaining_stabilisation_sensors") or []
        reason = facts.get("reason", "")
        name = facts.get("sensor", "The sensor")
        if "spiking" in reason:
            return (
                "recheck",
                f"{name} is spiking: often a loose connector. Check it again; if it keeps "
                "spiking, exclude it.",
            )
        if remaining:
            return (
                "exclude",
                f"{name} is invalid ({reason.split(':')[-1].strip() or 'see the log'}) and the "
                f"criterion still has {', '.join(remaining)}. Exclude it and log it; repair it "
                "after the test.",
            )
        return (
            "abort",
            f"{name} is invalid and no other stabilisation sensor is left on the test article: "
            "the soak can't be shown. Return to ambient and repair it.",
        )
    if kind == "stabilise":
        if temp is None or target is None or band is None:
            return None
        off = abs(temp - target)
        before = facts.get("temp_5min_ago_c")
        if before is not None and abs(before - target) - off > 0.2:
            return (
                "wait",
                f"Still approaching: {abs(before - target):.1f} °C from the target 5 min ago, {off:.1f} °C now. "
                "Give it more time.",
            )
        if off <= 2 * band:
            return (
                "start_dwell",
                f"The chamber settled {off:.1f} °C from the target, just outside the ±{band:g} °C band. Start "
                "the dwell; it is logged as a deviation.",
            )
        return (
            "abort",
            f"The chamber stays {off:.1f} °C from the target and isn't approaching: return to ambient and "
            "check the chamber.",
        )
    if kind == "interrupted":
        status = facts.get("status")
        if status == "alarm":
            return (
                "ambient",
                "The chamber is in alarm: confirm it at the chamber, then bring it back to ambient.",
            )
        if facts.get("connected") and status in ("idle", "finish", "work"):
            return (
                "resume",
                f"The chamber is connected and {status}: resending the step continues the campaign.",
            )
        return None
    if kind == "pump_slow":
        now_p, before = facts.get("pressure"), facts.get("pressure_30min_ago")
        threshold, unit = facts.get("pressure_threshold"), facts.get("pressure_unit", "Torr")
        if not now_p or not threshold:
            return None
        if before and before > now_p * 1.5:
            return (
                "wait",
                f"Still pumping down: {before:.1e} {unit} 30 min ago, {now_p:.1e} now, towards "
                f"{threshold:.1e}. Give it more time.",
            )
        return (
            "abort",
            f"The pressure stays at {now_p:.1e} {unit}, above {threshold:.1e}, and isn't falling: "
            "check the chamber for a leak or a pump fault before heating or cooling.",
        )
    if kind == "vent":
        low, high = facts.get("vent_band_c") or (None, None)
        if temp is not None and low is not None and low <= temp <= high:
            return (
                "approve",
                f"The chamber reads {temp:.1f} °C, inside the vent window {low:g} to {high:g} °C.",
            )
        return None
    return None


PROMPT = """You are the Analyst of a thermal vacuum test campaign. The run supervisor stopped the
campaign at a decision point and a person will decide. Recommend one option.

Decision point: {title}
{detail}

Facts (JSON): {facts}

Options:
{options}

The rules suggest: {suggestion}

Reply with only a JSON object, no other text:
{{"option": "one of: {keys}", "rationale": "at most 40 words, quoting the numbers in the facts"}}"""


def _ask_local(config, hold, facts, suggestion):
    """Asks the local model for a recommendation."""
    options = "\n".join(f"- {o['key']}: {o['label']}. {o['help']}" for o in hold["options"])
    prompt = PROMPT.format(
        title=hold["title"],
        detail=hold.get("detail", ""),
        facts=json.dumps(facts, ensure_ascii=False),
        options=options,
        suggestion=f"{suggestion[0]} ({suggestion[1]})" if suggestion else "nothing",
        keys=", ".join(o["key"] for o in hold["options"]),
    )
    body = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 220,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        config["url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(config["timeout_s"])) as response:
            reply = json.loads(response.read().decode("utf-8"))
        text = reply["choices"][0]["message"]["content"]
    except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError) as exc:
        log.info("hold advisor: no answer from the local model (%s)", exc)
        return None
    return parse_answer(text, [o["key"] for o in hold["options"]])


def parse_answer(text: str, keys: list[str]) -> tuple[str, str] | None:
    """The model's {"option", "rationale"}, if it names an offered option."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        answer = json.loads(match.group(0))
    except ValueError:
        return None
    option = str(answer.get("option", "")).strip()
    rationale = str(answer.get("rationale", "")).strip()
    if option not in keys or not rationale:
        return None
    return option, rationale


def _refine(campaign_id, hold, facts, suggestion, config):
    answer = _ask_local(config, hold, facts, suggestion)
    if answer is None:
        return
    current = cs.open_hold_of(campaign_id)
    if current is None or current["id"] != hold["id"]:
        return
    rec = current["recommendation"]
    if rec and rec["by"] != RULES_BY:
        return
    with contextlib.suppress(cs.CampaignError):
        cs.recommend(campaign_id, answer[0], answer[1], by=LOCAL_BY, hold_id=hold["id"])


def advise(campaign_id, hold, facts, config=None, background=True):
    """Recommends an option at a decision point."""
    config = config or load_config()
    suggestion = rule(hold["kind"], facts)
    if suggestion:
        try:
            cs.recommend(campaign_id, suggestion[0], suggestion[1], by=RULES_BY, hold_id=hold["id"])
        except cs.CampaignError:
            return
    if config.get("model") != "local" or hold["kind"] not in MODEL_KINDS:
        return
    if background:
        threading.Thread(
            target=_refine,
            args=(campaign_id, hold, facts, suggestion, config),
            name="hold-advisor",
            daemon=True,
        ).start()
    else:
        _refine(campaign_id, hold, facts, suggestion, config)
