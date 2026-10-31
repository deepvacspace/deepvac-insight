---
title: Run a thermal test campaign
type: procedure
tags: [procedure, campaign, qualification, acceptance, hold point, report, professor]
sources: [insight app/services/campaign_service.py, insight app/api/campaign_supervisor.py, insight app/services/campaign_report.py, insight app/views/campaigns.py]
updated: 2026-10-28
---
# Run a thermal test campaign

A qualification or acceptance test of a part (the test article), from the customer's
requirement to a signed as-run report. The Professor drives it from the chat, or an operator
starts it in the **Test Campaigns** page; either way only a person approves it and decides at
hold points ([[limits-and-safety]]).

## The stages

1. **Requirement**: the test article, its lowest and highest predicted temperatures (°C) and
   the level, qualification or acceptance.
2. **Plan**: the specification comes from the standards pack (`data/standards_pack.json`).
   Hot plateau = highest prediction + the level's margin; cold plateau = lowest prediction −
   the margin; the pack gives the cycles, the dwell per plateau, the ramp limit and the
   stabilisation band. Both plateaus must be inside the chamber's setpoint range
   ([[chamber-configuration]]). Every value names its source; cycles, dwell and ramp can be
   changed with a reason, which the report shows. The demo pack's values are illustrative until
   a DeepVac engineer replaces them with the standard's.
3. **Approval**: the campaign waits at its first hold point until an operator approves it.
4. **Run**: Insight's run supervisor (with the Agent API on) runs the steps: pump-down
   (confirmed by the operator: no vacuum gauge is connected), then per cycle a ramp to the hot
   plateau, the hot dwell, a ramp to the cold plateau, the cold dwell; then back to ambient and
   venting (approved by the operator). A dwell starts when the chamber temperature is inside
   the band around the plateau.
5. **Report**: when the campaign has completed or been aborted, the report is issued (the
   Professor writes the summary from the facts, or a template does) and an operator signs it off.

## Hold points and what the options do

| Hold point | Raised when | Options |
|---|---|---|
| Approval | the plan is submitted | approve and start · decline |
| Pump-down | before the first ramp | pumped down, start cycling · abort |
| Anomaly | during a dwell the temperature stays out of the band longer than the excursion time; the dwell clock pauses | extend the dwell · accept and continue (a deviation) · abort and return to ambient |
| Not stabilised | a plateau isn't inside the band within the stabilisation timeout | wait longer · start the dwell anyway (a deviation) · abort |
| Interrupted | the chamber's job ended, an alarm, or another job's setpoint took over | resend this step · return to ambient · end the campaign now |
| Vent | back at ambient | approve venting (allowed only inside the vent window) |

The Professor can recommend one option with its reasons; the page marks it with a star. The
operator decides. **Stop now** ends a running campaign at once and stops the chamber's job.

## Rules the supervisor enforces

Only the run supervisor sends a campaign's commands. A setpoint must be one of the campaign's
plateaus or ambient, inside the chamber's range, ramping no faster than the specification;
cycling starts only after the pump-down is confirmed; venting needs the approval and a live
temperature inside the vent window. A refused command is logged and listed in the report. While a
campaign runs, don't send other jobs to the chamber ([[procedure-agent-requests]]): the
campaign would stop at an *interrupted* hold point.

## The evidence

Every step, decision and refusal goes into the campaign's log, each entry chained to the one
before with SHA-256; **Verify log** rechecks it. The report prints the last hash, signed with
the installation's Ed25519 key, so a changed entry or report can be detected.
