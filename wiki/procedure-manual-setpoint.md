---
title: Set a temperature (manual setpoint)
type: procedure
tags: [procedure, setpoint, manual, ramp, hold, controller]
sources: [insight app/views/controller.py, insight app/program_editor.py, insight app/api/worker_tools.py, insight app/services/plc_gate.py]
updated: 2026-10-24
---
# Set a temperature (manual setpoint)

How to bring the chamber to a temperature and keep it there, by hand in Insight or through the
agent. The job it makes is described in [[programs-and-jobs]].

## By hand (Insight, Controller page)

A setpoint is a program of one interval: Insight sends it as a setpoint job, and the next
**Start** replaces it without stopping the chamber.

1. Select the chamber PLC (top right of the page; the dot is green while it answers). If step 3
   says **ALARM**, deal with that first ([[procedure-alarm-and-finish]]).
2. Step 1, **Experiment details**: a name or purpose and the operator (required), the DUT.
3. Step 2, **Intervals**: keep one interval. Open its card: **Setpoint, °C** (limited to the
   range in [[limits-and-safety]]) and the **Mode**: **Full speed, then hold** (the duration is
   the hold time; on the last interval **Hold until stopped**), **Linear ramp** (from the
   chamber's temperature over the ramp time) or **Custom ramp rate** (°C/min; the ramp time is
   calculated).
4. Step 3, **Confirm and start**: check the summary, press **Start** (or **Delay start**). If a
   setpoint is running Insight asks before replacing it; a running program must be stopped
   first.
5. Follow it in Analysis → **● Live** (the home page). To end it: **Stop** (Insight asks first).

## Through the agent

Ask the Professor, e.g. "Ramp to 40 °C at 2 °C/min and hold 30 minutes, reason: soak test". The
worker checks the live status and files a request (`request_setpoint`); it reaches the chamber
only after you approve it in Agent Requests ([[procedure-agent-requests]]).

## Good practice

- Give a ramp rate for large changes when the DUT needs gentle transitions.
- Starting from far below the setpoint, expect output saturation at first ([[pid-control]]).
- Note the reason: it ends up in the request and helps whoever analyses the run later.
