---
title: Limits and safety rules
type: reference
tags: [safety, limits, approval, setpoint range, ramp, hold, rules]
sources: [control2-client settings.json (control_params), insight app/api/worker_tools.py, insight app/services/agent_requests_service.py, insight app/api/server.py]
updated: 2026-10-24
---
# Limits and safety rules

The limits every chamber action is checked against, and the rules the crew follows. They are
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

## Things to keep in mind

- **Two masters**: DeepVac Control, Insight's Controller page and the Agent API can all send
  jobs to the same PLC. A job replaces the one running, whoever sent it. Agree who operates.
- **Compressor protection**: the PLC delays the R23 compressor start and keeps the R404A
  compressor running a minimum time ([[chamber-overview]]); a setpoint that keeps switching
  between heating and cooling makes the refrigeration cycle.
- **The DUT** has its own limits; the chamber's range is not the DUT's.
