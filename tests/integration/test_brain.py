"""Tests of the Brain: wiki, experiments database and tools."""

import asyncio
import json
import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from app.api import activity, server
from app.api.brain_tools import BrainTools
from app.services import brain_db, data_service, wiki_generated, wiki_service

pytestmark = pytest.mark.integration

REPO_WIKI = Path(__file__).resolve().parents[2] / "wiki"

LESSON = """---
title: R23 start delay and cooling to -70 °C
type: lesson
tags: [r23, cooling, -70]
sources: [#12, #13]
updated: 2026-10-24
---
# R23 start delay and cooling to -70 °C

The first minutes of a cool-down to -70 °C are spent waiting for the R23 compressor ([[chamber-overview]]).
"""


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    copy = tmp_path / "wiki"
    shutil.copytree(REPO_WIKI, copy)
    monkeypatch.setenv("DEEPVAC_WIKI_DIR", str(copy))
    return copy


def test_the_wiki_is_complete_and_findable(wiki):
    assert wiki_service.lint() == []
    pages = {p["page"]: p for p in wiki_service.list_pages()}
    assert {
        "chamber-overview",
        "pid-control",
        "limits-and-safety",
        "procedure-agent-requests",
    } <= set(pages)
    assert all(p["summary"] and p["type"] in wiki_service.PAGE_TYPES for p in pages.values())
    assert wiki_service.search("R23 compressor start delay")[0]["page"] == "chamber-overview"
    assert wiki_service.search("settling time overshoot definition")[0]["page"] == "analysis-kpis"
    assert wiki_service.search("approve request")[0]["page"] == "procedure-agent-requests"
    page = wiki_service.read_page("pid-control")
    assert page["meta"]["type"] == "concept" and "pid-tuning-lessons" in page["links"]
    assert "glossary" in page["backlinks"]
    with pytest.raises(wiki_service.WikiError, match="No page 'pid-tunning'. Closest"):
        wiki_service.read_page("pid-tunning")
    index = wiki_service.rebuild_index().read_text(encoding="utf-8")
    assert "## Procedures" in index and "[[procedure-manual-setpoint]]" in index


def test_proposals_wait_for_a_person(wiki, deepvac_data_dir):
    with pytest.raises(wiki_service.WikiError, match="frontmatter"):
        wiki_service.propose("new-lesson", "# no frontmatter\n\ntext", "because")
    with pytest.raises(wiki_service.WikiError, match="kebab-case"):
        wiki_service.propose("New Lesson", LESSON, "because")
    with pytest.raises(wiki_service.WikiError, match="generated"):
        wiki_service.propose("pid-tuning-lessons", LESSON, "because")
    proposal = wiki_service.propose(
        "r23-start-delay", LESSON, "seen in #12 and #13", "professor agent"
    )
    assert proposal["status"] == "pending" and not (wiki / "r23-start-delay.md").exists()
    with pytest.raises(wiki_service.WikiError, match="signed-in"):
        wiki_service.decide(proposal["id"], True, "")
    decided = wiki_service.decide(proposal["id"], True, "Ana")
    assert decided["status"] == "approved" and decided["decided_by"] == "Ana"
    assert wiki_service.read_page("r23-start-delay")["meta"]["type"] == "lesson"
    assert "[[r23-start-delay]]" in (wiki / "index.md").read_text(encoding="utf-8")
    assert "created [[r23-start-delay]]" in (wiki / "log.md").read_text(encoding="utf-8")

    update = wiki_service.propose(
        "r23-start-delay", LESSON.replace("first minutes", "first 90 s"), "measured", "x"
    )
    (wiki / "r23-start-delay.md").write_text(LESSON + "\nEdited by hand.\n", encoding="utf-8")
    with pytest.raises(wiki_service.WikiError, match="changed after this proposal"):
        wiki_service.decide(update["id"], True, "Ana")
    assert wiki_service.decide(update["id"], False, "Ana", "outdated")["status"] == "rejected"


def _insert_run(key, name, start, setpoint):
    rows, temp = [], 24.0
    for i in range(400):
        ref = 24.0 if i < 10 else setpoint
        u = max(-100.0, min(100.0, 8 * (ref - temp)))
        temp += u * 0.004
        rows.append(
            {
                "timestamp": start + 2 * i,
                "temp": temp,
                "temp_ref": ref,
                "temp_u": u,
                "temp_kp": 10.0,
                "temp_ki": 600.0,
                "temp_kd": 6.0,
            }
        )
    conn = data_service.connect_cache()
    try:
        conn.execute(
            "INSERT INTO runs (key, id, group_name, root_path, run_path, samples_path, source_mtime, samples, "
            "duration_s, mae, start_time, end_time, columns_json, numeric_columns_json, summary_json, bands_json, "
            "annotations_json, samples_json, cached_at, source, start_epoch) VALUES (?, ?, 'tests', '', '', '', 0, ?, "
            "798, 1.0, ?, '', '[]', '[]', '{}', '[]', '[]', ?, datetime('now'), 'import', ?)",
            (key, name, len(rows), data_service.format_ts(start), json.dumps(rows), start),
        )
        conn.commit()
    finally:
        conn.close()


