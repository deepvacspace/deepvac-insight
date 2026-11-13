---
title: Run a thermal test campaign
type: procedure
tags: [procedure, campaign, qualification, acceptance, decision point, stabilisation, vacuum, report, professor]
sources: [insight app/services/campaign_service.py, insight app/api/campaign_supervisor.py, insight app/services/hold_advisor.py, insight app/services/campaign_report.py, insight app/views/ai_agents.py]
updated: 2026-11-13
---
# Run a thermal test campaign

A qualification or acceptance test of a part (the test article), from the customer's
requirement to a signed as-run report. The Professor drives it from the chat, or an operator
starts it in Insight's **AI Agents** page; either way only a person approves it and decides at
decision points ([[decision-points]], [[limits-and-safety]]).

## The stages

1. **Requirements**: the test article and the test type.
   - *Thermal cycling*: the level (qualification or acceptance), the margin (°C) and the number
     of cycles; the margin and cycles come from the level unless the requirement sets them.
   - *Single ramp* (an accuracy test, no level): the direction (up or down) and the margin.
   - The test article's lowest and highest predicted temperatures come from the standards pack
     for its articles; never invent them for another part.
2. **Plan**: the specification comes from the standards pack (`data/standards_pack.json`).
   Hot plateau = highest prediction + margin; cold plateau = lowest prediction − margin; the
   pack gives the dwell per plateau, the ramp limit, the stabilisation band, the stabilisation
   criterion, the pressure rule and the sensor validity rule. Both plateaus must be inside the
   chamber's setpoint range ([[chamber-configuration]]). Every value names its source; cycles,
   dwell and ramp can be changed with a reason, which the report shows. The demo pack's values
   are illustrative until a Deepvac engineer replaces them with the standard's.
3. **Approval**: the first decision point. An operator approves (the campaign starts) or
   declines (the plan goes back to the Plan stage to be changed).
4. **Run**: Insight's run supervisor (with the Agent API on) runs the steps:
   - **Pump-down**: with a vacuum gauge, cycling starts by itself once the gauge reads the
     start pressure (7.5×10⁻⁶ Torr in the demo pack); too slow, a decision point. Without a
     gauge the operator confirms ([[vacuum-and-outgassing]]).
   - **Per cycle**: a ramp to the hot plateau, the hot dwell, a ramp to the cold plateau, the
     cold dwell.
   - **Stabilisation**: a dwell starts when the procedure's stabilisation sensors on the test
     article are inside the band and steady, not when the shroud reaches its setpoint
     ([[test-article-thermocouples]]).
   - **Pressure rule**: a heating ramp is held while the pressure is above the rule's limit, and
     a dwell's clock pauses ([[vacuum-and-outgassing]]).
   - **Sensors**: an invalid thermocouple (open, frozen, spiking) is left out; only a person
     takes a stabilisation sensor out of the criterion ([[test-article-thermocouples]]).
   - Then back to ambient and venting, approved by the operator inside the vent window.
5. **Report**: when the campaign has completed or been aborted, the report is issued (the
   Professor writes the summary from the facts, the local model does offline, or a template) and
   an operator signs it off. Every pressure in it is in Torr.

## Rules the supervisor enforces

Only the run supervisor sends a campaign's commands, through the gateway: a setpoint must be one
of the campaign's plateaus or ambient (or, under the pressure rule, a hold between them), inside
the chamber's range, ramping no faster than the specification; cycling starts only in vacuum;
venting needs a person's approval and a live temperature inside the vent window; nobody forces an
output, changes alarms, interlocks or PID settings during a campaign, and an agent never decides a
decision point. A refused command is logged and listed in the report ([[limits-and-safety]]).
While a campaign runs, don't send other jobs to the chamber ([[procedure-agent-requests]]): the
campaign would stop at an *interrupted* decision point.

## The evidence

Every step, decision and refusal goes into the campaign's log, each entry chained to the one
before with SHA-256; **Verify log** rechecks it. The report prints the last hash, signed with the
installation's Ed25519 key, so a changed entry or report can be detected.

## What the Professor does

Before planning, read this page, [[decision-points]] and the pages they link. Show the user the
specification with its sources and the steps; submit only when the user agrees. During the run,
Insight tells the Professor when a decision point opens: read the campaign (`get_campaign`),
recommend one option with the numbers behind it (`recommend`), and leave the decision to the
operator. At the end, `get_report_facts`, then `issue_report`.
