---
title: Limits and safety rules
type: reference
tags: [safety, limits, approval, setpoint range, ramp, hold, rules]
sources: [control2-client settings.json (control_params), insight app/api/worker_tools.py, insight app/services/agent_requests_service.py, insight app/api/server.py, insight app/api/campaign_supervisor.py (gateway)]
updated: 2026-11-13
---
# Limits and safety rules

The limits every chamber action is checked against, and the rules the agent follows. They are
enforced by the software; this page explains them.

## Limits

| Limit | Value | Enforced by |
|---|---|---|
| Setpoint range | −75 … 150 °C (configuration `control_params`, see [[chamber-configuration]]) | worker tools, Controller page, program check |
| Ramp rate of a requested setpoint | above 0, at most 20 °C/min | worker tools |
| Hold time of a requested setpoint | above 0, at most 7 days (10 080 min) | worker tools |
| Program | every interval's temperature within the setpoint range | worker tools, Controller page |
| Undecided request | expires after 30 minutes | Agent Requests |

## Rules

1. **Only a person approves.** Agents file requests; nothing reaches the PLC until a signed-in
   user approves it in Insight ([[procedure-agent-requests]]).
2. **Read before acting.** The worker reads the live status first; if the chamber is
   disconnected or in alarm it requests nothing and reports the state ([[alarms]]).
3. **Say why.** Every request carries its reason; the operator reads it when deciding.
4. **Never claim a change happened** until its request is executed.
5. **Numbers come from tools**, never from memory: run numbers, KPIs, values.
6. **Simulations are simulations**: [[digital-twin]] results are not measurements.

## The gateway during a test campaign

Every command of a running campaign goes through Insight's gateway; a refused one is logged with
its reasons and listed in the report ([[procedure-test-campaign]]). It refuses:

| Attempt | Why |
|---|---|
| any command not from the run supervisor (an agent's own job, a ramp, a setpoint) | only the run supervisor sends a campaign's commands |
| a setpoint that isn't a plateau or ambient (e.g. +150 °C), or outside the chamber's range | the campaign's own temperatures only; the pressure rule's hold between them is the one exception ([[vacuum-and-outgassing]]) |
| a ramp faster than the specification (e.g. 10 °C/min against 2) | the specification's ramp limit |
| cycling without the pump-down | thermal cycling runs only in vacuum |
| venting without a person's approval, or outside the vent window | condensation when cold; a person decides |
| forcing a PLC output | the PLC's own interlocks stay primary |
| changing alarm or interlock settings | only the customer's Service Engineer, never during a campaign |
| changing PID or calibration settings | only a person, at a decision point (writing back the operator's recorded PID table) |
| an agent deciding a decision point or approving its own plan | only a signed-in person decides ([[decision-points]]) |

## Things to keep in mind

- **Two masters**: Deepvac Control, Insight's Controller page and the Agent API can all send
  jobs to the same PLC. A job replaces the one running, whoever sent it. Agree who operates.
- **Compressor protection**: the PLC delays the R23 compressor start and keeps the R404A
  compressor running a minimum time ([[chamber-overview]]); a setpoint that keeps switching
  between heating and cooling makes the refrigeration cycle.
- **The DUT** has its own limits; the chamber's range is not the DUT's. Its thermocouples, not
  the shroud, say when a plateau is reached ([[test-article-thermocouples]]).