def _actions_dat(path: Path, entries):
    """Builds a Deepvac Control event log file."""

    def qstring(text):
        data = text.encode("utf-16-be")
        return struct.pack(">I", len(data)) + data

    out = struct.pack(">I", len(entries))
    for epoch, kind, message in entries:
        out += struct.pack(">IBI", 8, 0, 3)
        out += qstring("date") + struct.pack(">IBQ", 5, 0, epoch)
        out += qstring("msg") + struct.pack(">IB", 10, 0) + qstring(message)
        out += qstring("type") + struct.pack(">IBi", 2, 0, kind)
    path.write_bytes(out)


def test_the_experiments_database(deepvac_data_dir, wiki, tmp_path):
    for n in range(6):
        _insert_run(f"control/a{n}", f"cool {n}", 1_780_000_000 + n * 10_000, 0.0)
    log = tmp_path / "actions.dat"
    _actions_dat(
        log,
        [(1_780_000_100, 0, "Application started. User 'Ana'"), (1_780_000_200, 1, "PLC alarm 9")],
    )
    assert brain_db.import_control_log(log) == 2
    assert brain_db.import_control_log(log) == 0
    counts = brain_db.build()
    assert counts["experiments"] == 6 and counts["stages"] >= 6 and counts["events"] == 2

    rows = brain_db.query(
        "SELECT number, final_setpoint_c, kp, has_control FROM experiments ORDER BY number"
    )
    assert rows["columns"] == ["number", "final_setpoint_c", "kp", "has_control"]
    assert rows["rows"][0] == [1, 0.0, 10.0, 1]
    events = brain_db.query(
        "SELECT type, message FROM events WHERE source = 'control log' ORDER BY epoch"
    )
    assert events["rows"] == [["app", "Application started. User 'Ana'"], ["plc", "PLC alarm 9"]]
    limited = brain_db.query("SELECT * FROM stages", max_rows=2)
    assert limited["row_count"] == 2 and "narrow the query" in limited["note"]
    for bad in (
        "DELETE FROM experiments",
        "SELECT 1; DROP TABLE experiments",
        "PRAGMA table_info(experiments)",
        "ATTACH DATABASE 'x.db' AS x",
        "WITH t AS (SELECT 1) DELETE FROM experiments",
    ):
        with pytest.raises(brain_db.QueryError):
            brain_db.query(bad)
    assert brain_db.query("SELECT COUNT(*) FROM experiments")["rows"] == [[6]]
    schema = brain_db.schema()
    assert (
        schema["tables"]["experiments"]["rows"] == 6
        and "tail_mae" in schema["tables"]["experiments"]["columns"]
    )

    conn = sqlite3.connect(brain_db.brain_db_path())
    try:
        history = wiki_generated.experiment_history(conn)
        lessons = wiki_generated.pid_tuning_lessons(conn)
    finally:
        conn.close()
    assert "6 experiments (#1 … #6)" in history and "type: generated" in history
    assert "## 0 °C, cooling (6 steps" in lessons and "| 10 | 600 | 6 | 6 |" in lessons
    config = wiki_generated.chamber_configuration(
        {
            "control_params": [{"name": "temp", "min": -75, "max": 150}],
            "states": [{"name": "temp", "label": "Temperature, °C"}],
            "alarms": [{"index": 9, "label": "R23 circuit pressure exceeded"}],
            "plc_settings": {
                "general": [
                    {
                        "name": "alarm_cfg_1",
                        "label": "Alarm enable",
                        "value": 4.5559015672128453e-41,
                    }
                ],
                "pids": [
                    {
                        "name": "p1",
                        "label": "Temperature PID",
                        "min": -80,
                        "max": 200,
                        "points": 2,
                        "p": [5, 10],
                        "i": [600, 82],
                        "d": [6, 20],
                    }
                ],
            },
        }
    )
    assert "-75 … 150 °C" in config and "| `temp` | Temperature | °C |" in config
    assert (
        "0x00007F00 (bits 8, 9, 10, 11, 12, 13, 14)" in config
        and "| 1 | 60 … 200 °C | 10 | 82 | 20 |" in config
    )


def test_the_brain_tools_over_mcp(deepvac_data_dir, wiki):
    activity.clear()
    mcp = server.build_brain_server(BrainTools())
    result = asyncio.run(mcp.call_tool("wiki_search", {"query": "alarm confirm"}))
    found = json.loads(result.content[0].text)["results"]
    assert found[0]["page"] in ("procedure-alarm-and-finish", "alarms")
    assert activity.since(0)[-1]["agent"] == "Brain"
    assert "searching the wiki for 'alarm confirm'" in activity.since(0)[-1]["text"]
    proposal = asyncio.run(
        mcp.call_tool(
            "wiki_propose", {"page": "r23-start-delay", "content": LESSON, "reason": "seen in #12"}
        )
    )
    assert json.loads(proposal.content[0].text)["status"] == "pending"
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="not built yet"):
        asyncio.run(mcp.call_tool("sql_query", {"sql": "SELECT 1"}))
