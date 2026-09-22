"""Tests for auth_service's hub account linking."""

import sqlite3

import pytest

from app.services import auth_service

pytestmark = pytest.mark.unit


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    db_path = tmp_path / "deepvac_users.sqlite3"
    monkeypatch.setattr(auth_service, "AUTH_DB", db_path)
    return db_path


def test_fresh_database_includes_hub_columns(auth_db):
    user = auth_service.create_user("New User", "new@example.com", "password123")
    assert user["hub_user_id"] is None
    assert user["hub_email"] is None


def test_existing_pre_hub_schema_database_gets_columns_added(auth_db):
    """Adds the hub_* columns to a pre-existing users table without losing data."""
    conn = sqlite3.connect(auth_db)
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            remember_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO users (name, email, password_hash, password_salt, created_at, updated_at) "
        "VALUES ('Old User', 'old@example.com', 'aa', 'bb', 'now', 'now')"
    )
    conn.commit()
    conn.close()

    user = auth_service.authenticate("old@example.com", "irrelevant")
    assert user is None

    columns = {
        row["name"] for row in auth_service.connect_auth().execute("PRAGMA table_info(users)")
    }
    assert {"hub_user_id", "hub_email", "hub_org_id", "hub_org_name", "hub_linked_at"} <= columns

    row = (
        auth_service.connect_auth()
        .execute("SELECT * FROM users WHERE email = 'old@example.com'")
        .fetchone()
    )
    assert row["name"] == "Old User"


def test_link_hub_account_sets_fields(auth_db):
    user = auth_service.create_user("Linkable", "linkable@example.com", "password123")

    updated = auth_service.link_hub_account(
        user["id"],
        hub_user_id="hub-user-1",
        hub_email="linkable@hub.example.com",
        hub_org_id="hub-org-1",
        hub_org_name="Acme Labs",
    )

    assert updated["hub_user_id"] == "hub-user-1"
    assert updated["hub_email"] == "linkable@hub.example.com"
    assert updated["hub_org_id"] == "hub-org-1"
    assert updated["hub_org_name"] == "Acme Labs"
    assert updated["hub_linked_at"] is not None


def test_link_hub_account_rejects_unknown_user(auth_db):
    with pytest.raises(auth_service.AuthError):
        auth_service.link_hub_account(
            999999,
            hub_user_id="hub-user-1",
            hub_email="x@example.com",
            hub_org_id="hub-org-1",
            hub_org_name="Acme Labs",
        )


def test_unlink_hub_account_clears_fields(auth_db):
    user = auth_service.create_user("Linked", "linked@example.com", "password123")
    auth_service.link_hub_account(
        user["id"],
        hub_user_id="hub-user-1",
        hub_email="linked@hub.example.com",
        hub_org_id="hub-org-1",
        hub_org_name="Acme Labs",
    )

    updated = auth_service.unlink_hub_account(user["id"])

    assert updated["hub_user_id"] is None
    assert updated["hub_email"] is None
    assert updated["hub_org_id"] is None
    assert updated["hub_org_name"] is None
    assert updated["hub_linked_at"] is None
