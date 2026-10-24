"""Invokes the real application startup path (`python main.py
--smoke-test`) as an actual fresh subprocess, rather than in-process --
app.app._run_smoke_test() constructs its own QApplication, and pytest-qt's
shared session-scoped qapp fixture makes reusing that same process a real
source of double-QApplication/shared-Qt-static complications for no
benefit. A subprocess is also what Phase 6's CI frozen-build smoke test
uses, so this is the same invocation being exercised at the source level.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_smoke_test(tmp_path, extra_args=(), extra_env=None):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["DEEPVAC_DATA_DIR"] = str(tmp_path / "deepvac_data")
    env["DEEPVAC_DATA_ROOT"] = str(tmp_path / "empty_workspace")
    (tmp_path / "empty_workspace").mkdir(exist_ok=True)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--smoke-test", *extra_args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_smoke_test_exits_zero(tmp_path):
    result = _run_smoke_test(tmp_path, extra_args=["--no-splash"])
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "[smoke-test] OK" in result.stdout


def test_smoke_test_with_splash_also_exits_zero(tmp_path):
    result = _run_smoke_test(tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_logging_initialized_creates_log_file(tmp_path):
    _run_smoke_test(tmp_path, extra_args=["--no-splash"])
    log_file = tmp_path / "deepvac_data" / "logs" / "app.log"
    assert log_file.exists()


def test_runtime_paths_isolated_nothing_written_outside_data_dir(tmp_path):
    _run_smoke_test(tmp_path, extra_args=["--no-splash"])
    written = list((tmp_path / "deepvac_data").rglob("*"))
    assert written  # something was written (logs, at least)
    # Nothing escaped to tmp_path directly (only the two subdirs we set up).
    top_level = {p.name for p in tmp_path.iterdir()}
    assert top_level <= {"deepvac_data", "empty_workspace"}


def test_translation_initialization_does_not_crash_for_es_and_de(qapp):
    # In-process rather than via subprocess: forcing a non-English language
    # for the subprocess would require redirecting QSettings to a registry-
    # free format first (see qsettings_isolated), which is unnecessary
    # complexity just to prove install_language() itself doesn't raise for
    # either bundled translation.
    from app.services import i18n_service

    try:
        for code in ("en", "es", "de"):
            installed = i18n_service.install_language(qapp, code)
            assert installed == (code != "en")  # "en" needs no .qm, es/de do
    finally:
        i18n_service.install_language(qapp, "en")  # later tests read English texts
