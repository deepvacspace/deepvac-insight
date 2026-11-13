# Deepvac wiki: index

Every page with its summary. Rebuilt automatically: don't edit. How pages are written: [[SCHEMA]]; what changed: [[log]].

## Concepts

- [[chamber-overview]] **The Deepvac chamber**: What the chamber is, what it is made of and how the software talks to it. Start here; the current values of every setting are in [[chamber-configuration]].
- [[digital-twin]] **The digital twin**: A neural-network model of the chamber used to try PID settings before running them. Its results are simulations, never measurements: say so whenever one is quoted.
- [[pid-control]] **PID control**: How the chamber's temperature loop works, how its gains are organised and what to look at when judging a setting. What the data says about good settings is in [[pid-tuning-lessons]].
- [[programs-and-jobs]] **Programs, jobs and the PLC status**: What the PLC runs (jobs made of intervals), what each interval mode does, and how to read the status channels. How to start one: [[procedure-manual-setpoint]] and [[procedure-run-test-profile]].
- [[test-article-thermocouples]] **Shroud, platen and the test article's thermocouples**: What each temperature in a campaign is, when a plateau counts as stabilised, and how a faulty thermocouple is found and handled. The decision points they raise are in [[decision-points]].
- [[vacuum-and-outgassing]] **Vacuum, outgassing and the pressure rule**: How a campaign uses the chamber's pressure: the pump-down before cycling, why the pressure rises while heating, and the pressure rule that holds a heating ramp until it falls. Every pressure Insight shows is in Torr,…

## Procedures

- [[procedure-agent-requests]] **Approve or reject an agent's request**: How chamber actions asked for by the AI agents reach the chamber. Nothing the agents ask for is sent until a person approves it ([[limits-and-safety]]).
- [[procedure-alarm-and-finish]] **Confirm an alarm or a finished job**: What to do when the PLC status is *alarm* (4) or *finish* (3). The status values are explained in [[programs-and-jobs]], the alarms in [[alarms]].
- [[procedure-manual-setpoint]] **Set a temperature (manual setpoint)**: How to bring the chamber to a temperature and keep it there, by hand in Insight or through the agent. The job it makes is described in [[programs-and-jobs]].
- [[procedure-run-test-profile]] **Run a test profile (program)**: How to run a sequence of temperatures as one PLC program. Profiles and intervals are explained in [[programs-and-jobs]].
- [[procedure-test-campaign]] **Run a thermal test campaign**: A qualification or acceptance test of a part (the test article), from the customer's requirement to a signed as-run report. The Professor drives it from the chat, or an operator starts it in Insight's **AI Agents** pa…

## Reference

- [[agents-and-tools]] **The AI agents and their tools**: Who does what among the AI agents (n8n, `agents/`) and which tools each one has (Insight's Agent API). The rules they follow are in [[limits-and-safety]].
- [[alarms]] **Alarms**: How the PLC reports alarms, what each one means and what to do. The current alarm list (bit and label) is in [[chamber-configuration]].
- [[analysis-kpis]] **Control KPIs**: How every KPI of a run is computed (the Analysis Core of Deepvac Control, ported to Insight with identical results). Stages and anomalies: [[analysis-stages-and-anomalies]].
- [[analysis-stages-and-anomalies]] **Stages, steady states and anomalies**: How a run is split into stages, when the temperature counts as steady, and which anomalies are detected with which thresholds. KPIs: [[analysis-kpis]].
- [[data-and-recordings]] **Where the data is**: The four kinds of data the agent can use, what each one answers and how to reach it. What the run history holds is summarised in [[experiment-history]].
- [[decision-points]] **Decision points of a test campaign**: Where a running campaign stops until a person chooses an option: when each one is raised, what each option does, and how the Analyst's recommendation (★) is made. Only the signed-in person decides, and the decision is…
- [[glossary]] **Glossary**: Short definitions of the terms used across the wiki, with the page that explains each.
- [[limits-and-safety]] **Limits and safety rules**: The limits every chamber action is checked against, and the rules the agent follows. They are enforced by the software; this page explains them.

## Generated from the data

- [[chamber-configuration]] **Chamber configuration**: The chamber's current configuration: its channels, alarms, PLC settings, PID table, calibration and output bits, as Deepvac Control and Insight use them. What they mean: [[chamber-overview]], [[pid-control]], [[alarms]].
- [[experiment-history]] **Experiment history**: What the run history holds: how many experiments, when, of which kind, at which setpoints, and what went wrong. Details of any experiment: the analyst's tools or [[data-and-recordings]].
- [[pid-tuning-lessons]] **PID tuning: what the experiments show**: Step responses grouped by target temperature and direction: how they performed and which PID gains did best. Facts from the recorded runs, not recommendations: conditions differ between runs (starting temperature, loa…
