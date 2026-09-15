"""Phase 4 verification: state-transition unit tests + end-to-end UI smoke tests.

The smoke tests drive the real app.py via Streamlit's official AppTest harness
(streamlit.testing.v1) — no mocked widgets, actual script execution against a
temp seeded database. This is slower than a unit test but is what actually
proves "the app boots and the click-paths work" rather than "the functions
compile."
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import database
from db.models import Component
from db.seed import run_seed
from ui import state

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture()
def seeded_db(tmp_path):
    db_path = tmp_path / "test_ui.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    run_seed()
    yield
    database.get_engine().dispose()


@pytest.fixture()
def demo_seeded_db(tmp_path):
    """Catalog + the admin/persona demo accounts (db/seed_demo.py) — used by
    tests that need the standing admin login rather than a freshly registered
    user."""
    from db.seed_demo import run_demo_seed

    db_path = tmp_path / "test_ui_demo.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    run_demo_seed()
    yield
    database.get_engine().dispose()


def _register(at: AppTest, username: str, email: str, full_name: str, password: str = "Passw0rd!") -> AppTest:
    at.get_by_key("open_register").click().run()
    at.get_by_key("register_username").input(username)
    at.get_by_key("register_email").input(email)
    at.get_by_key("register_full_name").input(full_name)
    at.get_by_key("register_password").input(password)
    at.get_by_key("register_submit").click()
    at.run()
    return at


# ---------------------------------------------------------------------------
# ui/state.py — pure data helpers (no Streamlit runtime needed)
# ---------------------------------------------------------------------------
def test_new_build_draft_shape():
    draft = state.new_build_draft("Free")
    assert draft["creation_mode"] == "Free"
    assert draft["components"] == {}
    assert draft["tier"] == "Mid"


def test_set_and_remove_component():
    draft = state.new_build_draft("Free")
    fake_cpu = Component(id=42, category="CPU", name="Fake CPU", brand="Test", price_usd=100.0, specs_json="{}")

    state.set_component(draft, "CPU", fake_cpu)
    assert draft["components"]["CPU"] == 42

    state.remove_component(draft, "CPU")
    assert "CPU" not in draft["components"]


def test_resolve_build_state_skips_missing_ids(seeded_db):
    draft = state.new_build_draft("Free")
    draft["components"] = {"CPU": 999999}  # no such component
    build_state = state.resolve_build_state(draft)
    assert build_state == {}


def test_resolve_build_state_returns_real_components(seeded_db):
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]
    draft = state.new_build_draft("Free")
    draft["components"] = {"CPU": cpu.id}

    build_state = state.resolve_build_state(draft)
    assert build_state["CPU"].id == cpu.id


def test_build_total_cost():
    fake_cpu = Component(id=1, category="CPU", name="A", brand="T", price_usd=100.0, specs_json="{}")
    fake_gpu = Component(id=2, category="GPU", name="B", brand="T", price_usd=250.0, specs_json="{}")
    assert state.build_total_cost({"CPU": fake_cpu, "GPU": fake_gpu}) == 350.0


def test_resolve_build_state_handles_empty_draft():
    assert state.resolve_build_state(None) == {}
    assert state.resolve_build_state(state.new_build_draft()) == {}


# ---------------------------------------------------------------------------
# End-to-end smoke tests via AppTest
# ---------------------------------------------------------------------------
def test_app_boots_and_shows_landing_when_logged_out(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()

    assert not at.exception
    assert at.title[0].value == "Uncapped"
    assert at.session_state["page"] == "landing"
    assert at.session_state["auth_user"] is None


def test_gated_nav_buttons_disabled_when_logged_out(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()

    assert at.get_by_key("nav_create_build").disabled is True
    assert at.get_by_key("nav_my_builds").disabled is True
    assert at.get_by_key("nav_community").disabled is True


def test_register_flow_authenticates_user(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "alice", "alice@example.com", "Alice Doe")

    assert not at.exception
    assert at.session_state["auth_user"]["username"] == "alice"
    assert at.session_state["auth_user"]["full_name"] == "Alice Doe"
    assert "password" not in at.session_state["auth_user"]


def test_duplicate_username_shown_live_before_submit(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "bob", "bob@example.com", "Bob Row")

    # log out, then start a second registration reusing "bob" as the username
    at.get_by_key("logout_button").click().run()
    at.get_by_key("open_register").click().run()
    at.get_by_key("register_username").input("bob").run()

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "already taken" in markdown_text


def test_login_flow_by_email(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "carol", "carol@example.com", "Carol Row")
    at.get_by_key("logout_button").click().run()

    at.get_by_key("open_login").click().run()
    at.get_by_key("login_identifier").input("carol@example.com")
    at.get_by_key("login_password").input("Passw0rd!")
    at.get_by_key("login_submit").click()
    at.run()

    assert not at.exception
    assert at.session_state["auth_user"]["username"] == "carol"


def test_logout_returns_to_landing(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "dave", "dave@example.com", "Dave Row")
    at.get_by_key("sidebar_nav_my_builds").click().run()
    assert at.session_state["page"] == "my_builds"

    at.get_by_key("logout_button").click().run()
    assert not at.exception
    assert at.session_state["page"] == "landing"
    assert at.session_state["auth_user"] is None


def test_free_mode_selection_renders_without_exception(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "erin", "erin@example.com", "Erin Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    assert not at.exception
    assert at.session_state["create_mode"] == "Free"
    assert any(b.key and b.key.startswith("select_CPU_") for b in at.button)


def test_budget_mode_generates_full_compatible_build(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "frank", "frank@example.com", "Frank Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()

    assert not at.exception
    from engine.solvers import CATEGORY_ORDER

    components = at.session_state["build_draft"]["components"]
    assert set(components.keys()) == set(CATEGORY_ORDER)


def test_workload_mode_generates_baseline_build(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "grace", "grace@example.com", "Grace Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_workload").click().run()
    at.get_by_key("generate_workload_build").click().run()

    assert not at.exception
    assert at.session_state["build_draft"]["components"]


def test_analysis_falls_back_to_heuristic_without_api_key(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "heidi", "heidi@example.com", "Heidi Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("run_analysis").click().run()

    assert not at.exception
    assert at.session_state["build_draft_analysis"]["source"] == "heuristic"


def test_save_build_lands_on_my_builds(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "ivan", "ivan@example.com", "Ivan Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Ivan's Rig")
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception
    assert at.session_state["page"] == "my_builds"


def test_publish_and_view_in_community_thread(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "judy", "judy@example.com", "Judy Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Judy's Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    assert not at.exception

    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    assert view_buttons

    at.get_by_key(view_buttons[0]).click().run()
    assert not at.exception  # thread view must eager-load post.user without a DetachedInstanceError

    at.get_by_key("new_comment_input").input("Nice build!")
    at.get_by_key("post_comment").click()
    at.run()
    assert not at.exception


def test_fork_from_community_loads_build_studio(seeded_db):
    from engine.solvers import CATEGORY_ORDER

    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "kevin", "kevin@example.com", "Kevin Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Kevin's Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()
    at.get_by_key("fork_build").click()
    at.run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    components = at.session_state["build_draft"]["components"]
    assert set(components.keys()) == set(CATEGORY_ORDER)  # every category carried over, not just some


def test_my_builds_grouped_view_clone_loads_full_build(seeded_db):
    from engine.solvers import CATEGORY_ORDER

    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "laura", "laura@example.com", "Laura Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_workload").click().run()
    at.get_by_key("generate_workload_build").click().run()
    at.get_by_key("build_name_input").input("Laura's Rig")
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception  # default "Group by Workload Profile" view
    clone_buttons = [b.key for b in at.button if b.key and b.key.startswith("Clone_")]
    assert clone_buttons

    at.get_by_key(clone_buttons[0]).click()
    at.run()
    assert not at.exception
    assert at.session_state["page"] == "create_build"
    components = at.session_state["build_draft"]["components"]
    assert set(components.keys()) == set(CATEGORY_ORDER)


def test_my_builds_global_sort_orders_without_exception(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "mallory", "mallory@example.com", "Mallory Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Mallory's Rig")
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("my_builds_view_mode").set_value("Global Sort")
    at.run()
    assert not at.exception

    for sort_label in ("Cost (High to Low)", "Cost (Low to High)", "Date"):
        at.get_by_key("my_builds_sort").set_value(sort_label)
        at.run()
        assert not at.exception  # no DetachedInstanceError under any sort order

    assert any(b.key and b.key.startswith("Clone_") for b in at.button)


def test_analysis_invalidated_after_component_swap(seeded_db):
    """Regression test: swapping a part used to leave the synergy/bottleneck
    card showing stale numbers for a build that no longer matched what was on
    screen (ui/state.py::set_component didn't clear build_draft_analysis)."""
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "nathan", "nathan@example.com", "Nathan Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("run_analysis").click().run()

    assert at.session_state["build_draft_analysis"] is not None

    current_cpu_id = at.session_state["build_draft"]["components"]["CPU"]
    other_cpu_buttons = [
        b.key for b in at.button
        if b.key and b.key.startswith("select_CPU_") and str(current_cpu_id) not in b.key
    ]
    assert other_cpu_buttons
    at.get_by_key(other_cpu_buttons[0]).click().run()

    assert not at.exception
    assert at.session_state["build_draft_analysis"] is None


def test_comment_box_clears_and_resubmission_is_a_no_op(seeded_db):
    """Regression test: the comment text_area used to retain its posted text,
    so clicking "Post comment" again without retyping silently created a
    duplicate comment."""
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "olivia", "olivia@example.com", "Olivia Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Olivia's Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()

    at.get_by_key("new_comment_input").input("Great build!")
    at.get_by_key("post_comment").click()
    at.run()

    from db.repositories import community_repo

    post_id = at.session_state["selected_post_id"]
    assert len(community_repo.get_comments(post_id)) == 1
    assert at.get_by_key("new_comment_input").value == ""  # cleared, not left with stale text

    # clicking Post again with nothing retyped must be a no-op, not a duplicate
    at.get_by_key("post_comment").click()
    at.run()
    assert not at.exception
    assert len(community_repo.get_comments(post_id)) == 1


def test_admin_login_works(demo_seeded_db):
    """Task 2 requirement: the seeded admin account (admin / admin123) can log
    in via the standard dual-identifier form, just like any other user."""
    at = AppTest.from_file(str(APP_PATH))
    at.run()

    at.get_by_key("open_login").click().run()
    at.get_by_key("login_identifier").input("admin")
    at.get_by_key("login_password").input("admin123")
    at.get_by_key("login_submit").click()
    at.run()

    assert not at.exception
    assert at.session_state["auth_user"] is not None
    assert at.session_state["auth_user"]["username"] == "admin"
    assert at.session_state["auth_user"]["email"] == "admin@gmail.com"


def test_admin_login_works_by_email(demo_seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()

    at.get_by_key("open_login").click().run()
    at.get_by_key("login_identifier").input("admin@gmail.com")
    at.get_by_key("login_password").input("admin123")
    at.get_by_key("login_submit").click()
    at.run()

    assert not at.exception
    assert at.session_state["auth_user"]["username"] == "admin"


def test_build_deletion_requires_confirmation_then_removes_build(seeded_db):
    """Task 2 requirement: Delete asks for confirmation before it fires, and
    the build disappears from the list immediately once confirmed."""
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "petra", "petra@example.com", "Petra Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Disposable Rig")
    at.get_by_key("save_build").click()
    at.run()

    from db.repositories import builds_repo

    user_id = at.session_state["auth_user"]["id"]
    assert [b.name for b in builds_repo.get_builds_for_user(user_id)] == ["Disposable Rig"]

    delete_buttons = [b.key for b in at.button if b.key and b.key.startswith("Delete_")]
    assert delete_buttons
    at.get_by_key(delete_buttons[0]).click().run()

    # first click only arms the confirmation — build must still exist
    assert not at.exception
    assert len(builds_repo.get_builds_for_user(user_id)) == 1
    yes_buttons = [b.key for b in at.button if b.key and "confirm_Delete_" in b.key and b.key.endswith("_yes")]
    assert yes_buttons

    at.get_by_key(yes_buttons[0]).click().run()

    assert not at.exception
    assert builds_repo.get_builds_for_user(user_id) == []


def test_build_deletion_cancel_keeps_the_build(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "quinn", "quinn@example.com", "Quinn Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Keep Me")
    at.get_by_key("save_build").click()
    at.run()

    from db.repositories import builds_repo

    user_id = at.session_state["auth_user"]["id"]
    delete_buttons = [b.key for b in at.button if b.key and b.key.startswith("Delete_")]
    at.get_by_key(delete_buttons[0]).click().run()

    cancel_buttons = [b.key for b in at.button if b.key and "confirm_Delete_" in b.key and b.key.endswith("_cancel")]
    assert cancel_buttons
    at.get_by_key(cancel_buttons[0]).click().run()

    assert not at.exception
    assert [b.name for b in builds_repo.get_builds_for_user(user_id)] == ["Keep Me"]


def test_build_deletion_cascades_to_community_post(seeded_db):
    """Deleting a published build must also remove its community post (FK
    cascade on build_id) rather than leaving an orphaned post behind."""
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "randall", "randall@example.com", "Randall Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Published Then Deleted")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    from db.repositories import community_repo

    assert len(community_repo.get_feed()) == 1

    delete_buttons = [b.key for b in at.button if b.key and b.key.startswith("Delete_")]
    at.get_by_key(delete_buttons[0]).click().run()
    yes_buttons = [b.key for b in at.button if b.key and "confirm_Delete_" in b.key and b.key.endswith("_yes")]
    at.get_by_key(yes_buttons[0]).click().run()

    assert not at.exception
    assert community_repo.get_feed() == []


def test_build_studio_mode_cards_and_slot_grid_render(seeded_db):
    """Studio redesign smoke test: mode-selector cards and the 8-slot grid
    (with Selected/Empty status badges, popover pickers) render without
    exception, and every core category's picker is reachable."""
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "sam", "sam@example.com", "Sam Row")
    at.get_by_key("nav_create_build").click().run()

    assert not at.exception
    assert at.get_by_key("mode_budget") is not None
    assert at.get_by_key("mode_workload") is not None
    assert at.get_by_key("mode_free") is not None

    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    assert not at.exception

    from engine.solvers import CATEGORY_ORDER

    # every category was filled by the solver, so its "Remove" control (only
    # rendered once a component is selected) and at least one "Select"
    # button for a *different* candidate must both be reachable inside the
    # popover picker AppTest already traverses.
    for category in CATEGORY_ORDER:
        assert at.get_by_key(f"remove_{category}") is not None
        assert any(b.key and b.key.startswith(f"select_{category}_") for b in at.button)

    assert at.session_state["build_draft_analysis"] is None  # nothing analyzed yet, badges show "—"
