"""Application-wide logging and a global exception hook."""

import contextlib
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler

from app.common import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"

_logging_configured = False
_hook_installed = False
_exception_listeners = []


def setup_logging():
    global _logging_configured
    if _logging_configured:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    _logging_configured = True


def install_excepthook(show_dialog=True):
    """Logs uncaught exceptions and optionally shows a crash dialog."""
    global _hook_installed
    if _hook_installed:
        return
    setup_logging()

    logger = logging.getLogger("deepvac.crash")
    previous_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("Unhandled exception:\n%s", text)
        for listener in _exception_listeners:
            with contextlib.suppress(Exception):
                listener(exc_type, exc_value, exc_tb)
        if show_dialog:
            _show_crash_dialog(exc_type, exc_value, text)
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
    _hook_installed = True


def add_exception_listener(callback):
    """Registers a callback for uncaught exceptions."""
    _exception_listeners.append(callback)


def _show_crash_dialog(exc_type, exc_value, text):
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            return

        def tr(s):
            return QCoreApplication.translate("LogService", s)

        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(tr("Unexpected error"))
        box.setText(
            tr("An unexpected error occurred and has been logged:")
            + "\n\n"
            + f"{exc_type.__name__}: {exc_value}\n\n"
            + tr("Log file: {0}").format(LOG_FILE)
        )
        box.setDetailedText(text)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()
    except Exception:
        pass
