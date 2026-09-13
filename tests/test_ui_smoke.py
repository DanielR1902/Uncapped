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
    assert at.session_state["build_draft"]["components"]


def test_my_builds_grouped_and_global_views(seeded_db):
    at = AppTest.from_file(str(APP_PATH))
    at.run()
    _register(at, "laura", "laura@example.com", "Laura Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("generate_budget_build").click().run()
    at.get_by_key("build_name_input").input("Laura's Rig")
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception  # default "Group by Workload Profile" view

    at.get_by_key("my_builds_view_mode").set_value("Global Sort")
    at.run()
    assert not at.exception

    assert any(b.key and b.key.startswith("Clone_") for b in at.button)
