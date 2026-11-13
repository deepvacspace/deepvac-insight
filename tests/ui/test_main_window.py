"""Tests of the main window."""

import pytest

from app.main_window import DeepVacDesktop

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, fake_user, qtbot):
    win = DeepVacDesktop(current_user=fake_user)
    qtbot.addWidget(win)
    win.restore_window_state()
    return win


def test_main_window_constructs(window):
    assert window is not None


def test_sidebar_is_present_with_six_nav_buttons(window):
    assert len(window._nav_buttons) == 6


def test_title_bar_is_present(window):
    assert window.title_bar is not None
    assert not hasattr(window.title_bar, "center_btn")


def test_default_page_is_analysis_with_its_live_tab(window):
    from app.common import PAGE_ANALYSIS

    assert window.content_stack.currentIndex() == PAGE_ANALYSIS
    assert window.editor_area.page_for("live") is not None


def test_settings_menu_opens_without_raising(window):
    window._show_settings()


def test_account_menu_opens_without_raising(window):
    window._show_account_menu()


def test_theme_application_does_not_raise(window):
    window.dark = False
    window.apply_theme()
    window.dark = True
    window.apply_theme()


def test_language_application_does_not_raise(window):
    from app.services import i18n_service

    for code in i18n_service.AVAILABLE_LANGUAGES:
        window._change_language(code)


def test_window_closes_cleanly(window):
    window.close()


def test_pages_construct_with_current_user_set(window, fake_user):
    assert window.current_user["email"] == fake_user["email"]
    assert window.runs == []
