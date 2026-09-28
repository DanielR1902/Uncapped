"""db/seed_mass_content.py::seed_if_empty — the auto-seed hook app.py calls on
every startup so a freshly-deployed, genuinely empty database (e.g. Streamlit
Community Cloud, no local db/uncapped.db bundled) never presents a completely
empty Community feed / My Builds page, while never touching an
already-populated one.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db import database, seed_mass_content
from db.models import Build, CommunityComment, CommunityPost, User
from db.repositories import builds_repo, components_repo
from db.seed_mass_content import seed_if_empty


@pytest.fixture()
def temp_db(tmp_path):
    db_path = tmp_path / "test_seed_bootstrap.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    yield
    database.get_engine().dispose()


def _counts() -> dict[str, int]:
    with database.session_scope() as session:
        return {
            "users": session.execute(select(func.count()).select_from(User)).scalar(),
            "builds": session.execute(select(func.count()).select_from(Build)).scalar(),
            "posts": session.execute(select(func.count()).select_from(CommunityPost)).scalar(),
            "comments": session.execute(select(func.count()).select_from(CommunityComment)).scalar(),
        }


def test_seed_if_empty_bootstraps_a_genuinely_fresh_database(temp_db):
    """A brand-new database — no catalog, no users, no builds at all — must
    come up fully populated (users, 70 builds, 30 published posts, threaded
    comments) after a single call, matching db/seed_mass_content.py's own
    documented contract."""
    before = _counts()
    assert before == {"users": 0, "builds": 0, "posts": 0, "comments": 0}

    summary = seed_if_empty()

    assert summary is not None
    assert summary["total_builds"] == 70
    assert summary["admin_builds"] == 15
    assert summary["published"] == 30

    after = _counts()
    assert after["users"] == 6  # admin + 5 personas
    assert after["builds"] == 70
    assert after["posts"] == 30
    assert after["comments"] > 0


def test_seed_if_empty_backs_off_gracefully_on_concurrent_integrity_error(temp_db, monkeypatch):
    """Regression for the reported startup crash: if a concurrent
    seed_if_empty() call (another Streamlit session cold-starting the same
    freshly-deployed, empty database at once) causes an IntegrityError
    somewhere in this call's own seeding attempt, this call must back off
    and return None instead of propagating the crash up through app.py's
    module-level startup code."""

    def _raise_integrity_error():
        raise IntegrityError("INSERT INTO builds ...", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(seed_mass_content, "run_mass_content_seed", _raise_integrity_error)

    result = seed_if_empty()

    assert result is None


def test_seed_if_empty_is_a_no_op_once_any_build_exists(temp_db):
    """The core contract this hook exists for: once the database already has
    real content, calling this again on every later Streamlit rerun must
    never wipe or duplicate anything — a plain no-op returning None."""
    from db.repositories import users_repo

    components_repo_seed = components_repo.get_by_category("CPU")
    if not components_repo_seed:
        from db.seed import run_seed

        run_seed()
        components_repo_seed = components_repo.get_by_category("CPU")

    user = users_repo.create_user(
        username="real_user", password_hash="hash", email="real@example.com", full_name="Real User"
    )
    cpu = components_repo_seed[0]
    builds_repo.create_build(
        user_id=user.id,
        name="A Real User's Own Build",
        creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd,
        compatibility_score=100.0,
        is_public=False,
    )

    before = _counts()
    assert before["builds"] == 1

    result = seed_if_empty()

    assert result is None
    after = _counts()
    assert after == before  # completely untouched — no wipe, no reseed
