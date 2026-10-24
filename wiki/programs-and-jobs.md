---
title: Programs, jobs and the PLC status
type: concept
tags: [program, job, interval, ramp, hold, cycle, status, setpoint, test profile, flags]
sources: [insight app/services/plc_gate.py, insight app/services/plc_programs.py, control2-client src/program/intervalcardlist.cpp, control2-client src/experiments/runrecord.cpp, control2-client settings.json (control_params, default_program)]
updated: 2026-10-24
---
# Programs, jobs and the PLC status

What the PLC runs (jobs made of intervals), what each interval mode does, and how to read the
status channels. How to start one: [[procedure-manual-setpoint]] and
[[procedure-run-test-profile]].

## Jobs

A job is a list of intervals sent to the PLC in one go. Two types (`work_type`):

| Type | Value | Made by |
|---|---|---|
| Manual setpoint | 1 | a temperature, with an optional ramp and hold |
| Program | 2 | a test profile: a list of intervals, optionally repeated in cycles |

Sending a new job replaces the running one.

## Intervals

Each interval has a **duration** (s), a **temperature** (°C) and **flags**:

| Flags | DeepVac Control calls it | What happens |
|---|---|---|
| RAMP (bit 9) | "Ramp to X °C" | the setpoint moves linearly to X over the duration |
| AWAIT (bit 8) | "Go to X °C and hold" (maximum speed) | the setpoint jumps to X; the interval holds it for its duration |
| neither | step / hold | the setpoint is X for the duration |

Bits 0–2 enable the controlled channels and are set from the configuration (the default program
uses flags 515 = channels 0 and 1 + RAMP: a ramp to 60 °C over 3600 s).

A manual setpoint becomes one or two intervals: a ramp (when a rate is given: duration =
|start − target| / rate) and a hold. Without a hold time the hold lasts 99 h 59 min 59 s, which
means "until stopped".

Test profiles made in Insight are a list of steps (setpoint, duration); each becomes a
"go to and hold" interval. Profiles imported from DeepVac Control keep their PLC program.

A **cycle** repeats a range of intervals a number of times (`cycle_cur` / `cycle_total`).

## Status channels

| Channel | Meaning |
|---|---|
| `status_condition` | 1 idle, 2 work (a job runs), 3 finish (the job ended; waiting to be confirmed), 4 alarm |
| `interval` / `intervals_count` | interval running (1-based) / intervals in the job |
| `cycle_cur` / `cycle_total` | cycle running / cycles programmed |
| `t_total` / `t_interval` | time since the job / the interval started, s |
| `alarm_out_status` / `alarm_in_status` | active alarms / their trigger conditions, as bits ([[alarms]]) |
| `outputs` / `inputs` | which components are on, as bits ([[chamber-configuration]]) |

## Commands

`stop` (end the job), `next` (skip to the next interval), confirm finish and confirm alarm
(the PLC spells them `confrim_finish`, `confrim_alarm`). See [[procedure-alarm-and-finish]].
