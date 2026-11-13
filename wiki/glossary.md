---
title: Glossary
type: reference
tags: [glossary, terms, definitions]
sources: [the pages of this wiki]
updated: 2026-11-13
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
| Shroud | the surface the chamber's PID controls (`temp`) in a campaign ([[test-article-thermocouples]]) |
| Platen | the plate the test article is bolted to (`temp_platen`) |
| Test article | the part under test in a campaign (the DUT); its thermocouples are `temp1` … `temp4` |
| Stabilisation sensor | a test-article thermocouple the procedure names for deciding when a plateau is reached |
| Soak / dwell | the time held at a plateau once stabilised |
| Decision point | where a campaign stops until a person chooses an option; formerly "hold point" ([[decision-points]]) |
| Pressure rule | a heating ramp held while the pressure is above a limit ([[vacuum-and-outgassing]]) |
| Outgassing / desorption | gas the hardware gives off in vacuum, more while heating |
| Open / frozen / spiking thermocouple | the sensor faults the run supervisor detects ([[test-article-thermocouples]]) |
| Gateway | the check every campaign command passes; refusals are logged ([[limits-and-safety]]) |
| Signed log | a campaign's log, each entry chained to the one before with SHA-256 |
