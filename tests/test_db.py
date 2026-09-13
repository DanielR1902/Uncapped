"""Phase 1 verification: schema creation, catalog seeding, repository CRUD."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from db import database
from db.repositories import builds_repo, community_repo, components_repo, users_repo
from db.seed import run_seed

EXPECTED_TABLES = {
    "users",
    "components",
    "workload_mappings",
    "builds",
    "build_components",
    "community_posts",
    "community_comments",
    "llm_cache",
}


@pytest.fixture()
def temp_db(tmp_path):
    db_path = tmp_path / "test_uncapped.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    yield
    database.get_engine().dispose()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_all_tables_created(temp_db):
    inspector = inspect(database.get_engine())
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES.issubset(tables)


def test_foreign_keys_enforced(temp_db):
    with pytest.raises(IntegrityError):
        with database.session_scope() as session:
            from db.models import Build

            session.add(Build(user_id=999999, name="orphan", creation_mode="Free", total_cost=0, compatibility_score=100))


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def test_seed_populates_catalog(temp_db):
    summary = run_seed()
    assert summary["skipped"] is False
    assert summary["components"] >= 100
    assert summary["workload_mappings"] > 0

    for category in ["CPU", "Motherboard", "GPU", "RAM", "Storage", "PSU", "Case", "Cooler"]:
        rows = components_repo.get_by_category(category)
        assert len(rows) >= 10, f"{category} has too few seeded rows ({len(rows)})"

    for category in ["NetworkCard", "SoundCard", "OpticalDrive"]:
        rows = components_repo.get_by_category(category)
        assert len(rows) >= 2, f"{category} has too few seeded rows ({len(rows)})"


def test_seed_is_idempotent(temp_db):
    first = run_seed()
    second = run_seed()
    assert second["skipped"] is True
    assert second["components"] == first["components"]


def test_seeded_components_have_required_fields(temp_db):
    run_seed()
    cpus = components_repo.get_by_category("CPU")
    for cpu in cpus:
        assert cpu.socket is not None
        assert cpu.tdp_watts is not None
        assert cpu.benchmark_score is not None
        assert cpu.price_usd > 0

    motherboards = components_repo.get_by_category("Motherboard")
    boards_by_socket = {mb.socket for mb in motherboards}
    cpu_sockets = {cpu.socket for cpu in cpus}
    # every CPU socket must have at least one matching motherboard, or the
    # budget/free-build engine would have no valid pairing to offer.
    assert cpu_sockets.issubset(boards_by_socket)


def test_workload_matches_query(temp_db):
    run_seed()
    gaming_high = components_repo.get_workload_matches("Gaming", "High")
    assert len(gaming_high) > 0
    for component, mapping in gaming_high:
        assert mapping.workload_profile == "Gaming"
        assert mapping.tier == "High"
        assert component.category in {
            "CPU", "Motherboard", "GPU", "RAM", "Storage", "PSU", "Case", "Cooler",
            "NetworkCard", "SoundCard", "OpticalDrive",
        }


# ---------------------------------------------------------------------------
# users_repo
# ---------------------------------------------------------------------------
def test_user_crud(temp_db):
    user = users_repo.create_user("alice", "hashed-pw", "alice@example.com", "Alice Doe")
    assert user.id is not None

    fetched = users_repo.get_by_username("alice")
    assert fetched is not None
    assert fetched.email == "alice@example.com"

    assert users_repo.get_by_email("alice@example.com").id == user.id
    assert users_repo.get_by_id(user.id).username == "alice"
    assert users_repo.get_by_username("nobody") is None


def test_duplicate_username_rejected(temp_db):
    users_repo.create_user("dave", "hash1", "dave@example.com", "Dave A")
    with pytest.raises(IntegrityError):
        users_repo.create_user("dave", "hash2", "dave2@example.com", "Dave B")


def test_duplicate_email_rejected(temp_db):
    users_repo.create_user("erin", "hash1", "erin@example.com", "Erin A")
    with pytest.raises(IntegrityError):
        users_repo.create_user("erin2", "hash2", "erin@example.com", "Erin B")


# ---------------------------------------------------------------------------
# builds_repo
# ---------------------------------------------------------------------------
def test_build_crud(temp_db):
    run_seed()
    user = users_repo.create_user("bob", "hashed-pw", "bob@example.com", "Bob Row")
    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]

    build = builds_repo.create_build(
        user_id=user.id,
        name="My First Build",
        creation_mode="Free",
        components=[
            builds_repo.BuildComponentInput(component_id=cpu.id),
            builds_repo.BuildComponentInput(component_id=gpu.id),
        ],
        total_cost=cpu.price_usd + gpu.price_usd,
        compatibility_score=100.0,
    )
    assert build.id is not None
    assert len(build.components) == 2

    fetched = builds_repo.get_build(build.id)
    assert fetched is not None
    assert {bc.component.category for bc in fetched.components} == {"CPU", "GPU"}

    builds_repo.set_public(build.id, True)
    assert builds_repo.get_build(build.id).is_public is True

    public_builds = builds_repo.get_public_builds()
    assert any(b.id == build.id for b in public_builds)


def test_get_builds_for_user_sorting(temp_db):
    run_seed()
    user = users_repo.create_user("frank", "hash", "frank@example.com", "Frank Row")
    cpu = components_repo.get_by_category("CPU")[0]

    cheap = builds_repo.create_build(
        user_id=user.id, name="Cheap", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=100.0, compatibility_score=100.0,
    )
    expensive = builds_repo.create_build(
        user_id=user.id, name="Expensive", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=900.0, compatibility_score=100.0,
    )

    desc = builds_repo.get_builds_for_user(user.id, builds_repo.BuildsFilter(sort="cost_desc"))
    assert desc[0].id == expensive.id

    asc = builds_repo.get_builds_for_user(user.id, builds_repo.BuildsFilter(sort="cost_asc"))
    assert asc[0].id == cheap.id


def test_build_rejects_unknown_component(temp_db):
    user = users_repo.create_user("grace", "hash", "grace@example.com", "Grace Row")
    with pytest.raises(ValueError):
        builds_repo.create_build(
            user_id=user.id, name="Bad Build", creation_mode="Free",
            components=[builds_repo.BuildComponentInput(component_id=999999)],
            total_cost=0.0, compatibility_score=100.0,
        )


# ---------------------------------------------------------------------------
# community_repo
# ---------------------------------------------------------------------------
def test_community_post_and_comment_crud(temp_db):
    run_seed()
    user = users_repo.create_user("carol", "hash", "carol@example.com", "Carol Row")
    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user.id, name="Community Build", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )

    post = community_repo.create_post(build.id, user.id, "Check out my build!", "Some notes")
    assert post.id is not None
    assert post.build.id == build.id

    feed = community_repo.get_feed()
    assert any(p.id == post.id for p in feed)

    comment = community_repo.add_comment(post.id, user.id, "Nice build!")
    assert comment.id is not None

    comments = community_repo.get_comments(post.id)
    assert len(comments) == 1
    assert comments[0].content == "Nice build!"
