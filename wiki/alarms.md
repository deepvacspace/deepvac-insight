---
title: Alarms
type: reference
tags: [alarm, safety, pressure, overcurrent, overtemperature, r404a, r23]
sources: [control2-client settings.json (alarms, plc_settings.general), insight app/api/worker_tools.py, insight app/plc_control.py]
updated: 2026-10-24
---
# Alarms

How the PLC reports alarms, what each one means and what to do. The current alarm list (bit
and label) is in [[chamber-configuration]].

## How they show

- The PLC sets a bit in `alarm_out_status` for every active alarm, and `status_condition`
  becomes 4 (alarm). `alarm_in_status` holds the trigger conditions.
- Insight names the active alarms on the Controller page and in the worker's live status;
  DeepVac Control logs them ("PLC" messages in its event log).
- Whether an alarm is processed and how it triggers is set by the PLC settings `alarm_cfg_1`
  and `alarm_cfg_2` (bit masks).

## The alarms

| Alarm | Likely meaning | First checks |
|---|---|---|
| R404A circuit pressure exceeded | high-stage refrigerant pressure too high | condenser airflow / cooling water, ambient temperature, compressor cycling |
| R23 circuit pressure exceeded | low-stage pressure too high (`pres_23_high`) | R404A stage working (it cools the R23 condenser), start delay respected |
| Chamber volume fan overcurrent | the fan motor draws too much current | blocked fan, ice at low temperature |
| Chamber volume overtemperature (safety device) | the independent overtemperature protection tripped | setpoint vs DUT limits, heater stuck on; a safety device needs a person on site |
| Compressor overcurrent | a compressor draws too much current | repeated starts, refrigerant charge |
| Heater overcurrent | the heater draws too much current | heater element, wiring |
| Peripheral fan overcurrent | a peripheral fan draws too much current | the fan |

"Likely meaning" and "first checks" are general guidance for this kind of chamber; the service
documentation of the chamber prevails.

## What to do

[[procedure-alarm-and-finish]]: find and fix the cause, then **Confirm Alarm**. Agents never
request a setpoint, program or stop while the chamber is in alarm: they report the state
([[limits-and-safety]]).
