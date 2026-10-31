"""Campaigns page: drafting, hold-point decisions and the report."""

import pytest

from app.services import campaign_service as cs
from app.services import chambers_service
from app.views.campaigns import CampaignsPage

pytestmark = pytest.mark.ui


@pytest.fixture
def page(qapp, qtbot, deepvac_data_dir):
    chambers_service.add_chamber("DV-1", "127.0.0.1", 1, protocol="plc")
    widget = CampaignsPage({"name": "Ana"}, lambda: False)
    qtbot.addWidget(widget)
    widget.resize(1300, 900)
    widget.show()
    return widget


def _chamber():
    return next(c for c in chambers_service.list_chambers() if c["protocol"] == "plc")


def test_draft_submit_and_decide_the_approval(page):
    campaign = page.draft(
        {"article": "CubeSat", "op_min_c": -10, "op_max_c": 45, "level": "acceptance"}, _chamber()
    )
    assert campaign["state"] == "draft"
    assert page.title_label.text() == f"#{campaign['id']}  CubeSat"
    assert page.submit_btn.isVisible() and not page.hold_card.isVisible()
    assert "OFF" in page.supervisor_label.text()
    spec = page.sections["spec"][1]
    assert spec.item(2, 1).text() == "+50 °C" and "5 °C margin" in spec.item(2, 2).text()

    page.submit()
    assert cs.require(campaign["id"])["state"] == "approval"
    assert page.hold_card.isVisible()
    assert "Approve the test specification" in page.hold_title.text()

    cs.recommend(campaign["id"], "approve", "Follows the pack.")
    page.refresh()
    assert "Professor recommends: Approve and start" in page.hold_recommendation.text()
    buttons = [page.hold_options.itemAt(i).widget() for i in range(page.hold_options.count())]
    assert buttons[0].text().startswith("★ Approve and start")

    page.hold_comment.setText("checked with the customer")
    buttons[0].click()
    hold = cs.holds(campaign["id"])[0]
    assert hold["choice"] == "approve" and hold["decided_by"] == "Ana"
    assert hold["comment"] == "checked with the customer"
    log = page.sections["log"][1]
    assert log.item(0, 3).text() == "Hold point decision"


def test_report_and_sign_off_of_an_ended_campaign(page, monkeypatch):
    campaign = page.draft(
        {"article": "Bus", "op_min_c": 0, "op_max_c": 30, "rehearsal": True}, _chamber()
    )
    cs.update(campaign["id"], state=cs.ABORTED, end_reason="aborted, back at ambient")
    page.refresh()
    assert page.report_btn.isVisible() and not page.sign_btn.isVisible()
    page.issue_report()
    c = cs.require(campaign["id"])
    assert c["summary_source"] == "Written from a template."
    assert page.open_btn.isVisible() and page.sign_btn.isVisible()
    page.sign_off()
    assert cs.require(campaign["id"])["signed_off_by"] == "Ana"
    page.verify_log()
    assert page.verify_label.text().startswith("Log intact")
