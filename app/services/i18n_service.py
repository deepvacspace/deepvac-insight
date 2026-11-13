"""Language selection and Qt translator loading."""

from PySide6.QtCore import QTranslator

from app.common import RESOURCES_DIR

I18N_DIR = RESOURCES_DIR / "i18n"

AVAILABLE_LANGUAGES = {
    "en": "English",
    "es": "Español",
    "de": "Deutsch",
}

_installed_translator = None


def install_language(app, code):
    """Installs the translator for a language."""
    global _installed_translator

    if _installed_translator is not None:
        app.removeTranslator(_installed_translator)
        _installed_translator = None

    if code == "en":
        return False

    qm_path = I18N_DIR / f"deepvac_{code}.qm"
    if not qm_path.exists():
        return False

    translator = QTranslator(app)
    if not translator.load(str(qm_path)):
        return False

    app.installTranslator(translator)
    _installed_translator = translator
    return True
