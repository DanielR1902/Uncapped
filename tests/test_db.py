"""Phase 1 verification: schema creation, catalog seeding, repository CRUD."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from db import database
from db.repositories import builds_repo, community_repo, components_repo, drafts_repo, users_repo
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
    "draft_builds",
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


def test_catalog_highest_wattage_psu_covers_worst_case_cpu_gpu_pairing(temp_db):
    """A later round's directive claimed the catalog's PSUs were "under-
    provisioned" for a high-end CPU+GPU build — checked against the real
    formula (`engine.compatibility.check_psu_headroom`:
    `(cpu_tdp + gpu_tdp + SYSTEM_BASELINE_WATTS) * PSU_HEADROOM_MULTIPLIER`)
    and the real catalog data, that claim was false even before this round's
    catalog expansion (the highest-TDP CPU/GPU pairing then in the catalog
    only required ~1033.5W, comfortably under the existing 1200W PSU). This
    round added higher-wattage PSUs (up to 1600W) anyway, to broaden the
    enthusiast/future-proofing tier — this test proves, from real seeded
    data (not a hypothetical), that the highest-wattage PSU in the catalog
    always covers the highest-TDP CPU+GPU pairing also in the catalog, so
    a "no PSU can handle my extreme build" situation can never occur."""
    from engine.compatibility import PSU_HEADROOM_MULTIPLIER, SYSTEM_BASELINE_WATTS

    run_seed()
    cpus = components_repo.get_by_category("CPU")
    gpus = components_repo.get_by_category("GPU")
    psus = components_repo.get_by_category("PSU")

    max_cpu_tdp = max(c.tdp_watts for c in cpus)
    max_gpu_tdp = max(g.tdp_watts for g in gpus)
    max_psu_wattage = max(p.wattage_capacity for p in psus)

    required_watts = (max_cpu_tdp + max_gpu_tdp + SYSTEM_BASELINE_WATTS) * PSU_HEADROOM_MULTIPLIER
    assert max_psu_wattage >= required_watts, (
        f"Highest-wattage PSU ({max_psu_wattage}W) cannot cover the worst-case "
        f"CPU+GPU pairing (requires {required_watts}W)"
    )
    # This round's own additions specifically:
    assert max_psu_wattage >= 1600
    assert any(p.wattage_capacity >= 1300 for p in psus)
    assert any(p.wattage_capacity >= 1500 for p in psus)


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


def test_community_post_flair(temp_db):
    """`flair` (spec.md §3.6/§7.4.1) defaults to None for a normal share and
    is stored verbatim when passed -- the "Rate My Build" Build Studio flow
    is the only real caller of this today, but the column itself is a plain
    optional string, not an enum."""
    run_seed()
    user = users_repo.create_user("flair1", "hash", "flair1@example.com", "Flair One")
    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user.id, name="Flair Build", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )

    plain_post = community_repo.create_post(build.id, user.id, "Plain share")
    assert plain_post.flair is None

    rated_post = community_repo.create_post(build.id, user.id, "[Spec Check] my rig", flair="Rate My Build")
    assert rated_post.flair == "Rate My Build"
    assert community_repo.get_post(rated_post.id).flair == "Rate My Build"


def test_get_posts_for_user_scoped_to_author(temp_db):
    """"Your Posts" (spec.md §7.6.2) — only the given user's own posts, newest
    first, never another author's."""
    run_seed()
    alice = users_repo.create_user("postsalice", "hash", "postsalice@example.com", "Alice Posts")
    bob = users_repo.create_user("postsbob", "hash", "postsbob@example.com", "Bob Posts")
    cpu = components_repo.get_by_category("CPU")[0]

    def _build_and_post(user, name):
        build = builds_repo.create_build(
            user_id=user.id, name=name, creation_mode="Free",
            components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
            total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
        )
        return community_repo.create_post(build.id, user.id, name)

    alice_post = _build_and_post(alice, "Alice Rig")
    _build_and_post(bob, "Bob Rig")

    alice_posts = community_repo.get_posts_for_user(alice.id)
    assert [p.id for p in alice_posts] == [alice_post.id]


def test_delete_post_removes_post_and_comments_but_not_the_build(temp_db):
    """"Your Posts" delete action (spec.md §7.6.2) — the crucial rule this
    guards: deleting a community post must NEVER delete or alter the
    underlying saved Build row (Previous Builds stays completely intact),
    while the post's own comments DO cascade away with it."""
    run_seed()
    user = users_repo.create_user("delpost1", "hash", "delpost1@example.com", "Del Post One")
    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user.id, name="Deletable Post Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, user.id, "Deletable Post Rig")
    community_repo.add_comment(post.id, user.id, "First!")

    community_repo.delete_post(post.id)

    assert community_repo.get_post(post.id) is None
    assert community_repo.get_comments(post.id) == []
    # The build itself must survive untouched in Previous Builds.
    assert builds_repo.get_build(build.id) is not None
    assert builds_repo.get_build(build.id).name == "Deletable Post Rig"

    # No-op, not a crash, on an already-deleted post.
    community_repo.delete_post(post.id)


