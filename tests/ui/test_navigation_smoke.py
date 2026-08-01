"""Navigation smoke tests: visiting every sidebar page constructs its
expected top-level widget without raising, and re-visiting doesn't grow
signal-connection counts or attempt a real chamber connection.

Note on "duplicate signal connections": this app's pages (dashboard, runs,
reports, simulator, monitor, controller, opc) are all built once at
DeepVacDesktop construction time and live in a QStackedWidget -- _nav_to()
only changes the visible index, it never reconstructs a page. So the
realistic risk this check guards against (reconnecting the same signal on
every visit) doesn't actually exist in this navigation model; the test
below confirms that's genuinely true (receiver counts stay constant)
rather than asserting something that can't fail as written.
"""

import pytest

from app.main_window import DeepVacDesktop

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    return win


@pytest.mark.parametrize("index", range(8))
def test_navigate_to_each_page_does_not_raise(window, index):
    window._nav_to(index)
    assert window.content_stack.currentIndex() == index


@pytest.mark.parametrize("index", range(8))
def test_revisiting_a_page_does_not_grow_signal_connections(window, index):
    # QObject.receivers() wants the classic Qt SIGNAL() string form (a
    # leading '2' + the C++ method signature from QMetaMethod), not the
    # SignalInstance object itself.
    signature = "2active_page_changed(PyObject)"
    before = window.editor_area.receivers(signature)

    window._nav_to(index)
    window._nav_to(index)
    window._nav_to(index)

    after = window.editor_area.receivers(signature)
    assert after == before


def test_no_real_chamber_connection_attempted_while_navigating(window):
    # Live Monitoring's connections are only ever opened by an explicit
    # Connect click (app/views/monitoring.py's _on_mon_connect_new), never
    # by navigation or construction -- confirmed here rather than assumed.
    for index in range(8):
        window._nav_to(index)
    assert window._chamber_sessions == {}
    assert window.opc_server.is_running() is False


def test_dashboard_page_widgets_created(window):
    window._nav_to(0)
    assert window.content_stack.widget(0) is not None


def test_runs_page_widgets_created(window):
    window._nav_to(1)
    assert window.run_list is not None
    assert window.search_box is not None


def test_analysis_page_widgets_created(window):
    window._nav_to(2)
    assert window.editor_area is not None


def test_simulator_page_navigable(window):
    window._nav_to(3)
    assert window.content_stack.currentIndex() == 3


def test_reports_page_widgets_created(window):
    window._nav_to(4)
    assert window._report_status_filter is not None


def test_monitor_page_navigable(window):
    window._nav_to(5)
    assert window.content_stack.currentIndex() == 5


def test_controller_page_widgets_created(window):
    window._nav_to(6)
    assert window._test_profile_combo is not None
    assert window._test_start_btn is not None


def test_opc_page_navigable(window):
    window._nav_to(7)
    assert window.content_stack.currentIndex() == 7
