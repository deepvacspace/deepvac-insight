"""Smoke tests for DeepVacDesktop (app/main_window.py) construction and
its Settings/Account menus. Real widgets, real construction path (same as
production) -- just with isolated data (deepvac_ui) and no blocking modal
event loops (no_modal_dialogs/no_blocking_menus, both autouse)."""

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


def test_sidebar_is_present_with_eight_nav_buttons(window):
    assert len(window._nav_buttons) == 8


def test_title_bar_is_present(window):
    assert window.title_bar is not None


def test_default_page_is_dashboard(window):
    assert window.content_stack.currentIndex() == 0


def test_settings_menu_opens_without_raising(window):
    window._show_settings()  # QMenu.exec() is patched to return None (autouse)


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
    assert window.runs == []  # empty_workspace fixture -> nothing cached
