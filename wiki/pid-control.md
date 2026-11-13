---
title: PID control
type: concept
tags: [pid, control, gains, kp, ki, kd, tuning, output, saturation, windup]
sources: [control2-client settings.json (plc_settings.pids, states), insight app/api/worker_tools.py, insight app/core/analysis/controlanalysis.py, insight run database summaries (far/mid/near gains)]
updated: 2026-10-24
---
# PID control

How the chamber's temperature loop works, how its gains are organised and what to look at when
judging a setting. What the data says about good settings is in [[pid-tuning-lessons]].

## The loop

- Process value: `temp` (°C, calibrated). Setpoint: `temp_ref` (°C).
- Error = setpoint − temperature (positive when the chamber is too cold).
- Output: `temp_u` = `temp_u_p` + `temp_u_i` + `temp_u_d`, in % from −100 to 100. Positive drives
  the heater, negative the refrigeration (the worker reports `heating` above 1 %, `cooling`
  below −1 %).
- The gains in use are reported live: `temp_kp`, `temp_ki`, `temp_kd`.

## Gain table (gain scheduling)

The PLC holds a PID table (`p1`, "Temperature PID") that splits a temperature range into bands,
each with its own Kp, Ki and Kd. The band in use is reported in `temp_pid_idx`. The range, the
number of bands and the gains are in [[chamber-configuration]]; they are PLC settings written by
an operator from Deepvac Control. Agents can't change them.

Some older experiments (logger files, source `folder`) used three gain sets by distance to the
setpoint — far, mid and near — recorded in their summaries (`far_kp`, `mid_kp`, `near_kp`, …).

Gains are in the PLC's own units: compare them only with values recorded here.

## Judging a setting

The KPIs are defined in [[analysis-kpis]]. For a step to a new setpoint look at, in order:

1. **Tail MAE** and **tail std**: how close and how steady it ends.
2. **Overshoot**: how far it goes past the setpoint (a DUT may not tolerate it).
3. **Settling time**: how long until it stays within ±0.5 °C.
4. **Output saturation** and **integrator windup**: long periods at ±100 % mean the chamber
   can't go faster; gains don't help there, and a large integral then overshoots.
5. **P / I / D share** of the output: which term does the work.

The `cost` combines tail MAE, overshoot, tail std and final error into one number for ranking
settings (lower is better).

## General guidance (control theory, not specific to this chamber)

- More Kp: faster response, more overshoot, eventually oscillation.
- More Ki: removes the remaining offset faster, more overshoot and windup.
- More Kd: damps overshoot, amplifies sensor noise.
- Change one gain at a time and compare runs at the same setpoint and starting temperature.
- Test a setting on the [[digital-twin]] before running it on the chamber.