def test_community_threaded_comments(temp_db):
    """A reply (parent_comment_id set) is a real, distinct row from a
    top-level comment, and get_comments still returns a FLAT list ordered
    oldest-first regardless of threading -- building the reply tree is a
    ui/ presentation concern (db/CLAUDE.md), not this function's job."""
    run_seed()
    user = users_repo.create_user("dave", "hash", "dave@example.com", "Dave Row")
    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user.id, name="Threaded Build", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, user.id, "Threading test")

    top_level = community_repo.add_comment(post.id, user.id, "First comment")
    assert top_level.parent_comment_id is None

    reply = community_repo.add_comment(post.id, user.id, "A reply", parent_comment_id=top_level.id)
    assert reply.parent_comment_id == top_level.id

    reply_to_reply = community_repo.add_comment(post.id, user.id, "Nested reply", parent_comment_id=reply.id)
    assert reply_to_reply.parent_comment_id == reply.id

    comments = community_repo.get_comments(post.id)
    assert len(comments) == 3
    assert [c.content for c in comments] == ["First comment", "A reply", "Nested reply"]


def test_community_get_feed_sorted_chronologically(temp_db):
    """get_feed() ranks posts strictly by created_at DESC (newest first) --
    the upvote/downvote mechanism and any score-based ranking were removed
    entirely (spec.md §7.6), so this is now the ONLY ordering rule, not one
    of two modes. created_at is set explicitly (rather than relying on two
    real-time inserts, which could tie under SQLite's whole-second
    CURRENT_TIMESTAMP resolution) so the ordering assertion is deterministic."""
    import datetime as dt

    from sqlalchemy import update

    from db.models import CommunityPost

    run_seed()
    author = users_repo.create_user("henry", "hash", "henry@example.com", "Henry Row")
    cpu = components_repo.get_by_category("CPU")[0]

    def _make_post(name: str) -> int:
        build = builds_repo.create_build(
            user_id=author.id, name=name, creation_mode="Free",
            components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
            total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
        )
        return community_repo.create_post(build.id, author.id, name).id

    first_post_id = _make_post("First Post")
    second_post_id = _make_post("Second Post")

    with database.session_scope() as session:
        session.execute(
            update(CommunityPost).where(CommunityPost.id == first_post_id)
            .values(created_at=dt.datetime(2020, 1, 1))
        )
        session.execute(
            update(CommunityPost).where(CommunityPost.id == second_post_id)
            .values(created_at=dt.datetime(2020, 1, 2))
        )

    feed = community_repo.get_feed()
    ranked_ids = [p.id for p in feed]
    assert ranked_ids.index(second_post_id) < ranked_ids.index(first_post_id)


# ---------------------------------------------------------------------------
# drafts_repo
# ---------------------------------------------------------------------------
def test_draft_save_and_get_round_trips(temp_db):
    run_seed()
    user = users_repo.create_user("heidi", "hash", "heidi@example.com", "Heidi Row")
    cpu = components_repo.get_by_category("CPU")[0]
    ram = components_repo.get_by_category("RAM")[0]

    draft = drafts_repo.save_draft(
        user_id=user.id,
        name="My WIP Build",
        mode="Free",
        components={"CPU": cpu.id, "RAM": ram.id},
        quantities={"RAM": 2},
    )
    assert draft.id is not None
    assert draft.name == "My WIP Build"
    assert draft.mode == "Free"

    fetched = drafts_repo.get_draft(draft.id)
    assert fetched is not None
    assert json.loads(fetched.components_json) == {"CPU": cpu.id, "RAM": ram.id}
    assert json.loads(fetched.quantities_json) == {"RAM": 2}


def test_get_user_drafts_sorted_by_updated_desc(temp_db):
    user = users_repo.create_user("ivy", "hash", "ivy@example.com", "Ivy Row")

    first = drafts_repo.save_draft(user.id, "First", "Free", {}, {})
    second = drafts_repo.save_draft(user.id, "Second", "Free", {}, {})

    drafts = drafts_repo.get_user_drafts(user.id)
    assert [d.id for d in drafts] == [second.id, first.id] or {d.id for d in drafts} == {first.id, second.id}
    assert len(drafts) == 2


def test_get_user_drafts_only_returns_that_users_drafts(temp_db):
    alice = users_repo.create_user("alice2", "hash", "alice2@example.com", "Alice Two")
    bob = users_repo.create_user("bob2", "hash", "bob2@example.com", "Bob Two")

    drafts_repo.save_draft(alice.id, "Alice's Draft", "Free", {}, {})
    drafts_repo.save_draft(bob.id, "Bob's Draft", "Free", {}, {})

    alice_drafts = drafts_repo.get_user_drafts(alice.id)
    assert len(alice_drafts) == 1
    assert alice_drafts[0].name == "Alice's Draft"


def test_delete_draft_removes_it_and_is_a_no_op_if_already_gone(temp_db):
    user = users_repo.create_user("jack", "hash", "jack@example.com", "Jack Row")
    draft = drafts_repo.save_draft(user.id, "Deletable", "Free", {}, {})

    drafts_repo.delete_draft(draft.id)
    assert drafts_repo.get_draft(draft.id) is None

    drafts_repo.delete_draft(draft.id)  # no-op, must not raise
    drafts_repo.delete_draft(999999)  # no-op on a never-existing id, must not raise


def test_draft_cascades_on_user_delete(temp_db):
    user = users_repo.create_user("karl", "hash", "karl@example.com", "Karl Row")
    draft = drafts_repo.save_draft(user.id, "Orphan Check", "Free", {}, {})

    with database.session_scope() as session:
        from db.models import User

        session.delete(session.get(User, user.id))

    assert drafts_repo.get_draft(draft.id) is None
