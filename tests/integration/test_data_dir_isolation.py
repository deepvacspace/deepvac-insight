"""Tests that the data directory fixture isolates every database."""

import pytest

pytestmark = pytest.mark.integration


def test_auth_service_writes_inside_isolated_dir(deepvac_data_dir):
    from app.services import auth_service

    user = auth_service.create_user("Test User", "isolated@example.com", "password123")
    assert auth_service.AUTH_DB.exists()
    assert auth_service.AUTH_DB.is_relative_to(deepvac_data_dir)
    assert auth_service.authenticate("isolated@example.com", "password123")["id"] == user["id"]


def test_annotations_service_writes_inside_isolated_dir(deepvac_data_dir):
    from app.services import annotations_service

    conn = annotations_service.connect_annotations()
    conn.close()
    assert annotations_service.ANNOTATIONS_DB.exists()
    assert annotations_service.ANNOTATIONS_DB.is_relative_to(deepvac_data_dir)


def test_backup_service_writes_inside_isolated_dir(deepvac_data_dir):
    from app.services import annotations_service, backup_service

    annotations_service.connect_annotations().close()
    results = backup_service.backup_all(force=True)
    assert results
    for path in results:
        assert path.is_relative_to(deepvac_data_dir)


def test_two_tests_get_independently_isolated_directories(deepvac_data_dir, tmp_path):
    from app.services import auth_service

    assert not list(deepvac_data_dir.glob("*.sqlite3"))
    auth_service.create_user("Another User", "another@example.com", "password123")
    assert auth_service.AUTH_DB.exists()
