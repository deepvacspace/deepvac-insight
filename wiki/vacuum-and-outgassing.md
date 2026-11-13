---
title: Vacuum, outgassing and the pressure rule
type: concept
tags: [vacuum, pressure, pump-down, outgassing, desorption, torr, gauge, pressure rule, venting]
sources: [insight app/api/campaign_supervisor.py, insight app/services/campaign_service.py, insight app/services/pressure.py, simulator internal/plc/vacuum.go, simulator README.md]
updated: 2026-11-13
---
# Vacuum, outgassing and the pressure rule

How a campaign uses the chamber's pressure: the pump-down before cycling, why the pressure rises
while heating, and the pressure rule that holds a heating ramp until it falls. Every pressure
Insight shows is in Torr, whatever unit the PLC reports.

## Where the pressure comes from

The PLC reports the gauge as `pres` (with its setpoint `pres_ref`). Deepvac's recorded runs have
no vacuum gauge: in the demo the pressure comes from the chamber simulator and is **synthetic**,
shaped like a thermal-vacuum chamber's but never a measurement. Say so whenever one is quoted.

## Pump-down

The campaign's first step. With a gauge, the run supervisor holds the chamber at ambient and
starts cycling once the gauge reads the start pressure (the standards pack's `vacuum_start_hpa`;
7.5×10⁻⁶ Torr in the demo pack). If it takes longer than the pack's pump-down timeout, a decision
point asks whether to wait, start anyway (a deviation) or abort ([[decision-points]]). Without a
gauge the operator confirms the pump-down.

## Outgassing and desorption (general)

In vacuum the pressure floor is set by the gas the hardware gives off, mostly water. It falls
slowly with the time pumped and rises with temperature. Heating also desorbs the gas adsorbed on
the shroud, the platen and the test article: during a heating ramp the pressure can rise well
above the floor, and falls back within minutes once the temperature is held. The first hot ramp
after a pump-down desorbs the most; later ones less. This is general vacuum practice; how this
chamber behaves needs real gauge data, which the run history doesn't have yet.

## The pressure rule

Part of the test procedure, shown in the specification and approved with the plan:

- **Hold**: when the pressure goes above the rule's hold limit (the pack's `pressure_rule.hold_hpa`,
  7.5×10⁻⁵ Torr in the demo) during a **heating** ramp, the run supervisor holds the temperature
  where it is. During a dwell it pauses the dwell's clock instead: time above the limit doesn't
  count as soak.
- **Resume**: below the resume limit (`resume_hpa`, 3.75×10⁻⁵ Torr in the demo) the ramp is sent
  again from where it was held, or the dwell's clock runs again.
- **Too long**: held longer than `max_hold_min` (60 min in the demo), a decision point: keep
  holding, resume anyway (a deviation) or abort ([[decision-points]]).

The holds and resumes are the supervisor applying an approved rule, logged as *Ramp held
(pressure rule)* and *Ramp resumed (pressure rule)*; each hold is a deviation in the report. The
gateway allows the hold's setpoint only from the run supervisor, between the campaign's own
temperatures ([[limits-and-safety]]).

## Venting

Back at ambient, a decision point: the operator approves venting, and the gateway checks the
chamber's temperature is inside the vent window (10 to 40 °C in the demo pack), so the chamber
isn't vented cold (condensation) or hot.

Related: [[procedure-test-campaign]], [[test-article-thermocouples]].
