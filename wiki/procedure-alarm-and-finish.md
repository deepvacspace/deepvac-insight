---
title: Confirm an alarm or a finished job
type: procedure
tags: [procedure, alarm, finish, confirm, stop, status]
sources: [insight app/plc_control.py, insight app/services/plc_gate.py, control2-client settings.json (alarms)]
updated: 2026-10-24
---
# Confirm an alarm or a finished job

What to do when the PLC status is *alarm* (4) or *finish* (3). The status values are explained
in [[programs-and-jobs]], the alarms in [[alarms]].

## A finished job

The job's last interval ended: the status stays *finish* until someone acknowledges it.

1. Check the result (Analysis → ● Live; the job is saved as a run).
2. Press **Confirm finish** (Controller page, step 3). The chamber returns to *idle*.

## An alarm

1. Read which alarm is active (Controller page, step 3, or the worker's live status).
2. Find and fix the cause; for the overtemperature safety device someone must be at the chamber.
3. Press **Confirm Alarm**. If the cause remains the alarm comes back.
4. Restart the job if needed ([[procedure-manual-setpoint]], [[procedure-run-test-profile]]).

## Stopping a job

**Stop** (Controller page, asks first) ends the running job at once; the chamber goes to
*idle*. Agents can only request a stop ([[procedure-agent-requests]]).
