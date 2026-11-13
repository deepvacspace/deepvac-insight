"""Checks the test environment."""

import sys


def test_python_version_matches_requires_python():
    assert sys.version_info >= (3, 10)


def test_app_package_importable():
    import app  # noqa: F401
