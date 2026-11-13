"""Tests of a campaign through the AI Agents page."""

import time

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from app.services import campaign_service as cs
from app.services import chambers_service
from app.views.ai_agents import (
    AiAgentsPage,
    DecisionDialog,
    PlanView,
    RequirementForm,
    SpecGrid,
    StepList,
)
from tests.integration.test_demo_bridge import Host, Links, SampledClient

pytestmark = pytest.mark.ui


@pytest.fixture
def page(deepvac_ui, qtbot):
    chambers_service.add_chamber("Simulator", "127.0.0.1", 4321, protocol="plc")
    chamber = next(c for c in chambers_service.list_chambers() if c["name"] == "Simulator")
    client = SampledClient()
    client.now = time.time() + 3600
    page = AiAgentsPage(host=Host(Links(client, chamber), supervisor=object()))
    qtbot.addWidget(page)
    page.resize(1400, 900)
    page.show()
    qtbot.waitUntil(lambda: page.view is not None, timeout=10000)
    yield page
    page.shutdown()


def _texts(widget, cls=QLabel):
    return [w.text() for w in widget.findChildren(cls)]


def test_the_page_runs_a_campaign_through_insight(page, qtbot):
    form = page._stage
    assert isinstance(form, RequirementForm)
    assert page.rail.segments["requirement"].property("state") == "now"
    assert not page.chart.isVisible() and not page.feed.isVisible()
    assert form.article.currentText().startswith("6U CubeSat")
    assert [form.article.itemText(i) for i in range(form.article.count())] == [
        "6U CubeSat",
        "Reaction Wheel Assembly",
        "Optical Camera Payload",
        "Power Control and Distribution Unit",
    ]
    assert (form.margin.value(), form.cycles.value()) == (10.0, 4)
    form.level.setCurrentText("acceptance")
    assert (form.margin.value(), form.cycles.value()) == (5.0, 2)
    form.ramp.click()
    assert form.cyc_box.isHidden() and not form.ramp_box.isHidden()
    form.cycling.click()

    form.article.setEditText("Solar array drive")
    form.notes.setText("Customer ACME")
    form.draft_btn.click()
    qtbot.waitUntil(lambda: isinstance(page._stage, PlanView), timeout=10000)
    campaign = cs.latest_campaign()
    assert campaign["state"] == "draft" and campaign["created_by"] == "Ana"
    assert campaign["title"] == "Solar array drive"
    assert campaign["requirement"]["notes"] == "Customer ACME"
    plan = page._stage
    assert plan.state_text.text().endswith("Simulator")
    assert len(plan.findChild(SpecGrid).findChildren(QLabel, "cellValue")) >= 10
    assert _texts(plan.findChild(StepList), QLabel)[0] == "" or "Before cycling" in _texts(
        plan.findChild(StepList)
    )
    assert not any("clause to confirm" in t for t in _texts(plan))
    assert page.chart.isVisible() and page.feed.isVisible()
    buttons = {b.text(): b for b in plan.findChildren(QPushButton)}
    assert buttons["Cancel"].objectName() == "stopButton"

    buttons["Submit for approval"].click()
    qtbot.waitUntil(lambda: isinstance(page._dialog, DecisionDialog), timeout=10000)
    assert "Approve the test specification" in " ".join(_texts(page._dialog))
    assert any(t.startswith("Person logged in the decision: Ana") for t in _texts(page._dialog))
    approve = next(
        b
        for b in page._dialog.findChildren(QPushButton)
        if b.text().startswith("Approve and start")
    )
    approve.click()
    qtbot.waitUntil(lambda: cs.decisions(campaign["id"]) != [], timeout=10000)
    assert cs.decisions(campaign["id"])[0]["by"] == "Ana"
    page.refresh()
    assert any(t.startswith("Decision made (Ana)") for t in _texts(page.feed))
