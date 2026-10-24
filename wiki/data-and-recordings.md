---
title: Where the data is
type: reference
tags: [data, runs, experiments, database, recordings, parquet, sql, duckdb, event log, reports]
sources: [insight README.md, insight app/services/day_recorder.py, insight app/services/brain_db.py, insight app/api/runs.py, control2-client src/storage/recordingfile.h]
updated: 2026-10-24
---
# Where the data is

The four kinds of data the crew can use, what each one answers and how to reach it. What the
run history holds is summarised in [[experiment-history]].

| Data | Holds | Answers | Reached with |
|---|---|---|---|
| Run database | every recorded experiment: samples, stored analysis, events, details | "how did experiment #412 go?" | analyst tools (run summary, stages, KPIs, compare, export) |
| Experiments database (Brain) | the runs as plain SQL tables: one row per experiment with its KPIs and gains, stages, anomalies, events, test profiles, recorded days | "which runs at 0 °C had overshoot under 1 °C?", "how many runs per month?" | `sql_schema`, `sql_query` |
| Full-day recordings | every sample of every chamber, all day, experiment or not | "what did the chamber do on 12 March at night?" | analyst `list_recordings`, `get_recording` |
| Event log | DeepVac Control's messages (application started, PLC messages, errors) | "when was the application closed?" | the `events` table (source `control log`) |

## Run numbers

Runs are numbered #1, #2, … by start time, oldest first, the same in every tool and in the
experiments database (`experiments.number`). A run can also be named by title, `latest` or
`previous`. Sources: `import` (recorded by DeepVac Control), `folder` (logger files),
`monitoring` (recorded by Insight's Live tab).

## Full-day recordings

One Apache Parquet file per chamber and day (`data/recordings/<chamber>/YYYY-MM-DD.parquet` in
Insight, `data/recordings/YYYY-MM-DD.parquet` in DeepVac Control): a `timestamp` column (UTC,
microseconds) and one column per channel, NaN where nothing was received. pandas, pyarrow and
DuckDB read them directly:

```sql
SELECT date_trunc('hour', timestamp) AS hour, avg(temp), max(pres_23_high)
FROM 'data/recordings/*/2026-10-24.parquet' GROUP BY 1 ORDER BY 1;
```

About 0.3 GB per chamber and year of continuous 1 Hz recording.

## Files the crew makes

Chart images, PDF / HTML reports and CSV files exported by the analyst go to `data/reports`;
their links open from the chat.
