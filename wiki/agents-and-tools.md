---
title: The AI agents and their tools
type: reference
tags: [agent, agents, professor, worker, analyst, brain, observer, tools, n8n, mcp]
sources: [agents README.md, agents workflows/deepvac-agents.json, agents workflows/deepvac-observer.json, insight app/api/server.py, insight app/api/observer_monitor.py, insight app/services/hold_advisor.py]
updated: 2026-11-13
---
# The AI agents and their tools

Who does what among the AI agents (n8n, `agents/`) and which tools each one has (Insight's Agent
API). The rules they follow are in [[limits-and-safety]].

| Member | Does | Tools |
|---|---|---|
| Professor | plans, delegates, answers the user; reads the Brain; runs test campaigns | the campaign tools (standards pack, draft, submit, follow, recommend, report facts, issue the report), the Brain's tools, the worker, the analyst |
| Worker | reads the chamber and files requests | live status, PLC settings, programs; request a setpoint, a program or a stop; wait for a request's decision |
| Analyst | evaluates recorded data | runs (list, rank, summary, stages, KPIs, statistics, anomalies, compare, events), PID simulation on the [[digital-twin]], full-day recordings, exports (charts, reports, data) |
| Brain | the knowledge: this wiki and the experiments database; a tool of the Professor | `wiki_index`, `wiki_search`, `wiki_read`, `wiki_propose`, `sql_schema`, `sql_query` |
| Analyst (rules) | inside Insight, not an n8n agent: recommends an option (★) the moment a campaign decision point opens; offline the local model may replace it | the decision point's facts ([[decision-points]]) |
| Observer | its own workflow, started by Insight's events (alarm, job ended, drift, anomaly, offline, decision point or request waiting): summarises each, notifies people when someone should act, may propose a request (built, not connected yet) | `get_event`, `list_events`, `get_live_status`, `report_event`, `propose_stop`, `propose_setpoint` |

## Using the Brain

1. Before planning, search the wiki for the topic (`wiki_search`) and read the pages that
   match (`wiki_read`): procedures, limits, earlier lessons. For a test campaign:
   [[procedure-test-campaign]], [[decision-points]], [[test-article-thermocouples]] and
   [[vacuum-and-outgassing]].
2. For questions over many runs, query the experiments database (`sql_schema` first, then
   `sql_query`); for one run, the analyst's tools.
3. Quote the pages and run numbers the answer rests on.
4. When an experiment teaches something new, propose a lesson (`wiki_propose`, following
   [[SCHEMA]]): what was learned, the runs that show it, how sure it is. A person approves it.

## Chamber actions

Only through the worker's requests, approved by a person ([[procedure-agent-requests]]), or
through a test campaign an operator approved, run by Insight's run supervisor; at its decision
points the Analyst (rules) or the Professor recommends and the operator decides
([[procedure-test-campaign]], [[decision-points]]). The Observer's
proposals are worker-style requests too, and it makes none on a chamber running a campaign.

## Events the Observer hears of

Insight records them while its Agent API runs (gear menu → Observer Events) and sends the ones from
*warning* up to the Observer when that is switched on: an alarm (critical); a job ending; drift, the
temperature more than 3 °C from a setpoint steady for 10 min, for 5 min; oscillation, spikes or a
stuck temperature in a running job; a chamber not heard from for 60 s; a campaign decision point or an
agent request waiting for a person. People acknowledge them in Insight ([[alarms]],
[[limits-and-safety]]).
