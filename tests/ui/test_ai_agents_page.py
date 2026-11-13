"""Tests of the AI Agents page."""

import pytest

from app.common import PAGE_AI_AGENTS
from app.main_window import DeepVacDesktop
from app.views.ai_agents import AiAgentsPage, CampaignChart, sci, stage_of

pytestmark = pytest.mark.ui


def test_it_has_a_sidebar_button_and_builds_nothing_until_shown(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win._nav_to(PAGE_AI_AGENTS)
    assert win.content_stack.currentWidget() is win.ai_agents_widget
    assert win.act_ai_agents_btn.isChecked()
    assert win.ai_agents_widget.view is None
    assert win.demo._starts == ["online"]
    assert win.agent_chat.button.isHidden()
    win.demo.starting = True
    win._nav_to(PAGE_AI_AGENTS)
    assert win.demo._starts == ["online"]


def test_shown_it_builds_the_native_view(deepvac_ui, qtbot):
    page = AiAgentsPage()
    qtbot.addWidget(page)
    page.resize(1300, 850)
    page.show()
    assert page.view is not None and page.bridge is not None
    assert isinstance(page.chart, CampaignChart)
    assert not page.banner.isHidden()
    assert not page.api_btn.isHidden()
    page.apply_theme(True)
    page.apply_theme(False)
    page.shutdown()


def test_the_chart_draws_temperature_and_pressure(deepvac_ui, qtbot):
    chart = CampaignChart()
    qtbot.addWidget(chart)
    series = {
        "t_h": [0.0, 0.5, 1.0],
        "temp": [20.0, 35.0, 50.0],
        "setpoint": [50.0, 50.0, 50.0],
        "pres": [760.0, 1e-3, 3e-6],
        "pres_setpoint": [5e-6, 5e-6, 5e-6],
        "duts": [[20.0, 30.0, 45.0]] * 4,
    }
    chart.update_series(series, threshold=7.5e-6, duts_now=[44.8, 45.0, 45.2, 45.4])
    x, y = chart.pres.getData()
    assert y[0] == pytest.approx(2.8808, abs=1e-3)
    assert chart.tiles[0].text() == "+44.8°"
    assert chart.threshold.isVisible()
    chart.update_series({}, empty_text="Waiting")
    assert chart.empty.textItem.toPlainText() == "Waiting"


def test_results_and_deviations_read_as_rows(deepvac_ui, qtbot):
    from PySide6.QtWidgets import QLabel

    from app.views.ai_agents import _deviations, _results

    results = _results(
        [
            {
                "cycle": 1,
                "plateau": "hot",
                "target_c": 55,
                "dwell_min": 242.4,
                "min_c": 52.9,
                "max_c": 55.1,
                "within_band": True,
            },
            {
                "cycle": 1,
                "plateau": "cold",
                "target_c": -20,
                "dwell_min": 246,
                "min_c": -20.0,
                "max_c": -18.2,
                "within_band": False,
            },
        ]
    )
    qtbot.addWidget(results)
    texts = [w.text() for w in results.findChildren(QLabel)]
    assert "Cycle 1 · Hot plateau" in texts and "+55.0 °C for 242 min" in texts
    assert "Shroud 52.9 to 55.1 °C" in texts
    badges = results.findChildren(QLabel, "rowBadge")
    assert [b.text() for b in badges] == ["In band", "Out of band"]
    assert badges[1].property("bad")
    deviations = _deviations(
        [{"where": "cycle 1 hot dwell", "text": "Swung 1.3 °C", "decision": "Continue"}]
    )
    qtbot.addWidget(deviations)
    texts = [w.text() for w in deviations.findChildren(QLabel)]
    assert "Cycle 1 hot dwell" in texts and "Decision: Continue" in texts
    from app.views.ai_agents import _blocked

    blocked = _blocked(
        [
            {
                "actor": "Worker",
                "command": "turn off alarm alarm_cfg_1",
                "reasons": ["Only the customer's Service Engineer changes alarm settings."],
            }
        ]
    )
    qtbot.addWidget(blocked)
    texts = [w.text() for w in blocked.findChildren(QLabel)]
    assert "Worker" in texts and "Turn off alarm alarm_cfg_1" in texts
    assert [b.text() for b in blocked.findChildren(QLabel, "rowBadge")] == ["Blocked"]


def test_formatting_and_stages():
    assert sci(3e-6) == "3.0×10⁻⁶" and sci(760) == "760"
    assert stage_of(None) == "requirement" and stage_of({"state": "holding"}) == "run"
    assert stage_of({"state": "draft"}) == "plan" and stage_of({"state": "completed"}) == "report"
