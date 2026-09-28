"""db/seed_demo.py::_register_or_reuse_existing — regression coverage for the
IntegrityError crash reported when db/seed_mass_content.py::seed_if_empty()
(app.py's auto-seed-on-boot hook) loses a race against a concurrent call
creating the same admin/persona user first.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from auth import service as auth_service
from db import database, seed_demo
from db.repositories import users_repo


@pytest.fixture()
def temp_db(tmp_path):
    db_path = tmp_path / "test_seed_demo.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    yield
    database.get_engine().dispose()


def test_register_or_reuse_existing_creates_a_new_user_normally(temp_db):
    user = seed_demo._register_or_reuse_existing(
        username="brand_new", password="Password123!", email="brand_new@example.com", full_name="Brand New"
    )
    assert user.username == "brand_new"
    assert users_repo.get_by_username("brand_new") is not None


def test_register_or_reuse_existing_recovers_from_concurrent_integrity_error(temp_db, monkeypatch):
    """Simulates the exact race this fix targets: a concurrent caller (e.g.
    another Streamlit session's seed_if_empty()) inserts the same user
    between this call's own pre-check and its register() attempt, so the
    underlying INSERT raises IntegrityError. Must recover by reusing the row
    that actually won the race instead of propagating the crash."""
    winner = users_repo.create_user(
        username="admin", password_hash="irrelevant-hash", email="admin@gmail.com", full_name="admin admin"
    )

    def _fake_register(**kwargs):
        raise IntegrityError("INSERT INTO users ...", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(auth_service, "register", _fake_register)

    user = seed_demo._register_or_reuse_existing(
        username="admin", password="admin123", email="admin@gmail.com", full_name="admin admin"
    )
    assert user.id == winner.id


def test_register_or_reuse_existing_recovers_from_concurrent_validation_error(temp_db, monkeypatch):
    """Same race, but caught earlier by register()'s own pre-commit
    validate_unique check (auth.service.ValidationError) instead of a raw
    DB-level IntegrityError — both must be treated identically."""
    winner = users_repo.create_user(
        username="tech_enthusiast", password_hash="irrelevant-hash",
        email="tech.enthusiast@uncapped.dev", full_name="Priya Shah",
    )

    def _fake_register(**kwargs):
        raise auth_service.ValidationError({"username": "This username is already taken."})

    monkeypatch.setattr(auth_service, "register", _fake_register)

    user = seed_demo._register_or_reuse_existing(
        username="tech_enthusiast", password="Password123!",
        email="tech.enthusiast@uncapped.dev", full_name="Priya Shah",
    )
    assert user.id == winner.id


def test_register_or_reuse_existing_reraises_a_genuine_unrelated_failure(temp_db, monkeypatch):
    """An IntegrityError NOT explained by the row already existing (e.g. some
    other genuine data problem) must still surface, not be silently
    swallowed."""

    def _fake_register(**kwargs):
        raise IntegrityError("INSERT INTO users ...", {}, Exception("some other constraint failed"))

    monkeypatch.setattr(auth_service, "register", _fake_register)

    with pytest.raises(IntegrityError):
        seed_demo._register_or_reuse_existing(
            username="nobody_actually_created_this",
            password="Password123!",
            email="nobody@example.com",
            full_name="Nobody",
        )


def test_run_demo_seed_survives_admin_user_created_concurrently(temp_db, monkeypatch):
    """End-to-end: run_demo_seed() (called by seed_if_empty()) must not crash
    even if the admin user was created by a concurrent process between this
    call's own pre-check and its registration attempt."""
    winner = users_repo.create_user(
        username="admin", password_hash="irrelevant-hash", email="admin@gmail.com", full_name="admin admin"
    )

    real_register = auth_service.register
    call_count = {"n": 0}

    def _flaky_register(**kwargs):
        call_count["n"] += 1
        if kwargs["username"] == "admin":
            raise IntegrityError("INSERT INTO users ...", {}, Exception("UNIQUE constraint failed"))
        return real_register(**kwargs)

    monkeypatch.setattr(auth_service, "register", _flaky_register)

    summary = seed_demo.run_demo_seed()

    assert not summary["skipped"]
    assert users_repo.get_by_username("admin").id == winner.id
