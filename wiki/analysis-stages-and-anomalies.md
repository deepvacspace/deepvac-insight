---
title: Stages, steady states and anomalies
type: reference
tags: [stages, segmentation, ramp, hold, setpoint, steady state, anomalies, spike, gap, oscillation, flatline]
sources: [insight app/core/analysis/segmentation.py, insight app/core/analysis/steadystate.py, insight app/core/analysis/anomalies.py]
updated: 2026-10-24
---
# Stages, steady states and anomalies

How a run is split into stages, when the temperature counts as steady, and which anomalies are
detected with which thresholds. KPIs: [[analysis-kpis]].

## Stages

| Kind | When |
|---|---|
| setpoint | the setpoint is constant and the temperature is still moving towards it (a step response) |
| ramp | the setpoint moves gradually |
| hold | the setpoint is constant and the temperature has reached it (within 0.5 °C) |
| unknown | no setpoint recorded |

Setpoint changes smaller than 0.05 °C are noise; stages shorter than 30 s merge into the
previous one. When the PLC program interval was recorded, each stage carries it.

## Steady state

Steady when, over a trailing 120 s window: temperature std ≤ 0.1 °C, trend ≤ 0.05 °C/min and
mean error ≤ 0.5 °C; stretches shorter than 60 s don't count. Per stage: *time to steady* and
*share of time steady*.

## Anomalies

| Kind | Detected when | Severity |
|---|---|---|
| limit | a channel leaves a variable rule's band (rules set on the run in Insight) | warning |
| spike | an isolated jump (robust z-score > 8 of the sample-to-sample change) undone within 3 samples, and at least 2 % of the channel's span | warning |
| flatline | a measured channel doesn't change at all for 15 min | info |
| gap | a time step over 5 × the typical sample period and at least 10 s | info |
| oscillation | the temperature crosses the setpoint 6 times within 10 min (ignoring \|error\| < 0.2 °C) | warning |
| not_reached | a step that never settles within ±0.5 °C: critical when it never gets within 2 °C, else warning | critical / warning |
| saturation | the output at a limit for more than 300 s | info |

A run's summary counts them by severity; the experiments database lists each one
([[data-and-recordings]]).
