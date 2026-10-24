---
title: Glossary
type: reference
tags: [glossary, terms, definitions]
sources: [the pages of this wiki]
updated: 2026-10-24
---
# Glossary

Short definitions of the terms used across the wiki, with the page that explains each.

| Term | Meaning |
|---|---|
| TVAC | thermal vacuum test chamber ([[chamber-overview]]) |
| DUT | device under test |
| Setpoint (`temp_ref`) | the temperature the controller aims for, °C |
| Process value (`temp`) | the measured (calibrated) chamber temperature, °C |
| Output (`temp_u`) | controller output, −100 … 100 %: positive heats, negative cools ([[pid-control]]) |
| PID table / band | gain sets by temperature range; `temp_pid_idx` is the band in use |
| Cascade refrigeration | two refrigerant circuits in series: R404A (high stage) cools the R23 circuit (low stage) |
| Subcooling | cooling the liquid refrigerant below its condensing temperature (solenoids and PLC settings for R404A and R23) |
| Bypass solenoid | lets refrigerant bypass the evaporator to reduce cooling |
| Job | what the PLC runs: a manual setpoint or a program ([[programs-and-jobs]]) |
| Interval | one part of a job: duration, temperature, mode (ramp, go to and hold, step) |
| Cycle | a range of intervals repeated a number of times |
| Test profile | a saved program, run with Start Program ([[procedure-run-test-profile]]) |
| Run / experiment | one recorded job or test, numbered #N ([[data-and-recordings]]) |
| Full-day recording | every sample of a day, experiment or not, in a Parquet file |
| Stage | part of a run: setpoint step, ramp or hold ([[analysis-stages-and-anomalies]]) |
| MAE / tail MAE | mean absolute error over the run / its last 300 s ([[analysis-kpis]]) |
| Overshoot | how far the temperature goes past the setpoint |
| Settling time | until the temperature stays within ±0.5 °C of the setpoint |
| Saturation | the output stuck at ±100 % |
| Windup | the integral term growing while the output is saturated |
| Cost | weighted score of a run used to rank PID settings |
| Digital twin | a model of the chamber for simulating PID settings ([[digital-twin]]) |
| Agent request | a chamber action asked for by the worker, waiting for approval ([[procedure-agent-requests]]) |
| Proposal | a wiki change asked for by an agent, waiting for approval ([[SCHEMA]]) |
| PLC gate | the TCP protocol between the PLC and the PC applications |
