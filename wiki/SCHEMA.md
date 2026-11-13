# Deepvac wiki: how pages are written

The wiki is what the agent knows about the Deepvac chamber that the data alone doesn't say:
how the chamber works, how to operate it, what the numbers mean, and the lessons learned from
experiments. With the experiments database it is the agent's "Brain". People and agents read it;
agents can only **propose** changes, and a person approves each one in Deepvac Insight
(gear menu → Wiki Proposals).

## Pages

- One topic per page, in a file named after it in kebab-case: `pid-control.md`.
- Start with frontmatter, then a `# Title` heading and a one-paragraph summary (the index shows
  it, so it must say what the page is for):

```
---
title: PID control
type: concept
tags: [pid, control, tuning]
sources: [control2-client settings.json, insight app/api/worker_tools.py]
updated: 2026-10-24
---
# PID control

How the chamber's temperature loop works and what its gains do...
```

| Field | |
|---|---|
| `title` | what the page is about |
| `type` | `concept` (how something works), `procedure` (how to do something, step by step), `reference` (definitions, limits, tables), `lesson` (something learned from experiments, with the runs that show it), `generated` (rewritten from the data, never edited) |
| `tags` | a few lowercase words people would search for |
| `sources` | where the content comes from: run numbers (`#123`), documents, code, people. A page without sources is not accepted |
| `updated` | the date of the last change |

- Link other pages as `[[page-name]]`; every page should be linked from at least one other.
- Write for an engineer who knows thermal chambers but not this one. Short sentences, units on
  every number (°C, s, %, bar), English.

## Facts and claims

- A number belongs either in a generated page (taken from the data) or with its source. Never
  invent values; when something is not known, say so.
- Lessons quote the runs that show them (`#412, #415`) and say how sure they are: one run is an
  observation, a pattern across many runs is a lesson. Lessons are about this chamber; general
  control theory belongs in concept pages and is marked as general.
- Current configuration values (PID gains, alarm list, PLC settings) are in
  [[chamber-configuration]], which is regenerated from the configuration: link to it rather than
  copying values that change.

## Generated pages

`chamber-configuration`, `experiment-history` and `pid-tuning-lessons` are rewritten by
`scripts/build_brain.py` from the configuration and the experiments database. They can't be
edited or proposed; to change what they say, change the data or the script.

## Proposing a change (agents)

1. Read the page first (`wiki_read`) and keep everything that is still right.
2. Propose the whole new page (`wiki_propose`): frontmatter, title, summary, content.
3. Give the reason the reviewer needs: what is new or wrong, and the evidence.

A proposal made against an older version of a page is refused when approved; read the page
again and propose anew. [[index]] lists every page; [[log]] records every change.
