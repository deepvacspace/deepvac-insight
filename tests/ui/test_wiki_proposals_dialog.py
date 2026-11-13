"""Tests of the wiki proposals dialog."""

import shutil
from pathlib import Path

import pytest
from PySide6.QtWidgets import QInputDialog

from app.services import wiki_service
from app.wiki_proposals_dialog import WikiProposalsDialog

pytestmark = pytest.mark.ui

PAGE = """---
title: Glass heating at low temperature
type: lesson
tags: [condensation, glass]
sources: [#40]
updated: 2026-10-24
---
# Glass heating at low temperature

Keep the perimeter and glass heating on below 0 °C ([[chamber-overview]]).
"""


@pytest.fixture
def dialog(deepvac_data_dir, qapp, qtbot, tmp_path, monkeypatch):
    copy = tmp_path / "wiki"
    shutil.copytree(Path(__file__).resolve().parents[2] / "wiki", copy)
    monkeypatch.setenv("DEEPVAC_WIKI_DIR", str(copy))
    widget = WikiProposalsDialog({"name": "Ana"})
    qtbot.addWidget(widget)
    return widget, copy


def test_approve_and_reject(dialog, monkeypatch):
    widget, wiki = dialog
    assert widget.list.count() == 0 and not widget.approve_btn.isEnabled()
    first = wiki_service.propose("glass-heating", PAGE, "seen in #40", "professor agent")
    second = wiki_service.propose(
        "pid-control", PAGE.replace("glass", "pid"), "wrong", "professor agent"
    )
    widget.refresh()
    assert widget.list.count() == 2
    widget.list.setCurrentRow(1)
    assert "+ # Glass heating at low temperature" in widget.diff.toPlainText()
    assert "seen in #40" in widget.reason.text()
    widget.approve_btn.click()
    assert (
        (wiki / "glass-heating.md")
        .read_text(encoding="utf-8")
        .startswith("---\ntitle: Glass heating")
    )
    assert wiki_service.get_proposal(first["id"])["status"] == "approved"

    widget.list.setCurrentRow(0)
    assert "--- current" in widget.diff.toPlainText()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("not about PID", True))
    widget.reject_btn.click()
    rejected = wiki_service.get_proposal(second["id"])
    assert rejected["status"] == "rejected" and rejected["comment"] == "not about PID"
    assert widget.list.count() == 0
