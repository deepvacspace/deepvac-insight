"""Navigation smoke tests."""

import pytest

from app.common import (
    PAGE_ANALYSIS,
    PAGE_CAMPAIGNS,
    PAGE_CONTROLLER,
    PAGE_COUNT,
    PAGE_OPC,
    PAGE_REPORTS,
)
from app.main_window import DeepVacDesktop

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    return win


@pytest.mark.parametrize("index", range(PAGE_COUNT))
def test_navigate_to_each_page_does_not_raise(window, index):
    window._nav_to(index)
    assert window.content_stack.currentIndex() == index


@pytest.mark.parametrize("index", range(PAGE_COUNT))
def test_revisiting_a_page_does_not_grow_signal_connections(window, index):
    signature = "2active_page_changed(PyObject)"
    before = window.editor_area.receivers(signature)

    window._nav_to(index)
    window._nav_to(index)
    window._nav_to(index)

    after = window.editor_area.receivers(signature)
    assert after == before


def test_no_real_chamber_connection_attempted_while_navigating(window):
    for index in range(PAGE_COUNT):
        window._nav_to(index)
    assert window._chamber_sessions == {}
    assert window.opc_server.is_running() is False


def test_run_list_is_a_drawer_of_analysis(window):
    window._open_runs_drawer()
    assert window.content_stack.currentWidget() is window.editor_area
    assert window.runs_drawer.is_open()
    assert window.run_list is not None and window.search_box is not None
    window._nav_to(PAGE_CONTROLLER)
    assert not window.runs_drawer.is_open()


def test_analysis_page_widgets_created(window):
    window._nav_to(PAGE_ANALYSIS)
    assert window.editor_area is not None


def test_reports_page_widgets_created(window):
    window._nav_to(PAGE_REPORTS)
    assert window._report_status_filter is not None


def test_controller_page_widgets_created(window):
    window._nav_to(PAGE_CONTROLLER)
    assert window.content_stack.currentWidget() is window.controller_widget
    assert window.controller_widget.start_btn is not None


def test_opc_page_navigable(window):
    window._nav_to(PAGE_OPC)
    assert window.content_stack.currentIndex() == PAGE_OPC


def test_campaigns_page_navigable(window):
    window._nav_to(PAGE_CAMPAIGNS)
    assert window.content_stack.currentWidget() is window.campaigns_widget
