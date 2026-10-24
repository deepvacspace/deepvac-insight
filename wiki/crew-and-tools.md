---
title: The AI crew and its tools
type: reference
tags: [crew, agents, professor, worker, analyst, brain, observer, tools, n8n, mcp]
sources: [agents README.md, agents workflows/deepvac-crew.json, insight app/api/server.py]
updated: 2026-10-24
---
# The AI crew and its tools

Who does what in the AI crew (n8n, `../agents`) and which tools each one has (Insight's Agent
API). The rules they follow are in [[limits-and-safety]].

| Member | Does | Tools |
|---|---|---|
| Professor | plans, delegates, answers the user; reads the Brain | the Brain's tools (when connected), the worker, the analyst |
| Worker | reads the chamber and files requests | live status, PLC settings, programs; request a setpoint, a program or a stop; wait for a request's decision |
| Analyst | evaluates recorded data | runs (list, rank, summary, stages, KPIs, statistics, anomalies, compare, events), PID simulation on the [[digital-twin]], full-day recordings, exports (charts, reports, data) |
| Brain | the knowledge: this wiki and the experiments database | `wiki_index`, `wiki_search`, `wiki_read`, `wiki_propose`, `sql_schema`, `sql_query` |
| Observer | (planned) watches the chamber and raises what needs attention | |

## Using the Brain

1. Before planning, search the wiki for the topic (`wiki_search`) and read the pages that
   match (`wiki_read`): procedures, limits, earlier lessons.
2. For questions over many runs, query the experiments database (`sql_schema` first, then
   `sql_query`); for one run, the analyst's tools.
3. Quote the pages and run numbers the answer rests on.
4. When an experiment teaches something new, propose a lesson (`wiki_propose`, following
   [[SCHEMA]]): what was learned, the runs that show it, how sure it is. A person approves it.

## Chamber actions

Only through the worker's requests, approved by a person ([[procedure-agent-requests]]).
