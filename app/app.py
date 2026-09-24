"""Entry point — login flow, splash screen, and main() only."""

import sys

from PySide6.QtCore import QCoreApplication, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from app.common import ICON_PATH, LOGO_PATH
from app.login_window import LoginWindow
from app.main_window import DeepVacDesktop
from app.services import (
    auth_service,
    backup_service,
    i18n_service,
    log_service,
    settings_service,
)


class LoadingSplash(QSplashScreen):
    def drawContents(self, painter):
        painter.setPen(QColor("#f8fafc"))
        painter.setFont(QFont("Segoe UI", 11, 600))
        painter.drawText(QRectF(0, 224, 520, 54), Qt.AlignCenter | Qt.TextWordWrap, self.message())


def make_splash():
    pixmap = QPixmap(520, 300)
    pixmap.fill(QColor("#0b1020"))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    painter.setPen(QColor("#60a5fa"))
    painter.setBrush(QColor("#111827"))
    painter.drawRoundedRect(18, 18, 484, 264, 14, 14)

    logo = QPixmap(LOGO_PATH)
    if not logo.isNull():
        scaled = logo.scaledToHeight(90, Qt.SmoothTransformation)
        x = (520 - scaled.width()) // 2
        painter.drawPixmap(x, 62, scaled)
    else:
        painter.setPen(QColor("#f8fafc"))
        painter.setFont(QFont("Segoe UI", 26, QFont.Black))
        painter.drawText(QRectF(0, 60, 520, 80), Qt.AlignCenter, "Deepvac")

    painter.setPen(QColor("#60a5fa"))
    painter.drawLine(60, 192, 460, 192)

    painter.end()
    return LoadingSplash(pixmap)


def _remembered_user():
    token = QSettings("DeepVac", "Insight").value("auth/remember_token", "")
    return auth_service.get_user_by_token(token) if token else None


def _show_splash(app, window_receiver_attr_name=None):
    splash = make_splash()
    splash.show()
    splash.showMessage(
        QCoreApplication.translate("main", "Starting Deepvac…"),
        Qt.AlignCenter | Qt.AlignBottom,
        Qt.white,
    )
    app.processEvents()
    return splash


def _run_smoke_test(no_splash=False):
    """Runs the startup path headlessly and exits, for CI smoke checks."""
    failure = {}

    def on_exception(exc_type, exc_value, exc_tb):
        failure.setdefault("exc", (exc_type, exc_value))

    log_service.install_excepthook(show_dialog=False)
    log_service.add_exception_listener(on_exception)

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(ICON_PATH))
    i18n_service.install_language(app, settings_service.load_language())

    try:
        backup_service.backup_all()
    except Exception as exc:
        print(f"[backup] startup backup skipped: {exc}")

    fake_user = {"id": 0, "name": "Smoke Test", "email": "smoke-test@localhost"}

    window = None
    try:
        splash = None if no_splash else _show_splash(app)
        window = DeepVacDesktop(splash=splash, current_user=fake_user)
        window.restore_window_state()
        if splash is not None:
            splash.finish(window)
    except Exception as exc:
        failure.setdefault("exc", (type(exc), exc))

    QTimer.singleShot(200, app.quit)
    app.exec()

    if window is not None:
        window.close()

    if "exc" in failure:
        exc_type, exc_value = failure["exc"]
        print(f"[smoke-test] FAILED: {exc_type.__name__}: {exc_value}", file=sys.stderr)
        return 1
    print("[smoke-test] OK")
    return 0


def main():
    args = sys.argv[1:]
    if "--smoke-test" in args:
        sys.exit(_run_smoke_test(no_splash="--no-splash" in args))

    no_splash = "--no-splash" in args

    log_service.install_excepthook()

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(ICON_PATH))
    i18n_service.install_language(app, settings_service.load_language())

    try:
        backup_service.backup_all()
    except Exception as exc:
        print(f"[backup] startup backup skipped: {exc}")

    user = _remembered_user()

    while True:
        if user is None:
            login = LoginWindow()
            login.show()
            app.exec()
            user = login.authenticated_user
            if user is None:
                sys.exit(0)

        splash = None if no_splash else _show_splash(app)

        window = DeepVacDesktop(splash=splash, current_user=user)
        window.restore_window_state()
        if splash is not None:
            splash.finish(window)
        app.exec()

        if not window.logout_requested:
            break
        user = None

    sys.exit(0)


if __name__ == "__main__":
    main()
