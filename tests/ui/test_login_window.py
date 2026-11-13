"""Tests of the login window."""

import pytest

from app.login_window import LoginWindow
from app.services import auth_service

pytestmark = pytest.mark.ui


@pytest.fixture
def window(deepvac_ui, qtbot):
    win = LoginWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    return win


def test_login_window_constructs_and_becomes_visible(window):
    assert window.isVisible()


def test_email_and_password_controls_exist(window):
    assert window.login_email is not None
    assert window.login_password is not None
    assert window.login_password.echoMode() == window.login_password.EchoMode.Password


def test_empty_input_is_rejected_with_error_shown(window):
    window._do_login()
    assert window.error_lbl.isVisible()
    assert window.authenticated_user is None


def test_successful_login_transitions_to_authenticated(window, qtbot):
    auth_service.create_user("Real User", "real@example.com", "password123")

    qtbot.keyClicks(window.login_email, "real@example.com")
    qtbot.keyClicks(window.login_password, "password123")
    window._do_login()

    assert window.authenticated_user is not None
    assert window.authenticated_user["email"] == "real@example.com"


def test_failed_login_shows_nonblocking_error_and_does_not_authenticate(window, qtbot):
    qtbot.keyClicks(window.login_email, "nobody@example.com")
    qtbot.keyClicks(window.login_password, "wrong-password")
    window._do_login()

    assert window.authenticated_user is None
    assert window.error_lbl.isVisible()
    assert "Incorrect" in window.error_lbl.text() or window.error_lbl.text() != ""


def test_remember_me_state_passed_to_authentication_service(window, qtbot, monkeypatch):
    auth_service.create_user("Remembered User", "remember@example.com", "password123")

    calls = []
    original = auth_service.set_remember_token

    def spy(user_id):
        calls.append(user_id)
        return original(user_id)

    monkeypatch.setattr(auth_service, "set_remember_token", spy)

    window.remember_cb.setChecked(True)
    qtbot.keyClicks(window.login_email, "remember@example.com")
    qtbot.keyClicks(window.login_password, "password123")
    window._do_login()

    assert len(calls) == 1


def test_remember_me_unchecked_does_not_set_a_token(window, qtbot, monkeypatch):
    auth_service.create_user("Not Remembered", "notremember@example.com", "password123")

    calls = []
    monkeypatch.setattr(auth_service, "set_remember_token", lambda user_id: calls.append(user_id))

    window.remember_cb.setChecked(False)
    qtbot.keyClicks(window.login_email, "notremember@example.com")
    qtbot.keyClicks(window.login_password, "password123")
    window._do_login()

    assert calls == []


def test_create_account_mode_can_be_opened(window):
    window._show_signup()
    assert window.stack.currentIndex() == 1
    assert window.signup_name is not None


def test_signup_password_mismatch_shows_error(window, qtbot):
    window._show_signup()
    qtbot.keyClicks(window.signup_name, "New Person")
    qtbot.keyClicks(window.signup_email, "newperson@example.com")
    qtbot.keyClicks(window.signup_password, "password123")
    qtbot.keyClicks(window.signup_confirm, "different")
    window._do_signup()

    assert window.authenticated_user is None
    assert window.error_lbl.isVisible()


def test_signup_success_authenticates_and_creates_real_account(window, qtbot):
    window._show_signup()
    qtbot.keyClicks(window.signup_name, "Brand New")
    qtbot.keyClicks(window.signup_email, "brandnew@example.com")
    qtbot.keyClicks(window.signup_password, "password123")
    qtbot.keyClicks(window.signup_confirm, "password123")
    window._do_signup()

    assert window.authenticated_user is not None
    assert window.authenticated_user["email"] == "brandnew@example.com"
    assert auth_service.authenticate("brandnew@example.com", "password123") is not None


def test_closing_window_does_not_leave_threads_active(window, qtbot):
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    window.close()
    qtbot.wait(50)
    app = QApplication.instance()
    leaked = [t for t in app.findChildren(QThread) if t.isRunning()]
    assert not leaked
