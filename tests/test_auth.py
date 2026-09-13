"""Phase 3 verification: registration, hashing, dual-identifier login, duplicate rejection."""
from __future__ import annotations

import pytest

from auth import service
from db import database
from db.repositories import users_repo


@pytest.fixture()
def temp_db(tmp_path):
    db_path = tmp_path / "test_auth.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    yield
    database.get_engine().dispose()


# ---------------------------------------------------------------------------
# Registration + hashing
# ---------------------------------------------------------------------------
def test_register_creates_user_with_hashed_password(temp_db):
    user = service.register("alice", "Passw0rd!", "alice@example.com", "Alice Doe")
    assert user.id is not None
    assert user.username == "alice"
    assert user.email == "alice@example.com"

    row = users_repo.get_by_username("alice")
    assert row.password_hash != "Passw0rd!"
    assert row.password_hash.startswith(("$2a$", "$2b$"))


def test_register_rejects_short_password(temp_db):
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("bob", "short1", "bob@example.com", "Bob Row")
    assert "password" in exc_info.value.errors


def test_register_rejects_password_without_digit(temp_db):
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("carol", "onlyletters", "carol@example.com", "Carol Row")
    assert "password" in exc_info.value.errors


def test_register_rejects_invalid_email(temp_db):
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("dave", "Passw0rd!", "not-an-email", "Dave A")
    assert "email" in exc_info.value.errors


def test_register_rejects_short_username(temp_db):
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("ab", "Passw0rd!", "ab@example.com", "AB")
    assert "username" in exc_info.value.errors


def test_register_rejects_blank_full_name(temp_db):
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("ellen", "Passw0rd!", "ellen@example.com", "   ")
    assert "full_name" in exc_info.value.errors


# ---------------------------------------------------------------------------
# Duplicate rejection
# ---------------------------------------------------------------------------
def test_duplicate_username_rejected(temp_db):
    service.register("erin", "Passw0rd!", "erin@example.com", "Erin A")
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("erin", "Passw0rd2!", "erin2@example.com", "Erin B")
    assert "username" in exc_info.value.errors


def test_duplicate_email_rejected(temp_db):
    service.register("frank", "Passw0rd!", "frank@example.com", "Frank A")
    with pytest.raises(service.ValidationError) as exc_info:
        service.register("frank2", "Passw0rd!", "frank@example.com", "Frank B")
    assert "email" in exc_info.value.errors


def test_is_username_taken_and_is_email_taken(temp_db):
    assert service.is_username_taken("grace") is False
    assert service.is_email_taken("grace@example.com") is False

    service.register("grace", "Passw0rd!", "grace@example.com", "Grace Row")

    assert service.is_username_taken("grace") is True
    assert service.is_email_taken("grace@example.com") is True


def test_validate_unique_reports_both_conflicts_together(temp_db):
    service.register("henry", "Passw0rd!", "henry@example.com", "Henry Row")
    errors = service.validate_unique("henry", "henry@example.com")
    assert "username" in errors
    assert "email" in errors


# ---------------------------------------------------------------------------
# Dual-identifier login
# ---------------------------------------------------------------------------
def test_authenticate_by_username(temp_db):
    service.register("heidi", "Passw0rd!", "heidi@example.com", "Heidi Row")
    user = service.authenticate("heidi", "Passw0rd!")
    assert user is not None
    assert user.username == "heidi"


def test_authenticate_by_email(temp_db):
    service.register("ivan", "Passw0rd!", "ivan@example.com", "Ivan Row")
    user = service.authenticate("ivan@example.com", "Passw0rd!")
    assert user is not None
    assert user.username == "ivan"


def test_authenticate_wrong_password_returns_none(temp_db):
    service.register("judy", "Passw0rd!", "judy@example.com", "Judy Row")
    assert service.authenticate("judy", "WrongPass1") is None


def test_authenticate_unknown_identifier_returns_none(temp_db):
    assert service.authenticate("nobody", "whatever1") is None


# ---------------------------------------------------------------------------
# Session payload sanitization
# ---------------------------------------------------------------------------
def test_to_session_payload_excludes_password_hash(temp_db):
    user = service.register("kevin", "Passw0rd!", "kevin@example.com", "Kevin Row")
    payload = service.to_session_payload(user)

    assert "password" not in payload
    assert "password_hash" not in payload
    assert payload == {
        "id": user.id,
        "username": "kevin",
        "email": "kevin@example.com",
        "full_name": "Kevin Row",
    }
