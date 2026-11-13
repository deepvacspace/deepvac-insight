---
title: The Deepvac chamber
type: concept
tags: [chamber, tvac, refrigeration, cascade, r404a, r23, heater, plc, overview]
sources: [control2-client settings.json (states, monitor, plc_settings, control_params), control2-client src/assistant/assistantengine.cpp, insight app/services/plc_gate.py]
updated: 2026-10-24
---
# The Deepvac chamber

What the chamber is, what it is made of and how the software talks to it. Start here; the
current values of every setting are in [[chamber-configuration]].

## What it is

A thermal vacuum (TVAC) test chamber: a device under test (DUT) is held at programmed
temperatures. The temperature setpoint can be set between the limits of the configuration
(−75 … 150 °C, see [[limits-and-safety]]).

## How it heats and cools

- **Heating**: an electric heater, driven by the controller output (its duty cycle is limited by
  the PLC setting `teh_duty`), and perimeter and glass heating against condensation.
- **Cooling**: a two-stage **cascade refrigeration** system:
  - the **R404A circuit** (higher stage) with its compressor, evaporator, bypass, heat exchanger
    and subcooling solenoids;
  - the **R23 circuit** (lower stage, for the deep cold) with its compressor, evaporator, bypass
    and subcooling solenoids. Its discharge pressure is measured (`pres_23_high`, bar).
- A **chamber volume fan** moves the air; a **light** shows the inside.

The PLC protects the compressors with timings: a minimum run time of the R404A compressor
(`t_gis_404`) and a start delay of the R23 compressor (`t_delay_23`). Below a setpoint of
`temp_comp_alw` the refrigeration stays on. See [[chamber-configuration]] for the values and
the output bits that show which component is on.

## Control

A PLC (CODESYS project `testa200`) runs a PID loop on the chamber temperature: the output
`temp_u` (%) is positive when heating and negative when cooling ([[pid-control]]). The
temperature sensor is corrected by a calibration table (`temp` = calibrated, `temp_raw` =
measured). The PLC runs **jobs**: a manual setpoint or a program of intervals
([[programs-and-jobs]]), and raises **alarms** when something is wrong ([[alarms]]).

## Software

| | |
|---|---|
| PLC gate | binary TCP protocol (port 4321): the PLC sends its state about once a second; the PC sends jobs, settings and commands |
| Deepvac Control | the operator application (C++): connects to the PLC, runs jobs, records every sample in day files and experiments in its database |
| Deepvac Insight | analysis application (Python): run database and analysis, Controller page, full-day recording, and the Agent API the AI agents use ([[agents-and-tools]]) |

Both applications can command the same PLC; see [[limits-and-safety]]. Where the data ends up:
[[data-and-recordings]].
