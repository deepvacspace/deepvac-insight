---
title: Decision points of a test campaign
type: reference
tags: [decision point, hold point, campaign, approval, recommendation, analyst, operator]
sources: [insight app/services/campaign_service.py (HOLD_OPTIONS), insight app/api/campaign_supervisor.py, insight app/services/hold_advisor.py]
updated: 2026-11-13
---
# Decision points of a test campaign

Where a running campaign stops until a person chooses an option: when each one is raised, what
each option does, and how the Analyst's recommendation (★) is made. Only the signed-in person
decides, and the decision is logged under their name ([[procedure-test-campaign]]).

## Who recommends

As soon as the run supervisor raises a decision point, the **Analyst (rules)** recommends an
option from the facts (temperatures, pressures, sensor states, gains in use). Offline, the local
model may replace it a few seconds later; online, the Professor can recommend instead
(`recommend`). The recommendation is advice: an agent never decides, and the gateway refuses any
agent that tries ([[limits-and-safety]]).

## The decision points

| Decision point | Raised when | Options |
|---|---|---|
| Approval | the plan is submitted | approve and start · decline (back to the Plan stage) |
| Pump-down | before cycling, no gauge reading | pumped down, start cycling · abort |
| Slow pump-down | the gauge hasn't reached the start pressure within the pump-down timeout | wait longer · start cycling anyway (a deviation) · abort |
| Not stabilised | the stabilisation sensors aren't stabilised within the stabilisation timeout | wait longer · start the dwell anyway (a deviation) · abort and return to ambient |
| Anomaly | during a dwell the shroud stays out of the band longer than the excursion time; the dwell clock pauses | extend the dwell · accept and continue (a deviation) · abort and return to ambient |
| Oscillation | the shroud oscillates around the plateau during a dwell | revert to the operator's PID table (a person's change, written through the gateway) · keep the gains and continue (a deviation) · abort |
| Pressure | the pressure rule has held longer than its limit | keep holding · resume anyway (a deviation) · abort and return to ambient |
| Sensor | a stabilisation sensor is invalid during cycling | exclude it and continue (a deviation) · check it again in 2 min · abort and return to ambient |
| Interrupted | the chamber's job ended, an alarm, or another job's setpoint took over | resend this step · return to ambient · end the campaign now |
| Vent | back at ambient | approve venting (only inside the vent window) |

## What the rules recommend

- **Not stabilised**: wait while the slowest stabilisation sensor is still approaching the target;
  start the dwell if it settled within twice the band; else abort and check the mounting and the
  thermocouples.
- **Pressure**: keep holding while the pressure is within ten times the hold limit (outgassing
  still coming off); above that, abort and look for a leak.
- **Sensor**: check a spiking sensor again (often a connector); exclude an open or frozen one when
  another stabilisation sensor remains; abort when none would remain.
- **Oscillation**: revert the PID table when the gains in use differ from the operator's; else
  continue and log it, or abort if the swing is wider than twice the band.
- **Anomaly**: extend the dwell, or abort when the shroud is more than three times the band away.

Related: [[vacuum-and-outgassing]], [[test-article-thermocouples]], [[procedure-agent-requests]].
