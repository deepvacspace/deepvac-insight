"""Smoke tests of the UI fixtures."""

import pytest
from PySide6.QtCore import QSettings

pytestmark = pytest.mark.ui


def test_qsettings_isolated_does_not_leak_into_real_registry(qsettings_isolated):
    QSettings("DeepVac", "Insight").setValue("ui/theme_probe", "isolated-value")
    assert QSettings("DeepVac", "Insight").value("ui/theme_probe") == "isolated-value"


def test_no_modal_dialogs_records_instead_of_blocking(no_modal_dialogs):
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.critical(None, "Title", "Text")
    assert len(no_modal_dialogs) == 1
    assert no_modal_dialogs[0]["kind"] == "critical"


def test_deepvac_ui_yields_isolated_data_dir(deepvac_ui):
    assert deepvac_ui.name == "deepvac_data"
