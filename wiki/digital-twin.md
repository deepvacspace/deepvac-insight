---
title: The digital twin
type: concept
tags: [digital twin, simulation, gru, pid, what-if, model]
sources: [insight app/services/data_service.py (simulate_gru_run), insight app/api/analyst_tools.py (simulate_pid), control2-client data/model]
updated: 2026-10-24
---
# The digital twin

A neural-network model of the chamber used to try PID settings before running them. Its results
are simulations, never measurements: say so whenever one is quoted.

## What it is

A GRU network trained on the chamber's recorded runs predicts how the temperature responds to
the controller output; Insight closes the loop around it with the PID settings to test
(`simulate_pid`). DeepVac Control has its own models for its AI Advisor (`data/model`: GRU,
LSTM and twin models in ONNX).

## Using it (analyst `simulate_pid`)

| Input | Range / default |
|---|---|
| Kp | 1 … 50 |
| Ki | 1 … 1000 |
| Kd | 1 … 20 |
| target | the setpoint, °C |
| start | starting temperature, default 27 °C |
| duration | default 20 min |

It returns the KPIs of the simulated run ([[analysis-kpis]]).

## Limits

- It knows only the behaviour it was trained on: far from the recorded setpoints and gains its
  predictions are guesses.
- Compare simulated settings with each other, and confirm the best on the chamber before
  calling it a lesson ([[pid-tuning-lessons]], [[pid-control]]).
