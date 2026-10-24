---
title: Set a temperature (manual setpoint)
type: procedure
tags: [procedure, setpoint, manual, ramp, hold, controller]
sources: [insight app/plc_control.py, insight app/api/worker_tools.py, insight app/services/plc_gate.py]
updated: 2026-10-24
---
# Set a temperature (manual setpoint)

How to bring the chamber to a temperature and keep it there, by hand in Insight or through the
crew. The job it makes is described in [[programs-and-jobs]].

## By hand (Insight, Controller page)

1. Select the chamber PLC; the status line shows its state, temperature, setpoint and output.
   If it says **ALARM**, deal with that first ([[procedure-alarm-and-finish]]).
2. In **Manual setpoint**, enter the **Temperature** (°C; limited to the range in
   [[limits-and-safety]]).
3. Optional: **Ramp at** a rate (°C/min) to move there gradually instead of at full speed.
4. Optional: **Hold for** a time (minutes); without it the setpoint is held until stopped.
5. Press **Start**. If a job is running Insight asks before replacing it.
6. Follow it in Analysis → **● Live**. To end it: **Stop** (Insight asks first).

## Through the crew

Ask the Professor, e.g. "Ramp to 40 °C at 2 °C/min and hold 30 minutes, reason: soak test". The
worker checks the live status and files a request (`request_setpoint`); it reaches the chamber
only after you approve it in Agent Requests ([[procedure-agent-requests]]).

## Good practice

- Give a ramp rate for large changes when the DUT needs gentle transitions.
- Starting from far below the setpoint, expect output saturation at first ([[pid-control]]).
- Note the reason: it ends up in the request and helps whoever analyses the run later.
