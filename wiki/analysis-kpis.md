---
title: Control KPIs
type: reference
tags: [kpi, mae, tail mae, overshoot, settling, rise time, cost, saturation, analysis]
sources: [insight app/core/analysis/kpis.py, insight app/core/analysis/controlanalysis.py, insight app/core/analysis/runanalysis.py, control2-client src/core/analysis (the same Analysis Core)]
updated: 2026-10-24
---
# Control KPIs

How every KPI of a run is computed (the Analysis Core of DeepVac Control, ported to Insight with
identical results). Stages and anomalies: [[analysis-stages-and-anomalies]].

## Conventions

- Error = setpoint (`temp_ref`) − temperature (`temp`); positive when the chamber is too cold.
- Direction: heating when the setpoint is above the starting temperature, else cooling.
- A KPI that can't be computed is empty (NaN), never zero.

## Definitions

| KPI | Meaning |
|---|---|
| MAE | mean of \|error\| over the samples, °C |
| RMSE | root mean square error, °C |
| Bias | mean error, °C |
| Max \|error\| | largest \|error\|, °C |
| Overshoot | largest excursion past the setpoint in the approach direction, °C (worst step of a run) |
| Undershoot | largest excursion back after first reaching the setpoint, °C |
| Rise / fall time | 10 % → 90 % of the transition, heating / cooling, s |
| Time to near | until \|error\| ≤ 2 °C, s |
| Settling time | until \|error\| enters ±0.5 °C and stays, s (of the last step in a run) |
| Steady-state error | mean error after settling, °C |
| Tail MAE / tail std | MAE and temperature standard deviation over the last 300 s, °C |
| Final error | error at the last sample, °C |

**Which is the headline**: with a ramp, the whole-run MAE measures tracking; without one, it
mostly measures the distance to the first setpoint, so the tail MAE is the headline.

## Cost (ranking settings)

cost = 1 × tail MAE + 10 × overshoot + 0.5 × tail std + 0.5 × |final error| (a run that can't be
evaluated gets a huge penalty). Lower is better; overshoot weighs most.

## Controller behaviour

| KPI | Meaning |
|---|---|
| Output saturated | share of time `temp_u` is within 0.5 % of −100 % or 100 % |
| Longest saturation | longest continuous period at a limit, s |
| Integrator windup | time saturated while the integral term keeps pushing further, s |
| P / I / D share | share of the output from each term |

How to use them when comparing PID settings: [[pid-control]]; what the data shows:
[[pid-tuning-lessons]].
