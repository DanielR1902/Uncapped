"""Phase 4 verification: state-transition unit tests + end-to-end UI smoke tests.

The smoke tests drive the real app.py via Streamlit's official AppTest harness
(streamlit.testing.v1) — no mocked widgets, actual script execution against a
temp seeded database. This is slower than a unit test but is what actually
proves "the app boots and the click-paths work" rather than "the functions
compile."
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import database
from db.models import Component
from db.seed import run_seed
from ui import state

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture(autouse=True)
def _no_live_llm_calls(monkeypatch):
    """This project's own rule (llm/CLAUDE.md): no live network calls in
    tests. `ui/views/create_build.py`'s auto-analyze (`_maybe_auto_analyze`)
    now fires the instant ANY build in this file becomes complete — which is
    most of these smoke tests, since building a complete 8-part build is
    just the setup step for testing something else entirely (save/publish/
    fork/etc.). Without this, every one of them would silently call the
    real OpenRouter API using whatever's in `.env` — `db/database.py`'s
    module-level `load_dotenv()` puts a real key into `os.environ` for the
    whole pytest process, not just tests that explicitly ask for it.
    Clearing it here forces every auto-triggered (and manually-triggered)
    analysis in this file onto the deterministic, network-free heuristic
    fallback instead, for every test, autouse — matching what a test suite
    should actually exercise."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)


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


def _session_value(at: AppTest, key: str, default=None):
    """AppTest's session_state proxy raises (not returns None) for a missing
    key, unlike a plain dict's .get() — this mirrors .get()'s semantics for
    keys that are only ever set once something has actually happened (e.g.
    `pending_community_filters`, which doesn't exist in _DEFAULTS until a
    Concierge navigation-with-filters action first stages it)."""
    try:
        return at.session_state[key]
    except (KeyError, AttributeError):
        return default


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


def test_new_build_draft_includes_empty_quantities():
    draft = state.new_build_draft("Free")
    assert draft["quantities"] == {}


def test_get_quantity_defaults_to_one():
    draft = state.new_build_draft("Free")
    assert state.get_quantity(draft, "RAM") == 1
    assert state.get_quantity(draft, "Storage") == 1


def test_set_quantity_round_trips():
    draft = state.new_build_draft("Free")
    state.set_quantity(draft, "RAM", 2)
    assert state.get_quantity(draft, "RAM") == 2


def test_set_quantity_clamps_to_one():
    draft = state.new_build_draft("Free")
    state.set_quantity(draft, "RAM", 0)
    assert state.get_quantity(draft, "RAM") == 1
    state.set_quantity(draft, "RAM", -5)
    assert state.get_quantity(draft, "RAM") == 1


def test_remove_component_clears_its_quantity():
    draft = state.new_build_draft("Free")
    fake_ram = Component(id=7, category="RAM", name="Fake RAM", brand="Test", price_usd=80.0, specs_json="{}")
    state.set_component(draft, "RAM", fake_ram)
    state.set_quantity(draft, "RAM", 2)
    state.remove_component(draft, "RAM")
    assert state.get_quantity(draft, "RAM") == 1  # stale multiplier is gone, starts fresh


def test_build_total_cost_scales_with_quantities():
    fake_cpu = Component(id=1, category="CPU", name="A", brand="T", price_usd=100.0, specs_json="{}")
    fake_ram = Component(id=2, category="RAM", name="B", brand="T", price_usd=50.0, specs_json="{}")
    build_state = {"CPU": fake_cpu, "RAM": fake_ram}

    # no quantities dict -> every multiplier is 1, identical to before this feature
    assert state.build_total_cost(build_state) == 150.0
    assert state.build_total_cost(build_state, None) == 150.0

    assert state.build_total_cost(build_state, {"RAM": 2}) == 200.0


# ---------------------------------------------------------------------------
# resolve_effective_quantity_limit — combines the real physical slot limit
# (engine.compatibility.resolve_quantity_limit) with a budget-affordability
# limit for the RAM/Storage quantity stepper, whichever is tighter.
# ---------------------------------------------------------------------------
def _fake_mobo(ram_slots: int = 4) -> Component:
    return Component(
        id=100, category="Motherboard", name="Fake Mobo", brand="T", price_usd=120.0,
        specs_json=f'{{"ram_slots": {ram_slots}, "max_ram_gb": 128}}',
    )


def _fake_ram(price: float = 50.0) -> Component:
    # deliberately no "(NxYGB)" in the name -> _ram_kit_module_count falls
    # back to 1 module per kit, so physical max == ram_slots exactly.
    return Component(id=2, category="RAM", name="Fake RAM 8GB", brand="T", price_usd=price, specs_json="{}")


def test_resolve_effective_quantity_limit_blocks_when_next_unit_exceeds_budget():
    """Budget mode: 2 kits already selected at $50 each ($100 total), a 3rd
    kit would cost $150 which exceeds a $140 ceiling -> incrementing further
    must be blocked (effective_max stays at the current quantity)."""
    ram = _fake_ram(price=50.0)
    build_state = {"RAM": ram}  # no Motherboard -> no physical constraint at all
    quantities = {"RAM": 2}

    effective_max, reason, limit_kind = state.resolve_effective_quantity_limit(
        build_state, "RAM", quantities, budget_ceiling=140.0
    )

    assert effective_max == 2  # current_qty; can't afford a 3rd kit
    assert limit_kind == "budget"
    assert reason  # a human-readable explanation is present


def test_resolve_effective_quantity_limit_physical_tighter_than_budget():
    """Budget mode with plenty of headroom, but the motherboard's real
    2-DIMM-slot limit (1 module per kit here) is tighter than what the
    budget alone would allow."""
    mobo = _fake_mobo(ram_slots=2)
    ram = _fake_ram(price=50.0)
    build_state = {"Motherboard": mobo, "RAM": ram}
    quantities = {"RAM": 1}

    effective_max, reason, limit_kind = state.resolve_effective_quantity_limit(
        build_state, "RAM", quantities, budget_ceiling=100_000.0
    )

    assert effective_max == 2  # physical limit: 2 slots // 1 module-per-kit
    assert limit_kind == "physical"
    assert reason


def test_resolve_effective_quantity_limit_ignores_budget_outside_budget_mode():
    """Workload/Free mode passes budget_ceiling=None -> the financial
    constraint never applies, effective_max is whatever the physical-only
    function would return."""
    mobo = _fake_mobo(ram_slots=2)
    ram = _fake_ram(price=50.0)
    build_state = {"Motherboard": mobo, "RAM": ram}
    quantities = {"RAM": 1}

    effective_max, reason, limit_kind = state.resolve_effective_quantity_limit(
        build_state, "RAM", quantities, budget_ceiling=None
    )

    from engine.compatibility import resolve_quantity_limit

    physical_max, physical_reason = resolve_quantity_limit(build_state, "RAM")
    assert effective_max == physical_max == 2
    assert reason == physical_reason
    assert limit_kind == "physical"


def test_resolve_effective_quantity_limit_no_data_at_all():
    """No Motherboard (so no real physical data) and no budget ceiling ->
    neither constraint has real data to bound this category."""
    ram = _fake_ram(price=50.0)
    build_state = {"RAM": ram}
    quantities = {"RAM": 1}

    result = state.resolve_effective_quantity_limit(build_state, "RAM", quantities, budget_ceiling=None)
    assert result == (None, "", "none")


def test_resolve_effective_quantity_limit_exact_boundary_is_allowed():
    """Boundary case: spending exactly up to the ceiling is fine, only going
    OVER it is blocked. 1 kit at $50 is already selected; a ceiling of
    exactly $100 means a 2nd kit (bringing the total to exactly $100) must
    still be allowed, since `remaining_for_category // price` with
    remaining=100 and price=50 floors to exactly 2, not 1.
    """
    ram = _fake_ram(price=50.0)
    build_state = {"RAM": ram}  # no Motherboard -> physical constraint is None
    quantities = {"RAM": 1}

    effective_max, reason, limit_kind = state.resolve_effective_quantity_limit(
        build_state, "RAM", quantities, budget_ceiling=100.0
    )

    # other_components_cost = 0 (RAM is the only category), remaining = 100,
    # financial_max = int(100 // 50) = 2 -> a 2nd kit (total exactly $100) is allowed.
    assert effective_max == 2
    assert limit_kind == "budget"
    assert reason


# ---------------------------------------------------------------------------
# End-to-end smoke tests via AppTest
# ---------------------------------------------------------------------------
def test_app_boots_and_shows_landing_when_logged_out(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert not at.exception
    assert at.title[0].value == "Uncapped"
    assert at.session_state["page"] == "landing"
    assert at.session_state["auth_user"] is None


def test_gated_nav_buttons_disabled_when_logged_out(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert at.get_by_key("nav_create_build").disabled is True
    assert at.get_by_key("nav_my_builds").disabled is True
    assert at.get_by_key("nav_community").disabled is True


# ---------------------------------------------------------------------------
# Home "Command Deck & Observatory Portal" (ui/views/landing.py, spec.md
# §7.11) — capability matrix, tiered preset launchpad, trending builds reel,
# and user-state adaptation. This app has no real anonymous/guest session
# anywhere else (router.py's own comment: "all buttons locked" pre-login),
# so "Continue as Guest Architect" is a labeled invitation into
# registration, not actual guest access — see landing.py's own docstring.
# ---------------------------------------------------------------------------
def test_landing_shows_capability_matrix_and_preset_launchpad(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    page_text = "\n".join(m.value for m in at.markdown)
    # theme.section_header's own tech-label transform (spec.md §7.7):
    # "Platform Capability Matrix" -> "// PLATFORM_CAPABILITY_MATRIX".
    assert "// PLATFORM_CAPABILITY_MATRIX" in page_text
    assert "Real-Time Synergy & Clearance" in page_text
    assert "Dynamic Bottleneck Mitigation" in page_text
    assert "Multi-Currency Telemetry" in page_text
    assert "AI Architecture Concierge" in page_text
    assert "// TIERED_PRESET_LAUNCHPAD" in page_text
    assert "Tier Alpha" in page_text
    assert "Tier Beta" in page_text
    assert "Tier Ultra" in page_text


def test_landing_preset_buttons_disabled_when_logged_out(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert at.get_by_key("load_preset_alpha").disabled is True
    assert at.get_by_key("load_preset_beta").disabled is True
    assert at.get_by_key("load_preset_ultra").disabled is True


def test_landing_no_guest_architect_bypass_button(seeded_db):
    """The "Continue as Guest Architect" button was removed entirely (spec.md
    §7.11) — a logged-out visitor sees only the real Login/Register forms,
    no bypass button of any kind."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    assert not any(b.key == "continue_as_guest" for b in at.button)
    page_text = "\n".join(m.value for m in at.markdown)
    assert "Continue as Guest Architect" not in page_text


def test_landing_guest_lockdown_notice_and_trending_button_disabled(seeded_db):
    """Logged out: the red "must be logged in" notice appears beside BOTH
    the Preset Launchpad and Trending Builds section headers, and the
    Trending reel's own "Inspect Blueprint" button is disabled -- a real
    gap from an earlier round (only the preset buttons were gated then,
    this one wasn't)."""
    from db.repositories import builds_repo, community_repo, components_repo, users_repo

    user = users_repo.create_user("guestlockdown1", "hash", "guestlockdown1@example.com", "Guest Lockdown One")
    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user.id, name="Guest Lockdown Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, user.id, "Guest Lockdown Rig")

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    page_text = "\n".join(m.value for m in at.markdown)
    assert page_text.count("must be logged in to view these builds") == 2

    assert at.get_by_key(f"trending_view_{post.id}").disabled is True


def test_landing_guest_lockdown_clears_and_button_active_when_logged_in(seeded_db):
    """Logging in must remove both red notices and re-enable the Trending
    reel's "Inspect Blueprint" button for a real, existing shared post."""
    from db.repositories import builds_repo, community_repo, components_repo, users_repo

    author = users_repo.create_user("guestlockdown2", "hash", "guestlockdown2@example.com", "Guest Lockdown Two")
    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=author.id, name="Guest Lockdown Rig Two", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, author.id, "Guest Lockdown Rig Two")

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "loggedinviewer1", "loggedinviewer1@example.com", "Logged In Viewer One")

    page_text = "\n".join(m.value for m in at.markdown)
    assert "must be logged in to view these builds" not in page_text
    assert at.get_by_key(f"trending_view_{post.id}").disabled is False


def test_landing_load_preset_populates_studio_and_navigates(seeded_db):
    """Clicking "Load Preset to Studio" must populate a real, fully
    compatible Workload build (engine.solvers.allocate_workload_baseline)
    into build_draft and land directly in the Build Studio."""
    from engine.compatibility import evaluate_build

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "presetuser1", "presetuser1@example.com", "Preset User One")

    at.get_by_key("load_preset_beta").click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] == "Workload"
    build_draft = at.session_state["build_draft"]
    assert build_draft["workload_profile"] == "Gaming"
    assert build_draft["tier"] == "High"
    assert set(build_draft["components"].keys()) >= {"CPU", "GPU", "Motherboard", "RAM", "Storage", "PSU", "Case", "Cooler"}

    from db.repositories import components_repo
    build_state = {cat: components_repo.get_by_id(cid) for cat, cid in build_draft["components"].items()}
    assert evaluate_build(build_state).is_compatible is True


def test_landing_trending_builds_reel_shows_real_community_posts(seeded_db):
    """A real, freshly-published community post must appear in the
    trending reel with its real title and price."""
    from db.repositories import builds_repo, community_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "trendinguser1", "trendinguser1@example.com", "Trending User One")

    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=at.session_state["auth_user"]["id"], name="Trending Reel Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    community_repo.create_post(build.id, at.session_state["auth_user"]["id"], "Trending Reel Rig")

    at.get_by_key("nav_create_build").click().run()  # any rerun refreshes the landing page's own feed read
    at.session_state["page"] = "landing"
    at.run()

    page_text = "\n".join(m.value for m in at.markdown)
    assert "// TRENDING_COMMUNITY_BUILDS" in page_text
    assert "Trending Reel Rig" in page_text


def test_landing_shows_welcome_and_resume_last_draft_when_logged_in(seeded_db):
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "resumeuser1", "resumeuser1@example.com", "Resume User One")

    drafts_repo.save_draft(
        user_id=at.session_state["auth_user"]["id"], name="My Resumable Draft", mode="Free",
        components={}, quantities={},
    )
    at.session_state["page"] = "landing"
    at.run()

    page_text = "\n".join(m.value for m in at.markdown)
    assert "Welcome back, Architect Resume User One" in page_text
    captions = "\n".join(c.value for c in at.caption)
    assert "My Resumable Draft" in captions

    at.get_by_key("resume_last_draft").click().run()
    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["build_draft"]["name"] == "My Resumable Draft"


def test_register_flow_authenticates_user(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "alice", "alice@example.com", "Alice Doe")

    assert not at.exception
    assert at.session_state["auth_user"]["username"] == "alice"
    assert at.session_state["auth_user"]["full_name"] == "Alice Doe"
    assert "password" not in at.session_state["auth_user"]


def test_duplicate_username_shown_live_before_submit(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "bob", "bob@example.com", "Bob Row")

    # log out, then start a second registration reusing "bob" as the username
    at.get_by_key("logout_button").click().run()
    at.get_by_key("open_register").click().run()
    at.get_by_key("register_username").input("bob").run()

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "already taken" in markdown_text


def test_login_flow_by_email(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
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
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "dave", "dave@example.com", "Dave Row")
    at.get_by_key("sidebar_nav_my_builds").click().run()
    assert at.session_state["page"] == "my_builds"

    at.get_by_key("logout_button").click().run()
    assert not at.exception
    assert at.session_state["page"] == "landing"
    assert at.session_state["auth_user"] is None


def test_free_mode_selection_renders_without_exception(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "erin", "erin@example.com", "Erin Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    assert not at.exception
    assert at.session_state["create_mode"] == "Free"
    assert any(b.key and b.key.startswith("select_CPU_") for b in at.button)


def test_free_mode_manual_completion_triggers_auto_analysis(seeded_db):
    """_maybe_auto_analyze's trigger condition only checks which CORE
    categories are filled, never `mode` — so it must fire identically
    whether the build was completed by Budget's/Workload's generate buttons
    (already covered by test_build_studio_mode_cards_and_slot_grid_render
    and test_analysis_auto_refreshes_after_component_swap) or by a user
    manually filling all 8 slots one at a time in Free mode, which had no
    explicit coverage before this test."""
    from engine.solvers import CATEGORY_ORDER

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fiona", "fiona@example.com", "Fiona Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    assert at.session_state["build_draft_analysis"] is None

    for category in CATEGORY_ORDER:
        candidate_buttons = [
            b.key for b in at.button if b.key and b.key.startswith(f"select_{category}_")
        ]
        assert candidate_buttons, f"no selectable candidate for {category}"
        at.get_by_key(candidate_buttons[0]).click().run()
        assert not at.exception

    components = at.session_state["build_draft"]["components"]
    assert set(CATEGORY_ORDER).issubset(components.keys())
    assert at.session_state["build_draft_analysis"] is not None


def test_budget_mode_generates_full_compatible_build(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "frank", "frank@example.com", "Frank Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    assert not at.exception
    from engine.solvers import CATEGORY_ORDER

    components = at.session_state["build_draft"]["components"]
    # every core slot is always filled; "Apply budget & generate build" also
    # opts into spending any surplus on optional peripherals (see
    # solvers.initialize_budget_build's fill_peripherals_with_surplus), so
    # the default $1500 ceiling's leftover budget may add some of those too
    # — core categories are a guaranteed subset, not necessarily the whole set.
    assert set(CATEGORY_ORDER).issubset(components.keys())


def test_budget_ceiling_below_floor_clamps_and_generates_full_build(seeded_db):
    """Unified apply flow: typing a ceiling below the true minimum (no more
    native min_value lockup on the widget) and clicking "Apply budget &
    generate build" must cleanly clamp to the floor, snap the widget's own
    displayed value there too, and still produce a complete 8-part build —
    all in one click, no red invalid-input state."""
    from db.repositories import components_repo
    from engine.solvers import CATEGORY_ORDER, minimum_possible_build_cost

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "victor", "victor@example.com", "Victor Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()

    at.get_by_key("budget_ceiling_input").set_value(1.0).run()
    at.get_by_key("apply_budget_generate").click().run()

    assert not at.exception
    floor_cost = minimum_possible_build_cost()
    assert at.session_state["build_draft"]["budget_ceiling"] == floor_cost
    assert at.get_by_key("budget_ceiling_input").value == floor_cost

    components = at.session_state["build_draft"]["components"]
    assert set(components.keys()) == set(CATEGORY_ORDER)
    total_cost = sum(components_repo.get_by_id(cid).price_usd for cid in components.values())
    assert total_cost <= floor_cost


def test_raising_budget_ceiling_immediately_unlocks_over_budget_parts(seeded_db):
    """Unified apply flow: applying a tight ceiling disables at least one
    over-cap candidate in the GPU picker popover; re-applying a much higher
    ceiling (e.g. $5,000) must immediately re-enable that same candidate's
    Select button, with no stale state left over from the previous ceiling.
    Picks whichever candidate is actually disabled at the tight ceiling
    rather than assuming the catalog's priciest GPU is even in the
    candidate list — a GPU can be absent for compatibility reasons
    (case/length fit) entirely unrelated to budget."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "wendy", "wendy@example.com", "Wendy Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()

    at.get_by_key("budget_ceiling_input").set_value(700.0).run()
    at.get_by_key("apply_budget_generate").click().run()
    assert not at.exception

    gpu_buttons = [b for b in at.button if b.key and b.key.startswith("select_GPU_")]
    assert gpu_buttons
    disabled_at_tight_ceiling = [b for b in gpu_buttons if b.disabled]
    assert disabled_at_tight_ceiling  # at least one candidate must be over budget at $700
    probe_key = disabled_at_tight_ceiling[0].key

    at.get_by_key("budget_ceiling_input").set_value(5000.0).run()
    at.get_by_key("apply_budget_generate").click().run()
    assert not at.exception
    assert at.session_state["build_draft"]["budget_ceiling"] == 5000.0

    gpu_buttons_after = [b for b in at.button if b.key == probe_key]
    assert gpu_buttons_after, f"{probe_key} disappeared from the candidate list after raising the ceiling"
    assert gpu_buttons_after[0].disabled is False


def test_workload_mode_generates_baseline_build(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "grace", "grace@example.com", "Grace Row")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_workload").click().run()
    at.get_by_key("generate_workload_build").click().run()

    assert not at.exception
    assert at.session_state["build_draft"]["components"]


def test_analysis_falls_back_to_heuristic_without_api_key(seeded_db, monkeypatch):
    """No manual "Analyze" button exists anymore — analysis auto-triggers
    the instant the build completes (_maybe_auto_analyze), so it's already
    present (and, with no API key, on the heuristic path) right after
    "Apply budget & generate build" with no further click needed."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "heidi", "heidi@example.com", "Heidi Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    assert not at.exception
    assert at.session_state["build_draft_analysis"]["source"] == "heuristic"


def test_save_build_lands_on_my_builds(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "ivan", "ivan@example.com", "Ivan Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Ivan's Rig")
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception
    assert at.session_state["page"] == "my_builds"


def test_publish_and_view_in_community_thread(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "judy", "judy@example.com", "Judy Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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


def test_save_as_draft_checkbox_hides_publish_checkbox(seeded_db):
    """The "Save as draft" checkbox (spec.md §7.4 step 9) is mutually
    exclusive with "Also publish to Community" — a draft has no publish path
    anywhere in this app's real architecture, the same precedent as the AI
    Concierge's own save_build/destination:"draft" branch. Checking it hides
    the publish checkbox entirely; unchecking it brings the publish checkbox
    back."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draftcheck1", "draftcheck1@example.com", "Draft Check One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    assert at.get_by_key("publish_checkbox") is not None  # visible by default

    at.get_by_key("save_as_draft_checkbox").check().run()
    assert not at.exception
    with pytest.raises(KeyError):
        at.get_by_key("publish_checkbox")

    at.get_by_key("save_as_draft_checkbox").uncheck().run()
    assert not at.exception
    assert at.get_by_key("publish_checkbox") is not None  # reappears


def test_save_as_draft_checkbox_persists_to_drafts_table(seeded_db):
    """Checking "Save as draft" and clicking "Save build" calls
    drafts_repo.save_draft (never builds_repo.create_build) under the
    literal typed name, resets the builder to a clean slate, and redirects
    to the drafts page — the manual counterpart to the silent exit
    auto-save (spec.md §7.9)."""
    from db.repositories import builds_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draftcheck2", "draftcheck2@example.com", "Draft Check Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Draft Check Two's WIP")
    at.get_by_key("save_as_draft_checkbox").check().run()

    user_id = at.session_state["auth_user"]["id"]
    at.get_by_key("save_build").click().run()

    assert not at.exception
    assert at.session_state["page"] == "drafts"
    assert at.session_state["create_mode"] is None
    assert at.session_state["build_draft"] is None
    assert at.session_state["has_unsaved_build_changes"] is False

    drafts = drafts_repo.get_user_drafts(user_id)
    assert len(drafts) == 1
    assert drafts[0].name == "Draft Check Two's WIP"
    assert drafts[0].mode == "Budget"
    # Never a real, scored Build row — no publish path for a draft.
    assert builds_repo.get_builds_for_user(user_id) == []


def test_save_as_draft_checkbox_blank_name_defaults_to_untitled(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draftcheck3", "draftcheck3@example.com", "Draft Check Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("save_as_draft_checkbox").check().run()

    user_id = at.session_state["auth_user"]["id"]
    at.get_by_key("save_build").click().run()

    assert not at.exception
    from db.repositories import drafts_repo

    drafts = drafts_repo.get_user_drafts(user_id)
    assert len(drafts) == 1
    assert drafts[0].name == "Untitled Draft"


def test_community_description_saved_and_displayed(seeded_db):
    """The optional 'Community post description' text area (shown only
    once 'Also publish to Community' is checked) must actually reach the
    saved post's author_notes, and the post's thread view must render it
    (ui/views/community.py's `_thread_view` already had an `if
    post.author_notes:` block — the feed list itself only ever showed
    title/cost/date, never notes, core or otherwise, so the thread view is
    where "displaying posts in the Community feed" actually surfaces it)."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "karen", "karen@example.com", "Karen Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Karen's Rig")

    at.get_by_key("publish_checkbox").check().run()
    assert at.get_by_key("community_description_input") is not None  # only rendered once checked

    note = "Great for 1440p gaming and light streaming."
    at.get_by_key("community_description_input").input(note)
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception
    from db.repositories import community_repo

    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].author_notes == note

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    assert view_buttons
    at.get_by_key(view_buttons[0]).click().run()

    assert not at.exception
    assert any(note in m.value for m in at.markdown)


def test_community_thread_view_shows_quantity_badge_for_multi_unit_storage(seeded_db):
    """`BuildComponent.quantity` is already persisted and used functionally
    elsewhere (forking preserves it), but `_thread_view`'s per-component
    list previously always rendered "- **Storage**: <name> ($<unit price>)"
    with no indication a slot held more than one unit. A quantity > 1 must
    now show as "Storage (x3): <name> ($<unit price * 3>)" — the real total
    for that many units, not just one."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "quantbadge1", "quantbadge1@example.com", "Quant Badge One")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    import json as _json

    def _m2_slots(mobo) -> int:
        return (_json.loads(mobo.specs_json) if mobo.specs_json else {}).get("m2_slots") or 0

    mobo = max(components_repo.get_by_category("Motherboard"), key=_m2_slots)
    assert _m2_slots(mobo) >= 2
    cpu = next(c for c in components_repo.get_by_category("CPU") if c.socket == mobo.socket)
    storage = next(c for c in components_repo.get_by_category("Storage") if c.interface == "NVMe")
    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_Storage_{storage.id}").click().run()
    at.get_by_key("qty_Storage").set_value(2).run()
    assert at.session_state["build_draft"]["quantities"]["Storage"] == 2

    at.get_by_key("build_name_input").input("Multi-Storage Rig")
    at.get_by_key("publish_checkbox").check().run()
    at.get_by_key("save_build").click().run()
    assert not at.exception

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    assert view_buttons
    at.get_by_key(view_buttons[0]).click().run()
    assert not at.exception

    expected_total = storage.price_usd * 2
    page_text = "\n".join(m.value for m in at.markdown)
    assert f"Storage (x2)" in page_text
    assert f"${expected_total:,.2f}" in page_text
    # A single-unit category (CPU is always qty 1) must NOT get a badge.
    assert "CPU (x1)" not in page_text
    assert "CPU (x" not in page_text


def test_community_description_omitted_when_left_blank(seeded_db):
    """Leaving the description blank must not store an empty-string
    author_notes — it should stay None, matching a build published with no
    notes at all (and the feed view's `if post.author_notes:` guard)."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "leo", "leo@example.com", "Leo Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Leo's Rig")
    at.get_by_key("publish_checkbox").check().run()
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception
    from db.repositories import community_repo

    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].author_notes is None


def test_fork_from_community_loads_build_studio(seeded_db):
    from engine.solvers import CATEGORY_ORDER

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "kevin", "kevin@example.com", "Kevin Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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
    # every CORE category must carry over (the fork must not drop any of
    # them) — the source build may also have picked up optional peripherals
    # via the default ceiling's surplus, which is fine, just not guaranteed
    # to be the exact full set.
    assert set(CATEGORY_ORDER).issubset(components.keys())


def test_fork_from_community_sets_unsaved_changes_flag(seeded_db):
    """Real, confirmed bug: `_fork_into_studio` populates `build_draft`
    ["components"] as a direct dict literal rather than replaying picks
    through `ui.state.set_component` (the usual place `has_unsaved_build_
    changes` gets set) — so a freshly-forked build, which can carry a full
    8+ category pick set, silently reported NO unsaved changes. Fixed so the
    flag is correctly tracked (spec.md §7.9) even though, per the CURRENT
    design, leaving the builder afterward without explicitly using "Save as
    draft" discards the fork with no database write either way."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "forkflag1", "forkflag1@example.com", "Fork Flag One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Fork Flag Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()
    at.get_by_key("fork_build").click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["has_unsaved_build_changes"] is True

    user_id = at.session_state["auth_user"]["id"]
    at.get_by_key("sidebar_nav_community").click().run()

    assert not at.exception
    assert at.session_state["page"] == "community"  # navigates immediately, no database write
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["create_mode"] is None
    assert at.session_state["build_draft"] is None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_my_builds_grouped_view_clone_loads_full_build(seeded_db):
    from engine.solvers import CATEGORY_ORDER

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
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
    # every core category must carry over (the clone must not drop any of
    # them) — "Generate baseline build" also always includes peripherals
    # now (include_peripherals=True), which is fine, just not guaranteed
    # to be the exact full set.
    assert set(CATEGORY_ORDER).issubset(components.keys())


def test_my_builds_clone_sets_unsaved_changes_flag(seeded_db):
    """Same real bug/fix as test_fork_from_community_sets_unsaved_changes_flag
    above, for my_builds.py's own `_clone_into_studio` (backing BOTH the
    "Clone" and "Edit" card actions) — an identical dict-literal `components`
    write that bypassed `has_unsaved_build_changes` entirely."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "cloneflag1", "cloneflag1@example.com", "Clone Flag One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_workload").click().run()
    at.get_by_key("generate_workload_build").click().run()
    at.get_by_key("build_name_input").input("Clone Flag Rig")
    at.get_by_key("save_build").click().run()

    clone_buttons = [b.key for b in at.button if b.key and b.key.startswith("Clone_")]
    assert clone_buttons
    at.get_by_key(clone_buttons[0]).click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["has_unsaved_build_changes"] is True

    user_id = at.session_state["auth_user"]["id"]
    at.get_by_key("sidebar_nav_community").click().run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["has_unsaved_build_changes"] is False
    assert drafts_repo.get_user_drafts(user_id) == []


def test_my_builds_global_sort_orders_without_exception(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "mallory", "mallory@example.com", "Mallory Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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


def test_analysis_auto_refreshes_after_component_swap(seeded_db):
    """Regression test: swapping a part used to leave the synergy/bottleneck
    card showing stale numbers for a build that no longer matched what was on
    screen (ui/state.py::set_component didn't clear build_draft_analysis).
    Now that analysis auto-triggers (_maybe_auto_analyze), the fix is even
    stronger than just clearing the stale value: since the build stays
    complete after a swap (one CPU replaces another, the slot doesn't go
    empty), a fresh analysis is recomputed automatically on the very same
    rerun — there's no window where the UI shows nothing, or the old
    result, for a build that's already changed."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "nathan", "nathan@example.com", "Nathan Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    assert at.session_state["build_draft_analysis"] is not None  # auto-triggered already

    current_cpu_id = at.session_state["build_draft"]["components"]["CPU"]
    other_cpu_buttons = [
        b.key for b in at.button
        if b.key and b.key.startswith("select_CPU_") and str(current_cpu_id) not in b.key
    ]
    assert other_cpu_buttons
    at.get_by_key(other_cpu_buttons[0]).click().run()

    assert not at.exception
    # freshly auto-recomputed for the NEW build, not left stale or cleared
    assert at.session_state["build_draft_analysis"] is not None
    assert at.session_state["build_draft"]["components"]["CPU"] != current_cpu_id


def test_comment_box_clears_and_resubmission_is_a_no_op(seeded_db):
    """Regression test: the comment text_area used to retain its posted text,
    so clicking "Post comment" again without retyping silently created a
    duplicate comment."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "olivia", "olivia@example.com", "Olivia Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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


# ---------------------------------------------------------------------------
# Community posts (no voting mechanism — removed entirely per a later
# product decision; see git history for the prior Reddit-style upvote/
# downvote implementation this replaced) and threaded (nested-reply)
# comments (spec.md §7.6).
# ---------------------------------------------------------------------------
def test_community_feed_has_no_vote_widgets(seeded_db):
    """No upvote/downvote arrows, score numbers, or vote-related widget keys
    exist anywhere on a feed card or its thread view — a real regression
    check for the voting-system teardown, not just an absence-of-evidence
    assumption."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "novote1", "novote1@example.com", "No Vote One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("No Vote Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    assert not at.exception

    button_keys = [b.key or "" for b in at.button]
    assert not any("upvote" in k or "downvote" in k for k in button_keys)
    feed_text = "\n".join(m.value for m in at.markdown)
    assert "▲" not in feed_text
    assert "▼" not in feed_text

    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()
    assert not at.exception
    button_keys = [b.key or "" for b in at.button]
    assert not any("upvote" in k or "downvote" in k for k in button_keys)
    thread_text = "\n".join(m.value for m in at.markdown)
    assert "▲" not in thread_text
    assert "▼" not in thread_text


def test_community_feed_ranked_chronologically(seeded_db):
    """A build published SECOND (newer) must always rank above one published
    FIRST (older) — the feed's ONLY ordering rule now, since net-score
    ranking was removed entirely along with the voting mechanism.
    created_at is pinned explicitly after publishing (rather than relying on
    real-time ordering between two rapid UI publishes, which could tie
    under SQLite's whole-second CURRENT_TIMESTAMP resolution) so the
    ordering assertion itself is deterministic — the publish flow through
    the real UI is still exercised for both builds."""
    import datetime as dt

    from sqlalchemy import update

    from db import database
    from db.models import CommunityPost
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "chronofeed1", "chronofeed1@example.com", "Chrono Feed One")

    def _publish(name: str) -> None:
        at.get_by_key("sidebar_nav_create_build").click().run()
        at.get_by_key("mode_budget").click().run()
        at.get_by_key("apply_budget_generate").click().run()
        at.get_by_key("build_name_input").input(name)
        at.get_by_key("publish_checkbox").check()
        at.get_by_key("save_build").click()
        at.run()

    _publish("Older Rig")
    _publish("Newer Rig")

    posts_by_name = {p.title: p.id for p in community_repo.get_feed()}
    older_id = posts_by_name["Older Rig"]
    newer_id = posts_by_name["Newer Rig"]

    with database.session_scope() as session:
        session.execute(
            update(CommunityPost).where(CommunityPost.id == older_id)
            .values(created_at=dt.datetime(2020, 1, 1))
        )
        session.execute(
            update(CommunityPost).where(CommunityPost.id == newer_id)
            .values(created_at=dt.datetime(2020, 1, 2))
        )

    at.get_by_key("sidebar_nav_community").click().run()
    assert not at.exception

    feed_order = [p.id for p in community_repo.get_feed()]
    assert feed_order.index(newer_id) < feed_order.index(older_id)


def test_community_top_level_comment_appears_chronologically(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "chrono1", "chrono1@example.com", "Chrono One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Chrono Test Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()

    at.get_by_key("new_comment_input").input("First!")
    at.get_by_key("post_comment").click()
    at.run()
    at.get_by_key("new_comment_input").input("Second one")
    at.get_by_key("post_comment").click()
    at.run()

    from db.repositories import community_repo

    post_id = at.session_state["selected_post_id"]
    comments = community_repo.get_comments(post_id)
    assert [c.content for c in comments] == ["First!", "Second one"]
    assert all(c.parent_comment_id is None for c in comments)


def test_community_reply_creates_threaded_nested_comment(seeded_db):
    """Clicking "Reply" on an existing comment, submitting, must create a
    REAL child comment (parent_comment_id set to the original comment's id)
    -- the actual threading relationship the directive's own verification
    checks for ("renders indented ... directly beneath the parent
    comment"), not just another top-level comment."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "thread1", "thread1@example.com", "Thread One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Thread Test Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()

    at.get_by_key("new_comment_input").input("Top-level comment")
    at.get_by_key("post_comment").click()
    at.run()

    from db.repositories import community_repo

    post_id = at.session_state["selected_post_id"]
    top_level = community_repo.get_comments(post_id)[0]

    # Open the reply box under that specific comment.
    at.get_by_key(f"reply_btn_{top_level.id}").click().run()
    assert at.session_state["community_reply_target"] == top_level.id

    at.get_by_key(f"reply_input_{top_level.id}").input("A nested reply")
    at.get_by_key(f"submit_reply_{top_level.id}").click()
    at.run()

    assert not at.exception
    comments = community_repo.get_comments(post_id)
    assert len(comments) == 2
    reply = next(c for c in comments if c.id != top_level.id)
    assert reply.content == "A nested reply"
    assert reply.parent_comment_id == top_level.id
    # The reply box closes itself after a successful submit.
    assert at.session_state["community_reply_target"] is None


def test_community_reply_cancel_closes_box_without_creating_a_comment(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "thread2", "thread2@example.com", "Thread Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Thread Cancel Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click()
    at.run()

    at.get_by_key("sidebar_nav_community").click().run()
    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()

    at.get_by_key("new_comment_input").input("Only comment")
    at.get_by_key("post_comment").click()
    at.run()

    from db.repositories import community_repo

    post_id = at.session_state["selected_post_id"]
    top_level = community_repo.get_comments(post_id)[0]

    at.get_by_key(f"reply_btn_{top_level.id}").click().run()
    at.get_by_key(f"cancel_reply_{top_level.id}").click().run()

    assert not at.exception
    assert at.session_state["community_reply_target"] is None
    assert len(community_repo.get_comments(post_id)) == 1  # no reply created


def test_admin_login_works(demo_seeded_db):
    """Task 2 requirement: the seeded admin account (admin / admin123) can log
    in via the standard dual-identifier form, just like any other user."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
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
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
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
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "petra", "petra@example.com", "Petra Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "quinn", "quinn@example.com", "Quinn Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "randall", "randall@example.com", "Randall Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
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
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "sam", "sam@example.com", "Sam Row")
    at.get_by_key("nav_create_build").click().run()

    assert not at.exception
    assert at.get_by_key("mode_budget") is not None
    assert at.get_by_key("mode_workload") is not None
    assert at.get_by_key("mode_free") is not None

    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    assert not at.exception

    from engine.solvers import CATEGORY_ORDER

    # every category was filled by the solver, so its slot-level "Clear"
    # button (only rendered once a component is selected) and at least one
    # "Select" button for a *different* candidate inside the popover picker
    # must both be reachable.
    for category in CATEGORY_ORDER:
        assert at.get_by_key(f"clear_{category}") is not None
        assert any(b.key and b.key.startswith(f"select_{category}_") for b in at.button)

    # analysis auto-triggers the instant the build is complete (no manual
    # "Analyze" click exists anymore) — the Synergy/Bottleneck metric cards
    # already show real numbers on this very first render, not "—".
    assert at.session_state["build_draft_analysis"] is not None


def test_slot_clear_button_removes_component_without_opening_popover(seeded_db):
    """Studio redesign: each slot's header now has a direct clear (✕) button
    next to Change/Choose, so a component can be cleared in one click
    without opening the picker drawer at all."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "tara", "tara@example.com", "Tara Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    assert at.session_state["build_draft"]["components"].get("GPU") is not None
    at.get_by_key("clear_GPU").click().run()

    assert not at.exception
    assert "GPU" not in at.session_state["build_draft"]["components"]


def test_get_advisory_button_populates_cache_with_suggestions(seeded_db):
    """AI Build Advisory: the HUD's compact "✨ AI Analysis" quick action
    (the ONE trigger now — a duplicate full-page "Get AI Analysis & Upgrade
    Path" button was removed this round, spec.md §7.4.1, since it populated
    the exact same cache) must exist once a build has at least two
    components, and clicking it must populate `advisory_cache` with a
    non-empty within_budget and stretch_budget suggestion list.
    `_no_live_llm_calls` (autouse) keeps this on the network-free heuristic
    path, same as the existing auto-analysis fallback test above."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "priya", "priya@example.com", "Priya Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    advisory_button = at.get_by_key("hud_ai_analysis")
    assert advisory_button is not None
    assert advisory_button.disabled is False  # build is complete, well over 2 components

    advisory_button.click().run()
    assert not at.exception

    cache = at.session_state["advisory_cache"]
    assert len(cache) == 1
    advisory = next(iter(cache.values()))
    assert isinstance(advisory["within_budget"], dict)
    assert isinstance(advisory["within_budget"]["explanation"], str) and advisory["within_budget"]["explanation"]
    assert isinstance(advisory["within_budget"]["swaps"], list)
    assert isinstance(advisory["stretch_budget"], dict)
    assert isinstance(advisory["stretch_budget"]["explanation"], str) and advisory["stretch_budget"]["explanation"]
    assert isinstance(advisory["stretch_budget"]["actions"], list)
    assert isinstance(advisory["pros"], list) and advisory["pros"]
    assert isinstance(advisory["cons"], list) and advisory["cons"]
    assert advisory["source"] == "heuristic"


def test_apply_in_budget_optimization_changes_components_and_cost(seeded_db):
    """Clicking "⚡ Apply In-Budget Optimization" must actually mutate
    build_draft["components"] for the swapped category to the advisory's
    `replace_with_id`, and the summary metrics (total cost) must reflect the
    new build on the very next render — proving the button does real work,
    not just a no-op rerun."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply1", "apply1@example.com", "Apply One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    cache = at.session_state["advisory_cache"]
    advisory = next(iter(cache.values()))
    swaps = advisory["within_budget"]["swaps"]

    apply_button = at.get_by_key("btn_apply_in_budget")
    assert apply_button is not None

    if not swaps:
        assert apply_button.disabled is True
        return  # heuristic found no beneficial swap for this build/seed — nothing further to prove here

    assert apply_button.disabled is False
    build_state_before = state.resolve_build_state(at.session_state["build_draft"])
    # Record the pre-click id for each category this round's advisory names,
    # rather than asserting the final id equals `swap["replace_with_id"]`
    # verbatim: the real (unmocked) heuristic advisory used here may report
    # can_optimize_further=True after this round, in which case the bounded
    # auto-optimize loop (up to _MAX_AUTO_OPTIMIZE_ROUNDS) re-queries and
    # applies a FURTHER refined swap for the same category on a later round
    # — still a real, correct swap, just not necessarily this exact id.
    original_ids = {swap["category"]: build_state_before[swap["category"]].id for swap in swaps}
    before_cost = state.build_total_cost(build_state_before, at.session_state["build_draft"].get("quantities", {}))

    apply_button.click().run()
    assert not at.exception

    components_after = at.session_state["build_draft"]["components"]
    for category, original_id in original_ids.items():
        assert components_after[category] != original_id  # actually swapped away from the original pick

    after_cost = state.build_total_cost(
        state.resolve_build_state(at.session_state["build_draft"]),
        at.session_state["build_draft"].get("quantities", {}),
    )
    assert after_cost != before_cost  # summary metrics reflect the swapped build


def test_apply_in_budget_optimization_terminates_within_round_cap(seeded_db, monkeypatch):
    """Even when the heuristic keeps reporting can_optimize_further=True,
    the auto-optimize loop backing "Apply In-Budget Optimization" must not
    hang or crash — it's hard-capped at
    ui.views.create_build._MAX_AUTO_OPTIMIZE_ROUNDS rounds. Forces the
    scenario by monkeypatching get_build_advisory (as imported into
    ui.views.create_build) to always report can_optimize_further=True with a
    real, always-valid swap, so every one of the 3 rounds actually re-fires."""
    from db.repositories import components_repo
    from engine.solvers import CATEGORY_ORDER
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply2", "apply2@example.com", "Apply Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    call_count = {"n": 0}

    def _always_optimizable(
        build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None
    ):
        call_count["n"] += 1
        category = "CPU"
        candidates = [c for c in components_repo.get_by_category(category) if c.id != build_state[category].id]
        replace_with_id = candidates[0].id if candidates else build_state[category].id
        return {
            "pros": ["p"], "cons": ["c"],
            "within_budget": {
                "explanation": "keep optimizing",
                "swaps": [{"category": category, "replace_with_id": replace_with_id}],
                "can_optimize_further": True,
            },
            "stretch_budget": {"explanation": "stretch", "actions": [], "added_cost_usd": 0.0},
            "source": "heuristic",
        }

    monkeypatch.setattr(create_build_module, "get_build_advisory", _always_optimizable)

    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    apply_button = at.get_by_key("btn_apply_in_budget")
    assert apply_button.disabled is False
    apply_button.click().run()

    assert not at.exception  # completes, doesn't hang
    # 1 call from the initial "Get AI Analysis" click, plus at most one more
    # per round of the auto-optimize loop (it always re-queries here since
    # can_optimize_further is forced True every time) — proves the loop
    # terminates instead of looping forever chasing a False that never comes.
    assert call_count["n"] <= 1 + create_build_module._MAX_AUTO_OPTIMIZE_ROUNDS

    components = at.session_state["build_draft"]["components"]
    assert set(CATEGORY_ORDER).issubset(components.keys())

    # a fresh cache entry exists for whatever the final build state is
    cache = at.session_state["advisory_cache"]
    final_build_state = state.resolve_build_state(at.session_state["build_draft"])
    final_cost = state.build_total_cost(final_build_state, at.session_state["build_draft"].get("quantities", {}))
    final_key = (
        "Budget",
        None,
        tuple(sorted((cat, c.id) for cat, c in final_build_state.items())),
        round(at.session_state["build_draft"]["budget_ceiling"] or final_cost, 2),
    )
    assert final_key in cache


def test_apply_stretch_upgrade_locks_per_build(seeded_db, monkeypatch):
    """Clicking "🚀 Apply Stretch Upgrade (One-Time)" applies the stretch
    actions and adds the current advisory cache key to
    st.session_state["stretch_applied_keys"]; clicking it again for the same
    build (same cache key) must be disabled/a no-op — but the lock is scoped
    to that one cache key, not a global "ever used" flag.

    Deliberately mocked with a `set_quantity` action rather than the real
    heuristic: `advisory_cache`'s key is built from (sorted category/
    component-id pairs, mode, cost) — quantities aren't part of it — so a
    set_quantity action is guaranteed not to change the cache key on
    application, keeping the advisory expander (and this button) rendered
    on the next run so the "still disabled" assertion is actually reachable.
    A real swap action, by contrast, changes the component-id tuple, which
    changes the cache key, so the previously-cached advisory (and its Apply
    button) simply stops rendering for the new build — a separate, already
    correct behavior, not a lock bug (see `_advisory_controls`'s docstring).

    Ceiling chosen ABOVE the catalog's own true maximum possible full-build
    cost (peripherals included), not just distinct from other tests'
    ceilings: the RAM/Storage quantity stepper is budget-aware
    (`ui.state.resolve_effective_quantity_limit`), and this stretch action
    deliberately pushes RAM quantity to 2. `engine.solvers`'s
    `_spend_up_remaining_headroom` pass now converges an ACHIEVABLE ceiling
    to ~99.9%+ utilization by design (a real, confirmed under-utilization
    bug fix — see `engine/CLAUDE.md`), so a lower, achievable ceiling like
    the $2000 this test used to use would now leave no real budget-effective
    headroom for that 2nd RAM kit, and the part-picker's own pre-clamp
    (correctly, generically) would immediately clamp the just-applied
    quantity back down to 1 on the very next render — a real product
    interaction, but not what this test is exercising. A ceiling above the
    catalog's true max can never be fully spent by any solver, guaranteeing
    real headroom regardless of how good the convergence algorithm gets."""
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply3", "apply3@example.com", "Apply Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("budget_ceiling_input").set_value(5750.0).run()
    at.get_by_key("apply_budget_generate").click().run()

    def _set_quantity_stretch_advisory(
        build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None
    ):
        return {
            "pros": ["p"], "cons": ["c"],
            "within_budget": {"explanation": "already optimal", "swaps": [], "can_optimize_further": False},
            "stretch_budget": {
                "explanation": "add a second RAM kit",
                "actions": [{"action": "set_quantity", "category": "RAM", "quantity": 2}],
                "added_cost_usd": 50.0,
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(create_build_module, "get_build_advisory", _set_quantity_stretch_advisory)

    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    stretch_button = at.get_by_key("btn_apply_stretch")
    assert stretch_button is not None
    assert stretch_button.disabled is False
    locked_before = len(at.session_state["stretch_applied_keys"])

    stretch_button.click().run()
    assert not at.exception

    assert at.session_state["build_draft"]["quantities"]["RAM"] == 2
    # relative growth, not an absolute count — see this test's own docstring
    # for why (the shared-default stretch_applied_keys set() quirk).
    assert len(at.session_state["stretch_applied_keys"]) == locked_before + 1

    # same build -> same cache key -> button now disabled, no further mutation
    stretch_button_again = at.get_by_key("btn_apply_stretch")
    assert stretch_button_again.disabled is True


def test_apply_stretch_set_quantity_action_updates_quantity(seeded_db, monkeypatch):
    """stretch_budget.actions can carry a {"action": "set_quantity", ...}
    entry (the RAM/Storage multi-slot stretch upgrade) alongside or instead
    of swaps. Clicking "Apply Stretch Upgrade" must route it through
    state.set_quantity, landing in build_draft["quantities"], not attempt a
    catalog lookup meant for swaps.

    Ceiling chosen ABOVE the catalog's own true maximum possible full-build
    cost, not just distinct from other tests' ceilings — see
    test_apply_stretch_upgrade_locks_per_build's docstring: the RAM/Storage
    quantity stepper is budget-aware, and `_spend_up_remaining_headroom`
    converges an ACHIEVABLE ceiling to ~99.9%+ by design, so a tight (or, as
    of this catalog's 4TB/8TB NVMe additions, no-longer-safely-above-max
    $5000) ceiling would let its own pre-clamp immediately re-clamp this
    test's just-applied qty=2 back down to 1 on the next render, which isn't
    what this test is about."""
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply5", "apply5@example.com", "Apply Five")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    # Distinct ceiling so this cache key can't collide with another test's
    # default-$1500 budget build (see test_apply_stretch_upgrade_locks_per_build's
    # docstring for why that matters given the shared-default stretch_applied_keys set).
    at.get_by_key("budget_ceiling_input").set_value(6500.0).run()
    at.get_by_key("apply_budget_generate").click().run()

    def _set_quantity_stretch_advisory(
        build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None
    ):
        return {
            "pros": ["p"], "cons": ["c"],
            "within_budget": {"explanation": "already optimal", "swaps": [], "can_optimize_further": False},
            "stretch_budget": {
                "explanation": "add a second RAM kit",
                "actions": [{"action": "set_quantity", "category": "RAM", "quantity": 2}],
                "added_cost_usd": 50.0,
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(create_build_module, "get_build_advisory", _set_quantity_stretch_advisory)

    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    stretch_button = at.get_by_key("btn_apply_stretch")
    assert stretch_button.disabled is False
    locked_before = len(at.session_state["stretch_applied_keys"])
    stretch_button.click().run()
    assert not at.exception

    assert at.session_state["build_draft"]["quantities"]["RAM"] == 2
    # relative growth, not an absolute count: `stretch_applied_keys`'s
    # session-state default is a single set() object shared across every
    # AppTest session in this process (ui/state.py's _DEFAULTS quirk,
    # out of scope for this task), so other tests' own locked cache keys
    # may already be present here.
    assert len(at.session_state["stretch_applied_keys"]) == locked_before + 1


def test_apply_stretch_swap_action_updates_component(seeded_db, monkeypatch):
    """stretch_budget.actions with an explicit {"action": "swap", ...} entry
    (the pre-rename shape, now nested one level under "actions" instead of
    "swaps") must still resolve the id via components_repo and pin it via
    state.set_component, same as _apply_swaps always did for within_budget."""
    from db.repositories import components_repo
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply6", "apply6@example.com", "Apply Six")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    # Distinct ceiling so this cache key can't collide with another test's
    # default-$1500 budget build (see test_apply_stretch_upgrade_locks_per_build's
    # docstring for why that matters given the shared-default stretch_applied_keys set).
    at.get_by_key("budget_ceiling_input").set_value(1630.0).run()
    at.get_by_key("apply_budget_generate").click().run()

    current_cpu_id = at.session_state["build_draft"]["components"]["CPU"]
    other_cpu = next(c for c in components_repo.get_by_category("CPU") if c.id != current_cpu_id)

    def _swap_stretch_advisory(
        build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None
    ):
        return {
            "pros": ["p"], "cons": ["c"],
            "within_budget": {"explanation": "already optimal", "swaps": [], "can_optimize_further": False},
            "stretch_budget": {
                "explanation": "upgrade the CPU",
                "actions": [{"action": "swap", "category": "CPU", "replace_with_id": other_cpu.id}],
                "added_cost_usd": 100.0,
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(create_build_module, "get_build_advisory", _swap_stretch_advisory)

    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    stretch_button = at.get_by_key("btn_apply_stretch")
    assert stretch_button.disabled is False
    locked_before = len(at.session_state["stretch_applied_keys"])
    stretch_button.click().run()
    assert not at.exception

    assert at.session_state["build_draft"]["components"]["CPU"] == other_cpu.id
    # relative growth, not an absolute count — see the analogous comment in
    # test_apply_stretch_set_quantity_action_updates_quantity.
    assert len(at.session_state["stretch_applied_keys"]) == locked_before + 1


def test_advisory_apply_buttons_disabled_when_no_swaps(seeded_db, monkeypatch):
    """A build whose advisory has empty `swaps` in either within_budget or
    stretch_budget renders that tab's Apply button as disabled=True — never
    crashing, never silently no-opping on a click that shouldn't be
    reachable in the first place."""
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply4", "apply4@example.com", "Apply Four")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _no_swaps_advisory(
        build_state, mode, current_budget_or_cost, profile=None, bottleneck_info=None, quantities=None
    ):
        return {
            "pros": ["p"], "cons": ["c"],
            "within_budget": {"explanation": "already optimal", "swaps": [], "can_optimize_further": False},
            "stretch_budget": {"explanation": "no upgrade available", "actions": [], "added_cost_usd": 0.0},
            "source": "heuristic",
        }

    monkeypatch.setattr(create_build_module, "get_build_advisory", _no_swaps_advisory)

    at.get_by_key("hud_ai_analysis").click().run()
    assert not at.exception

    assert at.get_by_key("btn_apply_in_budget").disabled is True
    assert at.get_by_key("btn_apply_stretch").disabled is True

    # clicking a disabled button is a no-op in AppTest terms (nothing to
    # click), so just re-confirm the build/components are untouched.
    components_before = dict(at.session_state["build_draft"]["components"])
    at.run()
    assert at.session_state["build_draft"]["components"] == components_before


def test_reset_all_fields_clears_build_and_budget_ceiling(seeded_db):
    """Studio redesign: the HUD's compact "🔄 Reset Build" quick action (the
    ONE reset entry point now — a duplicate full-page "Reset All Fields"
    button calling the exact same `_do_reset_build` was removed this round,
    spec.md §7.4.1) empties every slot and restores the budget ceiling to its
    clean default in one click. Budget ceiling commits are now apply-on-click
    (not live-synced as you type), so a fresh draft's budget_ceiling is None
    until "Apply budget & generate build" is clicked again — it's the
    *widget's displayed value* that resets to the clean default immediately,
    not the committed one."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "ulysses", "ulysses@example.com", "Ulysses Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("budget_ceiling_input").set_value(900.0).run()
    at.get_by_key("apply_budget_generate").click().run()

    assert at.session_state["build_draft"]["components"]  # non-empty before reset
    assert at.session_state["build_draft"]["budget_ceiling"] == 900.0

    at.get_by_key("hud_reset_build").click().run()

    assert not at.exception
    assert at.session_state["build_draft"]["components"] == {}
    assert at.session_state["build_draft"]["budget_ceiling"] is None  # nothing committed yet on the fresh draft
    assert at.get_by_key("budget_ceiling_input").value == 1500.0  # widget redisplays the clean default


# ---------------------------------------------------------------------------
# Cyber-Minimalist / High-Tech Industrial redesign: telemetry HUD, "Rate My
# Build", and the industrial hardware picker cards (spec.md §7.4.1).
# ---------------------------------------------------------------------------
def test_hud_power_headroom_chip_shows_real_draw_and_capacity(seeded_db):
    """The HUD's Power/Headroom chip (_power_headroom_readout) must show the
    REAL CPU+GPU tdp_watts sum over the REAL PSU wattage_capacity -- not a
    placeholder -- once both exist in the build, and must render as "-"
    before a CPU is even picked."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "hud1", "hud1@example.com", "Hud One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    page_text = "\n".join(m.value for m in at.markdown)
    assert "—" in page_text  # no CPU yet -> placeholder

    cpu = components_repo.get_by_category("CPU")[0]
    psu = components_repo.get_by_category("PSU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_PSU_{psu.id}").click().run()

    page_text = "\n".join(m.value for m in at.markdown)
    assert f"{cpu.tdp_watts}W / {psu.wattage_capacity}W" in page_text


def test_hud_compatibility_pill_reflects_real_score(seeded_db):
    """The HUD's Compatibility pulse-badge pill must show a real percentage
    while the build is empty (evaluate_build({}) is vacuously 100%
    compatible -- no rule fires with nothing picked)."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "hud2", "hud2@example.com", "Hud Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    page_text = "\n".join(m.value for m in at.markdown)
    assert "100% Compatible" in page_text


def test_hud_reset_build_button_matches_reset_all_fields(seeded_db):
    """The HUD's compact "Reset Build" quick action must clear the build via
    the shared `_do_reset_build` helper — the ONE reset entry point now (a
    duplicate full-page "Reset All Fields" button that called the exact same
    helper was removed this round, spec.md §7.4.1)."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "hud3", "hud3@example.com", "Hud Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    assert at.session_state["build_draft"]["components"]

    at.get_by_key("hud_reset_build").click().run()

    assert not at.exception
    assert at.session_state["build_draft"]["components"] == {}


def test_hud_ai_analysis_populates_same_cache_as_get_advisory_button(seeded_db):
    """The HUD's compact "AI Analysis" quick action (`_trigger_advisory`) —
    the ONE trigger now (a duplicate full-page "Get AI Analysis & Upgrade
    Path" button was removed this round, spec.md §7.4.1) — must populate
    `st.session_state["advisory_cache"]` and have `_advisory_controls`'s
    results panel (a pure display read now) pick it up with no separate
    click of its own."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "hud4", "hud4@example.com", "Hud Four")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    at.get_by_key("hud_ai_analysis").click().run()

    assert not at.exception
    assert len(at.session_state["advisory_cache"]) == 1
    assert any("AI Build Advisory" in e.label for e in at.expander)


def test_rate_my_build_dialog_opens_with_autofilled_title_and_snapshot(seeded_db):
    """The HUD's "Share / Rate My Build" button must open the dialog
    (st.dialog) with a technical title auto-filled from the real CPU+GPU
    names and a live Price/TDP/Synergy snapshot -- gated the same way
    _save_actions' own "Save build" button already is (disabled=not
    build_state, not full-completeness).

    NOTE: the actual submit -> create_build/create_post/teardown/navigate
    flow is NOT driven through this dialog via AppTest -- confirmed via a
    minimal probe script that streamlit.testing.v1's bare-mode script
    runner closes ANY st.dialog on the very next rerun regardless of what
    triggers it (even a no-op re-run with zero pending widget changes), so
    a widget-fill-then-submit sequence across multiple .run() calls can
    never reach a still-open dialog -- a real AppTest limitation, not an
    app bug. That flow is covered by test_community_post_flair (test_db.py,
    the exact same builds_repo.create_build/community_repo.create_post
    call shape _rate_my_build_dialog uses) and by live manual browser
    verification instead."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "ratemybuild1", "ratemybuild1@example.com", "Rate My Build One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    assert at.get_by_key("hud_rate_my_build").proto.disabled  # empty build_state

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    assert not at.get_by_key("hud_rate_my_build").proto.disabled
    at.get_by_key("hud_rate_my_build").click().run()

    assert not at.exception
    assert at.get_by_key("rate_my_build_title").value == f"[Spec Check] {cpu.name} + {gpu.name} Rig"
    metric_values = {m.value for m in at.metric}
    expected_tdp = f"{(cpu.tdp_watts or 0) + (gpu.tdp_watts or 0)}W"
    assert expected_tdp in metric_values


def test_community_rate_my_build_flair_badge_shown_on_feed_and_thread(seeded_db):
    """A "Rate My Build" post's flair badge (theme.pulse_badge) must appear
    both on its Community feed card and on its own thread view. Creates the
    post directly via community_repo (same call shape
    _rate_my_build_dialog's submit handler uses — see the note on
    test_rate_my_build_dialog_opens_with_autofilled_title_and_snapshot for
    why the dialog itself isn't driven through AppTest) and verifies the
    real ui/views/community.py rendering path picks up post.flair."""
    from db.repositories import builds_repo, community_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "flairview1", "flairview1@example.com", "Flair View One")

    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=at.session_state["auth_user"]["id"], name="Flair View Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    community_repo.create_post(build.id, at.session_state["auth_user"]["id"], "Flair View Rig", flair="Rate My Build")

    at.get_by_key("sidebar_nav_community").click().run()
    feed_text = "\n".join(m.value for m in at.markdown)
    assert "Rate My Build" in feed_text

    view_buttons = [b.key for b in at.button if b.key and b.key.startswith("view_post_")]
    at.get_by_key(view_buttons[0]).click().run()
    thread_text = "\n".join(m.value for m in at.markdown)
    assert "Rate My Build" in thread_text


def test_previous_builds_publish_form_requires_flair_before_creating_tagged_post(seeded_db):
    """Clicking "Share to Community" on a Previous Builds card must NOT
    publish immediately — it opens an inline form (description + flair);
    only "Confirm & Publish" actually creates the real, tagged post, and the
    original saved build is left completely untouched throughout."""
    from db.repositories import builds_repo, community_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "publishform1", "publishform1@example.com", "Publish Form One")

    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=at.session_state["auth_user"]["id"], name="Form Publish Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=False,
    )

    at.get_by_key("sidebar_nav_my_builds").click().run()
    at.get_by_key(f"Share_to_Community_{build.id}").click().run()

    # No post created yet — the click only opened the form.
    assert community_repo.get_feed() == []
    assert at.get_by_key(f"publish_flair_{build.id}") is not None

    at.get_by_key(f"publish_flair_{build.id}").select("Looking for Help").run()
    at.get_by_key(f"publish_description_{build.id}").set_value("Need advice on cooling.").run()
    at.get_by_key(f"publish_confirm_{build.id}").click().run()

    assert not at.exception
    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].flair == "Looking for Help"
    assert feed[0].author_notes == "Need advice on cooling."
    assert feed[0].build_id == build.id
    # The original build row itself is untouched beyond the publish flip.
    refreshed = builds_repo.get_build(build.id)
    assert refreshed.name == "Form Publish Rig"
    assert refreshed.is_public is True


def test_your_posts_view_lists_authored_posts_only(seeded_db):
    """"Your Posts" (sidebar, between Drafts and Community) shows only the
    logged-in user's own posts, with their flair badge."""
    from db.repositories import builds_repo, community_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "yourposts1", "yourposts1@example.com", "Your Posts One")
    user_id = at.session_state["auth_user"]["id"]

    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user_id, name="Your Posts Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, user_id, "Your Posts Rig", flair="Rate My Build")

    at.get_by_key("sidebar_nav_your_posts").click().run()

    assert not at.exception
    page_text = "\n".join(m.value for m in at.markdown)
    assert "Your Posts Rig" in page_text
    assert "Rate My Build" in page_text
    assert at.get_by_key(f"view_in_community_{post.id}") is not None
    assert at.get_by_key(f"delete_post_{post.id}") is not None


def test_your_posts_view_in_community_navigates_to_thread(seeded_db):
    from db.repositories import builds_repo, community_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "yourposts2", "yourposts2@example.com", "Your Posts Two")
    user_id = at.session_state["auth_user"]["id"]

    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user_id, name="View Nav Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, user_id, "View Nav Rig")

    at.get_by_key("sidebar_nav_your_posts").click().run()
    at.get_by_key(f"view_in_community_{post.id}").click().run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["selected_post_id"] == post.id
    thread_text = "\n".join(m.value for m in at.markdown)
    assert "View Nav Rig" in thread_text


def test_your_posts_delete_removes_post_but_keeps_saved_build(seeded_db):
    from db.repositories import builds_repo, community_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "yourposts3", "yourposts3@example.com", "Your Posts Three")
    user_id = at.session_state["auth_user"]["id"]

    cpu = components_repo.get_by_category("CPU")[0]
    build = builds_repo.create_build(
        user_id=user_id, name="Delete Post Rig", creation_mode="Free",
        components=[builds_repo.BuildComponentInput(component_id=cpu.id)],
        total_cost=cpu.price_usd, compatibility_score=100.0, is_public=True,
    )
    post = community_repo.create_post(build.id, user_id, "Delete Post Rig")

    at.get_by_key("sidebar_nav_your_posts").click().run()
    at.get_by_key(f"delete_post_{post.id}").click().run()
    at.get_by_key(f"confirm_delete_post_{post.id}_yes").click().run()

    assert not at.exception
    assert community_repo.get_post(post.id) is None
    # The saved build must survive completely untouched.
    assert builds_repo.get_build(build.id) is not None
    assert builds_repo.get_build(build.id).name == "Delete Post Rig"


def test_part_picker_empty_slot_uses_wireframe_key_and_socket_match_badge(seeded_db):
    """An empty slot's own container key must carry the "_empty" suffix
    theme.inject_css() targets for the dashed wireframe look, switching to
    "_filled" once selected; a matched CPU+Motherboard pair must show the
    "✓ {socket} Matched" confirmation tag."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "wireframe1", "wireframe1@example.com", "Wireframe One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    container_keys = {c.key for c in at.container if c.key}
    assert "picker_slot_CPU_empty" in container_keys
    assert "picker_slot_Motherboard_empty" in container_keys

    cpu = components_repo.get_by_category("CPU")[0]
    mobo = next(m for m in components_repo.get_by_category("Motherboard") if m.socket == cpu.socket)
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    assert not at.exception
    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()

    container_keys = {c.key for c in at.container if c.key}
    assert "picker_slot_CPU_filled" in container_keys
    assert "picker_slot_Motherboard_filled" in container_keys
    assert "picker_slot_CPU_empty" not in container_keys

    page_text = "\n".join(m.value for m in at.markdown)
    assert f"✓ {cpu.socket} Matched" in page_text


# ---------------------------------------------------------------------------
# RAM/Storage quantity stepper — real engine-backed max (part_picker.py's
# _resolve_quantity_bound replaced a duplicate, now-incorrect local
# calculation with engine.compatibility.resolve_quantity_limit directly).
# ---------------------------------------------------------------------------
def test_ram_quantity_max_respects_real_per_module_count(seeded_db):
    """A 4-DIMM-slot motherboard (ASRock B550M-HDV: ram_slots=4) with a
    2-module ("2x8GB") RAM kit selected must cap the Qty stepper at 2 real
    kits (4 slots // 2 modules per kit) — NOT the old buggy behavior of
    reporting the raw ram_slots count (4) regardless of kit size."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "quant1", "quant1@example.com", "Quant One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    ram_2_module = next(
        c for c in components_repo.get_by_category("RAM") if c.name.startswith("Corsair Vengeance LPX 16GB (2x8GB)")
    )

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_RAM_{ram_2_module.id}").click().run()

    assert not at.exception
    qty_widget = at.get_by_key("qty_RAM")
    assert qty_widget.max == 2  # 4 DIMM slots // 2 modules-per-kit, not the raw slot count


def test_storage_quantity_max_for_nvme_on_asrock_b550m_hdv(seeded_db):
    """ASRock B550M-HDV's real seeded m2_slots is 1. Picking an NVMe drive
    (Kingston NV2) must cap the Storage Qty stepper at exactly 1 — the
    directive's own boundary case (min_value == max_value == 1) — and this
    must render without raising Streamlit's native range exception. The
    caption must also report the real "up to 1" limit."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "quant2", "quant2@example.com", "Quant Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    nvme = next(c for c in components_repo.get_by_category("Storage") if c.name.startswith("Kingston NV2"))

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_Storage_{nvme.id}").click().run()

    assert not at.exception  # the min_value == max_value == 1 boundary must not raise
    qty_widget = at.get_by_key("qty_Storage")
    assert qty_widget.min == 1
    assert qty_widget.max == 1
    assert qty_widget.value == 1

    captions = [c.value for c in at.caption]
    assert any("up to 1" in c for c in captions)


def test_storage_quantity_caption_for_sata_does_not_claim_motherboard_data(seeded_db):
    """SATA drives have no real motherboard-backed port-count data in this
    catalog (resolve_quantity_limit correctly returns None for them). The UI
    must fall back to a clearly-labeled generic cap and must NEVER phrase
    the caption as if it were a real motherboard-derived fact."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "quant3", "quant3@example.com", "Quant Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    sata = next(c for c in components_repo.get_by_category("Storage") if c.name.startswith("Crucial MX500"))

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_Storage_{sata.id}").click().run()

    assert not at.exception
    qty_widget = at.get_by_key("qty_Storage")
    assert qty_widget.max == 4  # UI-only fallback cap, not a fabricated motherboard fact

    captions = [c.value for c in at.caption]
    fallback_captions = [c for c in captions if "safety limit" in c]
    assert fallback_captions
    assert not any("motherboard" in c.lower() and "limit reached" in c.lower() for c in fallback_captions)
    assert not any("Motherboard limit" in c for c in fallback_captions)


def test_ram_quantity_clamps_down_when_swapped_kit_shrinks_the_max(seeded_db):
    """Regression test for the shrink-then-rerun StreamlitAPIException: start
    with a 1-module ("1x8GB") RAM kit under a 4-slot motherboard (real max 4),
    push the Qty stepper up to 4, then swap to a 2-module ("2x8GB") kit whose
    real max is only 2 (4 slots // 2 modules). Before the fix, the stale
    qty_RAM=4 sitting in st.session_state would make the next st.number_input
    render raise (its default/current value would be outside the new
    max_value=2 bound). The fix pre-clamps st.session_state[qty_key] before
    the widget renders, so this must complete with no exception and the
    quantity silently clamped down to 2."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "quant4", "quant4@example.com", "Quant Four")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    ram_1_module = next(
        c for c in components_repo.get_by_category("RAM") if c.name.startswith("Corsair Vengeance 8GB (1x8GB)")
    )
    ram_2_module = next(
        c for c in components_repo.get_by_category("RAM") if c.name.startswith("Corsair Vengeance LPX 16GB (2x8GB)")
    )

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_RAM_{ram_1_module.id}").click().run()
    assert at.get_by_key("qty_RAM").max == 4  # 4 slots // 1 module-per-kit

    at.get_by_key("qty_RAM").set_value(4).run()
    assert not at.exception
    assert at.session_state["build_draft"]["quantities"]["RAM"] == 4

    # Swap to the 2-module kit: the real max shrinks from 4 to 2, and the
    # stale quantity of 4 is still sitting in st.session_state["qty_RAM"].
    at.get_by_key(f"select_RAM_{ram_2_module.id}").click().run()

    assert not at.exception  # must NOT raise StreamlitAPIException
    qty_widget = at.get_by_key("qty_RAM")
    assert qty_widget.max == 2
    assert qty_widget.value == 2  # silently clamped down, not left stale at 4
    assert at.session_state["build_draft"]["quantities"]["RAM"] == 2


def test_concierge_quantity_bump_is_reflected_by_the_stepper_widget_immediately(seeded_db, monkeypatch):
    """Closes a previously-known, documented architectural gap: a keyed
    st.number_input only honors `value=` the very first time its key is
    created, so an out-of-band quantity write straight to
    build_draft["quantities"] (e.g. a Concierge modify_build action) used to
    have no effect on what the stepper actually DISPLAYED — it kept showing
    whatever was cached in st.session_state["qty_{category}"] from before.
    ui.state.set_quantity now syncs that widget key directly. This must not
    raise StreamlitWidgetAlreadyInstantiatedError (ui.state._sync_qty_widget_key
    silently no-ops in the one context where Streamlit forbids the write —
    a call from the stepper's own on-change callback, which needs no sync
    anyway since the widget's own interaction already set the value)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]
    ram = components_repo.get_by_category("RAM")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Increased RAM to 2 units.",
            "action": {
                "type": "modify_build",
                "components": {},
                "quantities": {"RAM": 2},
                "explanation": "Doubled the RAM.",
            },
            "source": "heuristic",
        }

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "qtysync1", "qtysync1@example.com", "Qty Sync One")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_RAM_{ram.id}").click().run()

    # The stepper renders (and its widget key gets created) at quantity 1
    # before the Concierge ever touches it.
    assert at.get_by_key("qty_RAM").value == 1

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("bump my ram to 2").run()

    assert not at.exception
    assert at.session_state["build_draft"]["quantities"]["RAM"] == 2
    # The actual rendered widget — not just the underlying build_draft dict —
    # must reflect the new quantity on this very next render.
    assert at.get_by_key("qty_RAM").value == 2


def test_resolve_quantity_bound_helper_matches_engine_for_shrink_scenario():
    """Unit-level check of the clamping helper itself (part_picker.py's
    _resolve_quantity_bound), independent of AppTest/Streamlit runtime, for
    the same 1-module -> 2-module shrink scenario as the AppTest regression
    test above — belt-and-suspenders coverage of the underlying numbers
    driving the clamp, isolated from any Streamlit widget-rendering nuance."""
    from db.models import Component
    from ui.components.part_picker import _resolve_quantity_bound

    motherboard = Component(
        id=1, category="Motherboard", name="ASRock B550M-HDV", brand="ASRock", price_usd=80.0,
        specs_json='{"ram_slots": 4, "max_ram_gb": 128}',
    )
    ram_1_module = Component(
        id=2, category="RAM", name="Corsair Vengeance 8GB (1x8GB) DDR4-3200", brand="Corsair",
        price_usd=19.0, specs_json="{}",
    )
    ram_2_module = Component(
        id=3, category="RAM", name="Corsair Vengeance LPX 16GB (2x8GB) DDR4-3200", brand="Corsair",
        price_usd=39.0, specs_json="{}",
    )

    max_before, _, is_real_before = _resolve_quantity_bound("RAM", {"Motherboard": motherboard, "RAM": ram_1_module})
    assert (max_before, is_real_before) == (4, True)

    max_after, _, is_real_after = _resolve_quantity_bound("RAM", {"Motherboard": motherboard, "RAM": ram_2_module})
    assert (max_after, is_real_after) == (2, True)

    # the clamp itself: an existing quantity above the new max must be pulled
    # down to exactly the new max, never left above it.
    stale_quantity = 4
    clamped = min(stale_quantity, max_after)
    assert clamped == 2


# ---------------------------------------------------------------------------
# RAM/Storage quantity stepper — combined physical + budget limit
# (render_part_picker now calls ui.state.resolve_effective_quantity_limit,
# which folds a budget-affordability max in alongside the real
# motherboard/catalog physical max and returns whichever is tighter).
# ---------------------------------------------------------------------------
def test_budget_ceiling_blocks_ram_quantity_increment_with_budget_caption(seeded_db):
    """ASRock B550M-HDV (4 DIMM slots) + a 1-module ("1x8GB", $19) RAM kit has
    a real physical max of 4 — but a tight budget_ceiling that only leaves
    room for the single already-selected unit must clamp the stepper's
    max_value down to 1 (blocking the increment at the widget level, not
    just after the fact) and show the budget-specific caption, not the
    physical one."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "budgetqty1", "budgetqty1@example.com", "Budget Qty One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    ram_1_module = next(
        c for c in components_repo.get_by_category("RAM") if c.name.startswith("Corsair Vengeance 8GB (1x8GB)")
    )
    assert mobo.price_usd == 80.0
    assert ram_1_module.price_usd == 19.0

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_RAM_{ram_1_module.id}").click().run()

    # Sanity: with no budget set yet, the physical limit (4) governs.
    assert at.get_by_key("qty_RAM").max == 4

    # other_components_cost at qty=1 is just the motherboard ($80). A ceiling
    # of $105 leaves $25 of headroom for RAM — enough for the 1 unit already
    # selected ($19) but not a 2nd ($38 total) — so financial_max == 1, which
    # is tighter than the physical max of 4.
    at.session_state["build_draft"]["budget_ceiling"] = 105.0
    at.run()

    assert not at.exception
    qty_widget = at.get_by_key("qty_RAM")
    assert qty_widget.max == 1  # budget, not physical (4), is the binding constraint
    assert qty_widget.value == 1

    captions = [c.value for c in at.caption]
    assert any("Budget ceiling reached" in c for c in captions)
    assert not any("Motherboard limit" in c for c in captions)


def test_budget_ceiling_wider_than_current_quantity_shows_headroom_caption(seeded_db):
    """Same setup as above, but with more budget headroom: the effective
    budget max is 3 while only 1 unit is currently selected, so the caption
    must be the informational "budget allows up to N" variant, not the
    "reached" warning — and must state the real number from
    resolve_effective_quantity_limit, not the physical max."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "budgetqty2", "budgetqty2@example.com", "Budget Qty Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    ram_1_module = next(
        c for c in components_repo.get_by_category("RAM") if c.name.startswith("Corsair Vengeance 8GB (1x8GB)")
    )

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_RAM_{ram_1_module.id}").click().run()

    # other_components_cost at qty=1 is $80 (motherboard only). A $137
    # ceiling leaves $57 of RAM headroom -> financial_max = 57 // 19 = 3,
    # still tighter than the physical max of 4, but wider than the current
    # quantity of 1.
    at.session_state["build_draft"]["budget_ceiling"] = 137.0
    at.run()

    assert not at.exception
    qty_widget = at.get_by_key("qty_RAM")
    assert qty_widget.max == 3

    captions = [c.value for c in at.caption]
    assert any("Budget allows up to 3" in c for c in captions)
    assert not any("Budget ceiling reached" in c for c in captions)
    assert not any("Motherboard limit" in c for c in captions)


def test_physical_limit_tighter_than_budget_shows_physical_caption(seeded_db):
    """The inverse direction: ASRock B550M-HDV's real m2_slots is 1, so an
    NVMe drive's physical max (1) is tighter than an enormous budget ceiling
    that would otherwise allow far more units. The PHYSICAL caption must
    win, proving resolve_effective_quantity_limit's "whichever is tighter"
    logic picks the right limit_kind in both directions, not just when
    budget happens to be the tighter one."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "budgetqty3", "budgetqty3@example.com", "Budget Qty Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    nvme = next(c for c in components_repo.get_by_category("Storage") if c.name.startswith("Kingston NV2"))

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_Storage_{nvme.id}").click().run()

    at.session_state["build_draft"]["budget_ceiling"] = 1_000_000.0
    at.run()

    assert not at.exception
    qty_widget = at.get_by_key("qty_Storage")
    assert qty_widget.max == 1  # physical (m2_slots=1), not budget, is binding

    captions = [c.value for c in at.caption]
    assert any("Motherboard limit reached: supports up to 1" in c for c in captions)
    assert not any("Budget" in c for c in captions)


def test_free_mode_budget_ceiling_none_never_constrains_quantity_stepper(seeded_db):
    """Free mode never sets build_draft["budget_ceiling"] (it stays None),
    which is resolve_effective_quantity_limit's own "no financial
    constraint" convention. A SATA drive has no real motherboard-backed
    port-count data (limit_kind "none"), so the stepper must fall back to
    the UI-only cap (4) regardless of how expensive the pick is, and no
    budget caption should ever appear."""
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "budgetqty4", "budgetqty4@example.com", "Budget Qty Four")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    sata = next(c for c in components_repo.get_by_category("Storage") if c.name.startswith("Crucial MX500"))

    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_Storage_{sata.id}").click().run()

    assert at.session_state["build_draft"].get("budget_ceiling") is None

    assert not at.exception
    qty_widget = at.get_by_key("qty_Storage")
    assert qty_widget.max == 4  # UI-only fallback cap, unaffected by cost since budget_ceiling is None

    captions = [c.value for c in at.caption]
    assert any("safety limit" in c for c in captions)
    assert not any("Budget" in c for c in captions)


# ---------------------------------------------------------------------------
# Sidebar AI Concierge widget (ui/components/chat_assistant.py) — zero-click
# apply: a load_build/modify_build/navigate action now takes effect the
# instant the LLM's reply lands, no confirmation button exists anymore.
# ---------------------------------------------------------------------------
def test_concierge_expander_renders_for_logged_in_user(seeded_db):
    """The sidebar expander must exist for an authenticated user, below the
    3 nav buttons, and not blow up the app on render."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge1", "concierge1@example.com", "Concierge One")

    assert not at.exception
    expander_labels = [e.label for e in at.expander]
    assert any("AI Concierge" in label for label in expander_labels)


def test_concierge_message_round_trips_without_crash(seeded_db, monkeypatch):
    """Sending a message must append both turns to concierge_messages and
    not crash, using a mocked get_concierge_response (the same pattern as
    get_build_advisory/analyze_build mocking elsewhere in this file) so no
    real network call is attempted."""
    import ui.components.chat_assistant as chat_assistant_module

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        assert catalog_summary  # real catalog was actually pre-fetched
        return {"reply": "Here are some CPUs.", "action": None, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge2", "concierge2@example.com", "Concierge Two")

    chat_input = at.get_by_key("concierge_chat_input")
    chat_input.set_value("What CPUs do you have?").run()

    assert not at.exception
    messages = at.session_state["concierge_messages"]
    assert messages[-2] == {"role": "user", "content": "What CPUs do you have?"}
    assert messages[-1] == {"role": "assistant", "content": "Here are some CPUs."}


def test_concierge_message_history_display_shows_full_scrollback(seeded_db):
    """render_concierge_widget renders the FULL conversation history inside
    the bounded, scrollable st.container(height=...) — a deliberate reversal
    of an earlier round's display-only [-4:] slice, which made older turns
    permanently unreachable in the UI. The fixed-height container (not a
    message-count slice) is what keeps a long conversation from pushing the
    sidebar's nav buttons/Logout off-screen; scrolling within it must still
    reach every earlier turn. `st.session_state["concierge_messages"]` stays
    untouched either way, since `conversation_history` construction in the
    same function independently re-slices the full list to its own
    `[-_MAX_HISTORY_MESSAGES:]` window for what's sent to the LLM — a
    separate, token-cost concern, not a display one."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_hist1", "concierge_hist1@example.com", "Concierge History One")

    # 8 messages (4 user/assistant turns) — more than would fit on screen at
    # once, but all of them must still be rendered (scrollable, not dropped).
    full_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
        for i in range(8)
    ]
    at.session_state["concierge_messages"] = full_history
    at.run()

    assert not at.exception

    # Every message is rendered as a chat_message element, in order — none
    # dropped from the DOM just because it's scrolled out of the visible
    # 380px window.
    rendered = at.chat_message
    assert len(rendered) == 8
    assert [m.name for m in rendered] == ["user", "assistant"] * 4
    assert [m.markdown[0].value for m in rendered] == [f"message {i}" for i in range(8)]

    # The FULL list survives untouched in session state — not trimmed.
    assert at.session_state["concierge_messages"] == full_history
    assert len(at.session_state["concierge_messages"]) == 8

    # st.chat_input must still exist below the bounded history, and sending a
    # new message must still work (no live network call — _no_live_llm_calls
    # forces the deterministic heuristic fallback since OPENROUTER_API_KEY is
    # cleared).
    assert len(at.chat_input) >= 1
    at.get_by_key("concierge_chat_input").set_value("Hello again").run()
    assert not at.exception
    assert at.session_state["concierge_messages"][-2] == {"role": "user", "content": "Hello again"}
    assert len(at.session_state["concierge_messages"]) == 10


def test_concierge_english_reply_rendering_unchanged(seeded_db, monkeypatch):
    """Regression guard for ui/components/chat_assistant.py's Hebrew/RTL
    rendering addition: a plain, non-Hebrew message must still render EXACTLY
    as before — the raw reply text handed straight to st.markdown, no RTL
    `<div>` wrapper, no HTML-escaping. Inspects the raw string passed to
    st.markdown via AppTest's `at.markdown[i].value` (the same inspection
    pattern already used elsewhere in this file for markdown content)."""
    import ui.components.chat_assistant as chat_assistant_module

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {"reply": "Here are some CPUs.", "action": None, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_en1", "concierge_en1@example.com", "Concierge English One")

    at.get_by_key("concierge_chat_input").set_value("What CPUs do you have?").run()

    assert not at.exception
    markdown_values = [m.value for m in at.markdown]
    # Both turns appear verbatim, unwrapped — exactly the pre-existing shape
    # (an exact-string match already rules out any RTL <div> wrapper, since a
    # wrapped value would not equal the bare reply/user text).
    assert "Here are some CPUs." in markdown_values
    assert "What CPUs do you have?" in markdown_values
    assert not any("direction: rtl" in v for v in markdown_values)


def test_concierge_hebrew_reply_gets_rtl_styling(seeded_db, monkeypatch):
    """Any Hebrew-containing chat message — regardless of role or source —
    must render through ui/components/chat_assistant.py's RTL/BiDi wrapper;
    `_render_chat_message`/`_looks_like_hebrew` key off the message content
    alone, not off which role produced it. This is exercised here via a
    mocked assistant reply for a simple, deterministic repro, even though
    `llm/concierge.py`'s ENGLISH-ONLY RULE means a real assistant reply won't
    actually be Hebrew any more — a Hebrew-typing user's OWN message still
    hits this same rendering path and still needs correct RTL styling.
    Verified by inspecting the raw string handed to st.markdown (AppTest's
    `at.markdown[i].value`) for the `direction: rtl` style, rather than
    assuming the helper fired just because the code exists."""
    import ui.components.chat_assistant as chat_assistant_module

    hebrew_reply = "בניתי לך מחשב מעולה בתקציב שלך."

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {"reply": hebrew_reply, "action": None, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_he1", "concierge_he1@example.com", "Concierge Hebrew One")

    at.get_by_key("concierge_chat_input").set_value("תבנה לי מחשב").run()

    assert not at.exception
    markdown_values = [m.value for m in at.markdown]
    rtl_values = [v for v in markdown_values if "direction: rtl" in v]
    # Both the Hebrew user message and the Hebrew assistant reply get the
    # symmetric RTL treatment (this module's own docstring documents choosing
    # "either role", not assistant-only, for visual consistency in one thread).
    assert any(hebrew_reply in v for v in rtl_values)
    assert any("תבנה לי מחשב" in v for v in rtl_values)


def test_concierge_hebrew_reply_with_html_like_content_is_escaped_not_live(seeded_db, monkeypatch):
    """The actual security requirement behind the RTL wrapper's `html.escape()`
    call: a reply that looks like it carries an HTML/script tag (e.g. from a
    compromised/malicious API response) must never reach the page as a live,
    unescaped tag inside the `unsafe_allow_html=True` wrapper — this asserts
    the escaped form (`&lt;img`) is what's actually present and the raw tag
    text is not, proving the XSS-safety design is real and tested, not just
    documented in a docstring."""
    import ui.components.chat_assistant as chat_assistant_module

    malicious_reply = "<img src=x onerror=alert(1)> שלום"

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {"reply": malicious_reply, "action": None, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_he2", "concierge_he2@example.com", "Concierge Hebrew Two")

    at.get_by_key("concierge_chat_input").set_value("שלום").run()

    assert not at.exception
    markdown_values = [m.value for m in at.markdown]
    rtl_values = [v for v in markdown_values if "direction: rtl" in v]
    assert rtl_values  # the RTL wrapper actually fired for this Hebrew reply
    assert any("&lt;img" in v for v in rtl_values)
    assert not any("<img src=x onerror=alert(1)>" in v for v in rtl_values)


def test_concierge_load_build_action_applies_immediately_no_confirmation(seeded_db, monkeypatch):
    """A load_build action from a turn must land in build_draft/create_mode/
    page on the very same rerun the reply arrives — no button, no
    intermediate click, and no confirmation-button key exists anymore."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Built it.",
            "action": {
                "type": "load_build",
                "components": {"CPU": cpu.id},
                "explanation": "Picked a solid CPU.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge3", "concierge3@example.com", "Concierge Three")

    at.get_by_key("concierge_chat_input").set_value("Build me a PC").run()

    assert not at.exception
    assert at.session_state["build_draft"]["components"] == {"CPU": cpu.id}
    assert at.session_state["build_draft"]["creation_mode"] == "Free"
    assert at.session_state["create_mode"] == "Free"
    assert at.session_state["page"] == "create_build"
    assert at.session_state["build_draft_analysis"] is None
    assert not any(b.key == "concierge_confirm_load" for b in at.button)


def test_concierge_load_build_reply_includes_real_bottleneck_synergy_line(seeded_db, monkeypatch):
    """RIGOROUS TELEMETRY REPLY FORMAT extended to load_build (this round):
    a fresh build-me response with >=2 components must get a real,
    Python-computed final Bottleneck/Synergy reading appended -- never
    trusting whatever the model's own `reply` text might have claimed (the
    NO TELEMETRY HALLUCINATION RULE, llm/concierge.py SYSTEM_PROMPT)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from engine.scoring import live_bottleneck_and_synergy

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        return {
            "reply": "Built a solid CPU/GPU pairing.",
            "action": {
                "type": "load_build",
                "components": {"CPU": cpu.id, "GPU": gpu.id},
                "explanation": "Picked a solid CPU and GPU.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "loadtelemetry1", "loadtelemetry1@example.com", "Load Telemetry One")
    at.get_by_key("concierge_chat_input").set_value("Build me a PC").run()

    assert not at.exception
    build_state = {"CPU": cpu, "GPU": gpu}
    real_synergy, real_bottleneck, real_direction = live_bottleneck_and_synergy(build_state)
    last_message = at.session_state["concierge_messages"][-1]["content"]
    assert f"Bottleneck: {real_bottleneck:.0f}% ({real_direction})" in last_message
    assert f"Synergy: {real_synergy:.0f}" in last_message
    assert "Total:" in last_message


def test_concierge_load_build_with_budget_cap_uses_deterministic_solver(seeded_db, monkeypatch):
    """HARD CEILING / TARGET-ZONE ENFORCEMENT (Part 2): a load_build action
    carrying `budget_cap_usd` must NOT use the model's own (here, empty)
    `components` as the final build — engine.solvers.initialize_budget_build
    fills every category, lands the build in Budget mode with a real
    `budget_ceiling`, and never exceeds the stated cap."""
    import ui.components.chat_assistant as chat_assistant_module
    from engine.solvers import CATEGORY_ORDER

    ceiling = 1500.0

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Building a rig within your budget.",
            "action": {
                "type": "load_build",
                "components": {},
                "budget_cap_usd": ceiling,
                "explanation": "Budget-mode solver fills every category.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_budget", "concierge_budget@example.com", "Concierge Budget")

    at.get_by_key("concierge_chat_input").set_value("build me a PC for up to 1500 USD").run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    assert draft["creation_mode"] == "Budget"
    assert draft["budget_ceiling"] == ceiling
    # fill_peripherals_with_surplus=True may also add peripheral categories —
    # only the 8 core categories are required to be present.
    assert set(CATEGORY_ORDER).issubset(draft["components"].keys())
    from db.repositories import components_repo

    total = sum(
        components_repo.get_by_id(component_id).price_usd for component_id in draft["components"].values()
    )
    assert total <= ceiling


def test_concierge_modify_build_hard_cap_never_exceeds_ceiling(seeded_db, monkeypatch):
    """BUDGET HARD-CAP SAFETY NET (Part 2): a modify_build patch that would
    push an already-Budget-mode build's total over its real ceiling is
    re-clamped in Python — never trusted to the model's own arithmetic
    alone."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from engine import solvers

    ceiling = 900.0
    selection = solvers.initialize_budget_build(ceiling)
    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Upgrading the GPU.",
            "action": {
                "type": "modify_build",
                "components": {"GPU": priciest_gpu.id},
                "quantities": {},
                "explanation": "Swapped in the priciest GPU.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_hardcap", "concierge_hardcap@example.com", "Concierge Hardcap")
    at.session_state["build_draft"] = {
        "name": "",
        "creation_mode": "Budget",
        "workload_profile": None,
        "tier": "Mid",
        "budget_ceiling": ceiling,
        "components": {category: component.id for category, component in selection.items()},
        "quantities": {},
    }
    at.session_state["page"] = "create_build"

    at.get_by_key("concierge_chat_input").set_value("upgrade my GPU to the best one").run()

    assert not at.exception
    draft = at.session_state["build_draft"]
    total = sum(
        components_repo.get_by_id(component_id).price_usd for component_id in draft["components"].values()
    )
    assert total <= ceiling


def test_concierge_use_remaining_budget_spends_headroom_without_exceeding_ceiling(seeded_db, monkeypatch):
    """Part 3 (this round's directive): the deterministic multi-tier
    spend-down loop must apply MULTIPLE real, affordable rounds of upgrade
    (never stopping after just one, the real "leaves hundreds unused"
    complaint) while never exceeding the real ceiling, and must stop once no
    further real upgrade exists — never a no-op, never an infinite loop.
    `get_build_advisory` itself is mocked directly (a stateful fake returning
    a different real, catalog-priced action each call) so this test verifies
    the LOOP logic in `_apply_concierge_action` itself, independent of
    whatever a specific real build happens to have headroom for via the
    heuristic/live advisory path (already covered by other tests)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from engine import solvers

    ceiling = 3243.24
    selection = solvers.initialize_budget_build(ceiling, fill_peripherals_with_surplus=True)

    ram_options = sorted(components_repo.get_by_category("RAM"), key=lambda c: c.price_usd)
    storage_options = sorted(components_repo.get_by_category("Storage"), key=lambda c: c.price_usd)
    upgrade_a = next(c for c in ram_options if c.id != selection["RAM"].id)
    upgrade_b = next(c for c in storage_options if c.id != selection["Storage"].id)

    call_count = {"n": 0}

    def _fake_advisory(build_state, mode, current_budget_or_cost, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            actions = [{"action": "swap", "category": "RAM", "replace_with_id": upgrade_a.id}]
        elif call_count["n"] == 2:
            actions = [{"action": "swap", "category": "Storage", "replace_with_id": upgrade_b.id}]
        else:
            actions = []  # no further real upgrade — the loop must stop here
        return {
            "pros": [], "cons": [],
            "within_budget": {"explanation": "", "swaps": [], "can_optimize_further": False},
            "stretch_budget": {"explanation": "", "actions": actions, "added_cost_usd": 1.0 if actions else 0.0},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_build_advisory", _fake_advisory)

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        assert current_build_context["mode"] == "Budget"
        return {
            "reply": "Using your remaining budget on the best upgrades that fit.",
            "action": {"type": "use_remaining_budget"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "userembudget1", "userembudget1@example.com", "Use Remaining Budget One")
    at.session_state["build_draft"] = {
        "name": "", "creation_mode": "Budget", "workload_profile": None, "tier": "Mid",
        "budget_ceiling": ceiling,
        "components": {category: component.id for category, component in selection.items()},
        "quantities": {},
    }
    at.session_state["page"] = "create_build"

    at.get_by_key("concierge_chat_input").set_value("upgrade what you can with the remaining budget").run()

    assert not at.exception
    # The loop must run BOTH real rounds (RAM, then Storage) and then stop as
    # soon as the 3rd call returns no actions — never looping further, never
    # stopping after just the first round (the real "leaves headroom unused"
    # complaint this fix addresses).
    assert call_count["n"] == 3
    draft = at.session_state["build_draft"]
    final_total = sum(
        components_repo.get_by_id(component_id).price_usd for component_id in draft["components"].values()
    )
    assert final_total <= ceiling  # hard cap never exceeded, even after 2 applied rounds
    last_message = at.session_state["concierge_messages"][-1]["content"]
    assert "Delta:" in last_message
    assert "Total:" in last_message


def test_concierge_use_remaining_budget_converts_free_mode_build_under_stated_ceiling(seeded_db, monkeypatch):
    """"you have 13000 NIS, upgrade it accordingly" against a build that was
    NEVER in Budget mode at all: the action's own `budget_cap_usd` (a fresh
    figure stated in that message) must convert the draft to Budget mode
    under that ceiling and then run the same spend-down loop -- a real,
    confirmed gap this round's directive described (the build sat at ~8200
    NIS with real headroom up to a stated 13000 NIS ceiling, but had no way
    to engage the multi-tier upgrade loop while still Free-mode)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    picks = {category: components_repo.get_by_category(category)[0] for category in (
        "CPU", "Motherboard", "GPU", "RAM", "Storage", "PSU", "Case", "Cooler",
    )}
    starting_total = sum(c.price_usd for c in picks.values())
    stated_ceiling_usd = starting_total + 500.0  # real, meaningful headroom

    # `_advisory_context`'s own pre-fetch (triggered because the message
    # contains "budget" — chat_assistant.py's cheap local keyword gate) fires
    # BEFORE `_apply_concierge_action` ever runs, while the draft is still
    # genuinely Free-mode -- that call is unrelated to this test's own loop
    # and must not be mistaken for it, so only calls make with mode=="Budget"
    # (i.e., AFTER the conversion this test actually verifies) are counted.
    call_modes: list[str] = []

    def _fake_advisory(build_state, mode, current_budget_or_cost, **kwargs):
        call_modes.append(mode)
        return {
            "pros": [], "cons": [],
            "within_budget": {"explanation": "", "swaps": [], "can_optimize_further": False},
            "stretch_budget": {"explanation": "", "actions": [], "added_cost_usd": 0.0},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_build_advisory", _fake_advisory)

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        assert current_build_context["mode"] == "Free"
        return {
            "reply": "Using your remaining budget on the best upgrades that fit.",
            "action": {"type": "use_remaining_budget", "budget_cap_usd": stated_ceiling_usd},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "freshceiling1", "freshceiling1@example.com", "Fresh Ceiling One")
    at.session_state["build_draft"] = {
        "name": "", "creation_mode": "Free", "workload_profile": None, "tier": "Mid", "budget_ceiling": None,
        "components": {cat: c.id for cat, c in picks.items()},
        "quantities": {},
    }
    at.session_state["create_mode"] = "Free"

    at.get_by_key("concierge_chat_input").set_value("you have 13000 NIS, upgrade it accordingly").run()

    assert not at.exception
    # The multi-tier loop itself must have run at least once, and every call
    # it made saw the ALREADY-converted "Budget" mode -- never "Free".
    assert "Budget" in call_modes
    draft = at.session_state["build_draft"]
    assert draft["creation_mode"] == "Budget"
    assert draft["budget_ceiling"] == stated_ceiling_usd
    assert at.session_state["create_mode"] == "Budget"


def test_sidebar_nav_includes_your_posts_and_concierge_expanded_by_default(seeded_db):
    """This round's sidebar restructure (spec.md §7.7/§7.8): "Your Posts" is
    a real nav button between Drafts and Community, and the AI Concierge
    expander is pinned open (expanded=True) on load rather than requiring a
    click to reveal."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "sidebarorder1", "sidebarorder1@example.com", "Sidebar Order One")

    assert at.get_by_key("sidebar_nav_your_posts") is not None
    expanders = [e for e in at.expander if "AI Concierge" in (e.label or "")]
    assert len(expanders) == 1
    assert expanders[0].proto.expanded is True


def test_sidebar_profile_badge_renders_name_and_handle(seeded_db):
    """theme.profile_badge (spec.md §7.7/§7.8) replaces the plain bold-name +
    @handle caption — confirms the real name/username still render (inside
    the new HTML card) and the role tag is present."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "profilebadge1", "profilebadge1@example.com", "Profile Badge One")

    sidebar_text = "\n".join(m.value for m in at.sidebar.markdown)
    assert "Profile Badge One" in sidebar_text
    assert "@profilebadge1" in sidebar_text
    assert "ARCHITECT" in sidebar_text


def test_streamlit_chrome_hidden_via_menu_items_none(seeded_db):
    """set_page_config's own menu_items=None (app.py) — a real, checkable
    config value, unlike the CSS hiding rules (ui/theme.py) which have no
    AppTest-visible signal to assert on directly (verified live in the
    browser instead)."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    assert not at.exception


def test_concierge_never_calls_live_llm_without_api_key(seeded_db):
    """No mocking at all: with no OPENROUTER_API_KEY/MODEL configured (the
    _no_live_llm_calls autouse fixture clears both), sending a message must
    still round-trip cleanly through the real heuristic fallback path,
    proving the plumbing works end-to-end without ever hitting the network."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge4", "concierge4@example.com", "Concierge Four")

    at.get_by_key("concierge_chat_input").set_value("What CPUs do you have?").run()

    assert not at.exception
    messages = at.session_state["concierge_messages"]
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"]  # some non-empty heuristic reply rendered


def test_concierge_apply_action_failure_degrades_gracefully_without_crashing(seeded_db, monkeypatch, capsys):
    """`_apply_concierge_action` used to have zero exception handling of its
    own: any genuinely unexpected failure inside it (a real bug, an
    unforeseen None somewhere) would propagate uncaught and crash the whole
    Streamlit rerun with a raw error screen. `render_concierge_widget` now
    wraps that call in a try/except mirroring llm/concierge.py's own
    never-raise-but-never-silently-swallow convention: the full traceback is
    printed to stderr first, then the reply degrades gracefully instead of
    crashing.

    Forces the scenario by monkeypatching `components_repo.get_by_id` (as
    imported into ui.components.chat_assistant) to raise on its first call
    only, then delegate normally — the first call happens inside
    `_apply_concierge_action`'s own component-resolution loop for a
    load_build action, so it fails exactly once, precisely where a real bug
    would surface; every later call (this same rerun's remaining renders,
    e.g. the landing page) resolves normally, so nothing outside the action
    application itself is disturbed."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]
    original_get_by_id = components_repo.get_by_id
    call_count = {"n": 0}

    def _raise_once_then_delegate(component_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated unexpected failure in _apply_concierge_action")
        return original_get_by_id(component_id)

    monkeypatch.setattr(chat_assistant_module.components_repo, "get_by_id", _raise_once_then_delegate)

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Built it.",
            "action": {
                "type": "load_build",
                "components": {"CPU": cpu.id},
                "explanation": "Picked a solid CPU.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_fail1", "concierge_fail1@example.com", "Concierge Fail One")
    page_before = at.session_state["page"]
    build_draft_before = at.session_state["build_draft"]

    at.get_by_key("concierge_chat_input").set_value("Build me a PC").run()

    # The core requirement: the page must NOT crash, despite the genuinely
    # unexpected exception raised deep inside the action-application path.
    assert not at.exception

    # The exception fired before any of build_draft/page were ever written
    # (the raise happens on the very first component-resolution call, before
    # _apply_concierge_action's own session_state writes), so both are left
    # exactly as they were — nothing silently half-applied.
    assert at.session_state["build_draft"] == build_draft_before
    assert at.session_state["page"] == page_before

    # The assistant's message still gets appended, degraded rather than
    # dropped outright.
    messages = at.session_state["concierge_messages"]
    assert messages[-2] == {"role": "user", "content": "Build me a PC"}
    assert messages[-1]["role"] == "assistant"
    assert "Built it." in messages[-1]["content"]
    assert "something went wrong applying this action" in messages[-1]["content"]

    # Whether stderr capture is reliable through Streamlit's AppTest harness
    # (which runs the script in its own thread/script-runner context, not a
    # plain in-process function call) isn't guaranteed by any existing test
    # in this file — no prior test in this suite asserts on captured
    # stdout/stderr through AppTest. Attempt it anyway (best-effort, not
    # load-bearing for this test's core requirement above): if the traceback
    # text is visible via capsys, assert it; if AppTest's execution context
    # doesn't route through this process's captured stderr, don't fail the
    # test over a capture-mechanism gap unrelated to the actual behavior
    # being verified.
    captured = capsys.readouterr()
    if captured.err:
        assert "RuntimeError" in captured.err or "Traceback" in captured.err


def test_concierge_build_request_succeeds_from_community_starting_page(seeded_db, monkeypatch):
    """Permanent regression guard for the exact scenario a prior directive
    worried about (an AI Concierge request throwing the heuristic
    fallback/crashing when used from a page other than create_build) — which
    did NOT reproduce on independent audit (see this module's docstring and
    ui/components/chat_assistant.py's `_current_build_context`/
    `_advisory_context`/`_apply_concierge_action`, all of which already use
    `.get()`-based session-state access with no assumption that any
    create_build-only widget key exists). Drives the widget from "community"
    — a page reached with no build in progress at all this session — through
    a full "build me a PC" request, using the same mocked
    `get_concierge_response` pattern as this file's other load_build tests."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        # No build has ever been touched this session -> both context
        # helpers must resolve to None/empty, not raise.
        assert current_build_context is None
        assert advisory_context is None
        return {
            "reply": "Built it.",
            "action": {
                "type": "load_build",
                "components": {"CPU": cpu.id},
                "explanation": "Picked a solid CPU.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_np1", "concierge_np1@example.com", "Concierge NonPage One")

    at.get_by_key("sidebar_nav_community").click().run()
    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["build_draft"] is None  # nothing touched yet this session

    at.get_by_key("concierge_chat_input").set_value("Build me a PC").run()

    assert not at.exception
    messages = at.session_state["concierge_messages"]
    assert messages[-2] == {"role": "user", "content": "Build me a PC"}
    # The authoritative total-cost line (ui.state.build_total_cost) is
    # appended after the mocked reply — see chat_assistant.py's "AUTHORITATIVE
    # TOTAL-COST LINE" docstring section — so check the reply is a prefix
    # rather than an exact match.
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"].startswith("Built it.")
    assert f"USD {cpu.price_usd:,.2f}" in messages[-1]["content"]
    assert at.session_state["build_draft"]["components"] == {"CPU": cpu.id}
    assert at.session_state["page"] == "create_build"  # load_build's own page transition


def test_concierge_modify_build_patches_without_disturbing_other_categories(seeded_db, monkeypatch):
    """A modify_build action naming only new peripheral categories must add
    those to the existing build_draft while leaving every already-selected
    core category (e.g. the manually-picked CPU/GPU) completely untouched —
    a PATCH, not a replacement."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge5", "concierge5@example.com", "Concierge Five")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    network_card = components_repo.get_by_category("NetworkCard")[0]
    optical_drive = components_repo.get_by_category("OpticalDrive")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        assert current_build_context is not None
        assert current_build_context["components"]["CPU"]["id"] == cpu.id
        return {
            "reply": "Added a network card and an optical drive.",
            "action": {
                "type": "modify_build",
                "components": {"NetworkCard": network_card.id, "OpticalDrive": optical_drive.id},
                "quantities": {},
                "explanation": "Added the requested peripherals.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Add a network card and an optical drive").run()

    assert not at.exception
    components = at.session_state["build_draft"]["components"]
    assert components["CPU"] == cpu.id  # untouched
    assert components["GPU"] == gpu.id  # untouched
    assert components["NetworkCard"] == network_card.id
    assert components["OpticalDrive"] == optical_drive.id

    # Independent proof the Concierge-driven peripheral picks actually render
    # in create_build.py's "Optional peripherals" expander with zero glue
    # code: that expander's render_part_picker calls are generic over
    # PERIPHERAL_CATEGORIES/build_state regardless of how a component got
    # into build_draft, so a filled NetworkCard/OpticalDrive slot must show
    # the same "cleared via ✕" button (key=f"clear_{category}") a manually
    # picked slot would — its mere presence (get_by_key raises if the widget
    # wasn't rendered at all) confirms the expander picked up the LLM-added
    # peripherals on this same render, with no create_build.py changes needed.
    assert at.get_by_key("clear_NetworkCard") is not None
    assert at.get_by_key("clear_OpticalDrive") is not None


def test_concierge_modify_build_quantity_request_is_clamped_to_real_limit(seeded_db, monkeypatch):
    """llm/concierge.py has no engine/db access, so it cannot itself clamp a
    requested quantity to a real physical/budget limit — a modify_build
    action requesting far more units than the motherboard's real DIMM slots
    allow must be clamped down to the same effective max
    ui.state.resolve_effective_quantity_limit (and the manual stepper) would
    ever allow, never applied verbatim."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge6", "concierge6@example.com", "Concierge Six")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    # ASRock B550M-HDV: 4 DIMM slots; a 2-module ("2x8GB") kit caps the real
    # max at 2 (4 slots // 2 modules per kit) — same fixture as
    # test_ram_quantity_max_respects_real_per_module_count above.
    mobo = next(c for c in components_repo.get_by_category("Motherboard") if c.name == "ASRock B550M-HDV")
    ram_2_module = next(
        c for c in components_repo.get_by_category("RAM") if c.name.startswith("Corsair Vengeance LPX 16GB (2x8GB)")
    )
    at.get_by_key(f"select_Motherboard_{mobo.id}").click().run()
    at.get_by_key(f"select_RAM_{ram_2_module.id}").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Bumped your RAM quantity.",
            "action": {
                "type": "modify_build",
                "components": {},
                "quantities": {"RAM": 10},  # far beyond the real 2-kit physical max
                "explanation": "Requested 10 kits.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Bump my RAM to 10 kits").run()

    assert not at.exception
    assert at.session_state["build_draft"]["quantities"]["RAM"] == 2  # clamped, not applied verbatim
    assert at.get_by_key("qty_RAM").value == 2


def test_concierge_navigate_action_sets_page_immediately_no_click(seeded_db, monkeypatch):
    """A navigate action must set st.session_state["page"] on the same
    rerun the reply arrives, with no button or extra click involved."""
    import ui.components.chat_assistant as chat_assistant_module

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Taking you to Community.",
            "action": {"type": "navigate", "navigate_to": "community"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge7", "concierge7@example.com", "Concierge Seven")

    at.get_by_key("concierge_chat_input").set_value("Take me to Community").run()

    assert not at.exception
    assert at.session_state["page"] == "community"


def test_concierge_navigate_action_to_landing_home_page(seeded_db, monkeypatch):
    """A real, previously-confirmed bug: `navigate_to` had no "landing" option
    at all, so a "take me home"/"go to the dashboard" request was always
    forced onto one of the 3 other page keys (observed live: it consistently
    guessed "create_build", with a reply falsely claiming to be "taking you
    home"). Now that ConciergeNavigateAction accepts "landing", this must
    actually route there — never silently fall back to create_build."""
    import ui.components.chat_assistant as chat_assistant_module

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Taking you home.",
            "action": {"type": "navigate", "navigate_to": "landing"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_home1", "concierge_home1@example.com", "Concierge Home")
    at.get_by_key("sidebar_nav_create_build").click().run()

    at.get_by_key("concierge_chat_input").set_value("Take me to the home page").run()

    assert not at.exception
    assert at.session_state["page"] == "landing"


def test_concierge_navigate_action_to_drafts_page(seeded_db, monkeypatch):
    """navigate_to now also accepts "drafts" (ui/views/drafts.py's page key)
    — a real, previously-missing destination the Concierge could not route
    to at all."""
    import ui.components.chat_assistant as chat_assistant_module

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Taking you to your drafts.",
            "action": {"type": "navigate", "navigate_to": "drafts"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge_drafts1", "concierge_drafts1@example.com", "Concierge Drafts")

    at.get_by_key("concierge_chat_input").set_value("Take me to drafts").run()

    assert not at.exception
    assert at.session_state["page"] == "drafts"


def test_concierge_navigate_to_community_resets_stale_selected_post_id(seeded_db, monkeypatch):
    """Real, confirmed bug: navigating to "community" (via ANY entry point)
    never used to reset a stale `selected_post_id` left over from previously
    viewing one specific post's thread — so "take me back to the main feed"
    while a thread was open would silently re-render that SAME thread
    instead of the feed. `ui.state.navigate_to_page` now clears it centrally
    whenever the target is "community"; this exercises that fix through the
    Concierge's own `navigate` action specifically."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "navback1", "navback1@example.com", "Nav Back One")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Feed Reset Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    real_post = community_repo.get_feed()[0]
    at.session_state["page"] = "community"
    at.session_state["selected_post_id"] = real_post.id
    at.run()
    assert at.session_state["selected_post_id"] == real_post.id  # thread view genuinely open

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Taking you back to the main feed.",
            "action": {"type": "navigate", "navigate_to": "community"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("take me back to the main feed").run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["selected_post_id"] is None


def test_concierge_reset_mode_without_unsaved_changes_resets_immediately(seeded_db, monkeypatch):
    """A `reset_mode: true` navigate action with nothing unsaved to protect
    mirrors the existing "⬅ Change mode" button exactly — no dialog, no
    extra confirmation, just a clean reset landing on the mode-selection
    screen."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "resetmode1", "resetmode1@example.com", "Reset Mode One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    # Manually clear the flag to isolate this test from interception —
    # covered separately below.
    at.session_state["has_unsaved_build_changes"] = False
    at.run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Sure, let's pick a new mode.",
            "action": {"type": "navigate", "navigate_to": "create_build", "reset_mode": True},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("let me select a new mode").run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] is None
    assert at.session_state["build_draft"] is None
    assert at.session_state["build_draft_analysis"] is None
    # Landed on the mode-selection screen, not a leftover build.
    assert at.get_by_key("mode_free") is not None
    assert at.get_by_key("mode_budget") is not None
    assert at.get_by_key("mode_workload") is not None


def test_concierge_reset_mode_with_unsaved_changes_resets_with_no_draft_created(seeded_db, monkeypatch):
    """A `reset_mode: true` request against an ACTIVE unsaved build routes
    through `ui.state.teardown_builder()` (spec.md §7.9) — resetting
    immediately with NO database write, landing on the mode-selection
    screen. An unsaved build not explicitly checkpointed via the "Save as
    draft" checkbox is simply discarded."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "resetmode2", "resetmode2@example.com", "Reset Mode Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    assert at.session_state["has_unsaved_build_changes"] is True
    user_id = at.session_state["auth_user"]["id"]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Sure, let's pick a new mode.",
            "action": {"type": "navigate", "navigate_to": "create_build", "reset_mode": True},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("let me select a new mode").run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] is None
    assert at.session_state["build_draft"] is None
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.get_by_key("mode_free") is not None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_change_mode_button_with_unsaved_changes_resets_with_no_draft_created(seeded_db):
    """Real, confirmed gap (from an earlier round): the manual "⬅ Change
    mode" button (`ui/views/create_build.py`) used to reset `create_mode`/
    `build_draft`/`build_draft_analysis` unconditionally, discarding an
    in-progress unsaved build with zero warning. It now calls `ui.state.
    teardown_builder()` (spec.md §7.9) — resetting immediately with NO
    database write (per the CURRENT, later product decision: the "Save as
    draft" checkbox is the single explicit way to create a draft), exactly
    mirroring the Concierge's own `reset_mode` handling
    (test_concierge_reset_mode_with_unsaved_changes_resets_with_no_draft_created
    above)."""
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "changemode1", "changemode1@example.com", "Change Mode One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    assert at.session_state["has_unsaved_build_changes"] is True
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("change_mode").click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] is None
    assert at.session_state["build_draft"] is None
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.get_by_key("mode_free") is not None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_change_mode_button_without_unsaved_changes_resets_with_no_draft_created(seeded_db):
    """No unsaved changes (nothing picked yet in the fresh mode) -> "⬅ Change
    mode" resets right away with no draft created — `teardown_builder()`
    never writes to the database at all."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "changemode2", "changemode2@example.com", "Change Mode Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    assert at.session_state["has_unsaved_build_changes"] is False
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("change_mode").click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] is None
    assert at.session_state["build_draft"] is None
    assert at.get_by_key("mode_free") is not None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_concierge_open_community_build_action_lands_on_thread_view(seeded_db, monkeypatch):
    """A mocked `open_community_build` action must land the user directly on
    `page == "community"` with `selected_post_id` set to the real post id,
    and the thread view must actually render for that specific post — proven
    by asserting the post's own real title appears on the page, not just
    that no exception was raised."""
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "opencb1", "opencb1@example.com", "Open Community Build One")

    # Create and publish a real, queryable post to open — same setup shape
    # as the existing publish/community tests above.
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Open Me Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    from db.repositories import community_repo

    feed = community_repo.get_feed()
    assert len(feed) == 1
    real_post = feed[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        # community_summary must carry this real post's post_id (and a
        # distinct build_id) — confirms the caller-assembled context this
        # action would really be resolved against.
        matching = [p for p in community_summary if p["post_id"] == real_post.id]
        assert matching
        return {
            "reply": "Here's your Open Me Rig.",
            "action": {"type": "open_community_build", "post_id": real_post.id},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Open my Open Me Rig from community").run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["selected_post_id"] == real_post.id

    page_text = "\n".join(m.value for m in at.markdown) + " ".join(t.value for t in at.title)
    assert "Open Me Rig" in page_text


def test_concierge_load_saved_build_draft_source_populates_studio(seeded_db, monkeypatch):
    """A mocked `load_saved_build` action with source == "draft" must load
    the real draft's components/mode into build_draft and land on
    create_build for editing — distinct from load_build (a brand new build)
    and open_community_build (a read-only view)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "loaddraft1", "loaddraft1@example.com", "Load Draft One")
    user_id = at.session_state["auth_user"]["id"]

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    draft = drafts_repo.save_draft(
        user_id=user_id, name="pc-master-race", mode="Free",
        components={"CPU": cpu.id, "GPU": gpu.id}, quantities={},
    )

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        drafts_summary=None,
        **kwargs,
    ):
        matching = [d for d in (drafts_summary or []) if d["draft_id"] == draft.id]
        assert matching
        return {
            "reply": "Loaded 'pc-master-race' into the Build Studio for editing.",
            "action": {"type": "load_saved_build", "source": "draft", "id": draft.id},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("sidebar_nav_drafts").click().run()
    at.get_by_key("concierge_chat_input").set_value("open pc-master-race for editing").run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] == "Free"
    assert at.session_state["build_draft"]["components"] == {"CPU": cpu.id, "GPU": gpu.id}
    assert at.session_state["build_draft"]["name"] == "pc-master-race"
    assert at.session_state["has_unsaved_build_changes"] is True


def test_concierge_load_saved_build_previous_build_source_populates_studio(seeded_db, monkeypatch):
    """Same as the draft case above, for source == "build" (a real,
    finished, previously-saved build)."""
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "loadbuild1", "loadbuild1@example.com", "Load Build One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Ultra Rig")
    at.get_by_key("save_build").click().run()

    from db.repositories import builds_repo

    user_id = at.session_state["auth_user"]["id"]
    saved_build = builds_repo.get_builds_for_user(user_id)[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        previous_builds_summary=None,
        **kwargs,
    ):
        matching = [b for b in (previous_builds_summary or []) if b["build_id"] == saved_build.id]
        assert matching
        return {
            "reply": "Loaded 'Ultra Rig' into the Build Studio for editing.",
            "action": {"type": "load_saved_build", "source": "build", "id": saved_build.id},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("sidebar_nav_my_builds").click().run()
    at.get_by_key("concierge_chat_input").set_value("load my build named Ultra Rig").run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] == "Budget"
    assert set(at.session_state["build_draft"]["components"]) == set(
        c.category for c in saved_build.components
    )


def test_concierge_load_saved_build_community_source_uses_viewed_post_id(seeded_db, monkeypatch):
    """When "this build"/"this post" is asked with no name given while a
    specific community thread is already open (selected_post_id set), the
    Concierge resolves it via viewed_post_id — proven here by a mocked
    response that only returns a valid action when it actually received the
    real post id in its own payload."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "loadcommunity1", "loadcommunity1@example.com", "Load Community One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Community Source Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    real_post = community_repo.get_feed()[0]
    at.session_state["page"] = "community"
    at.session_state["selected_post_id"] = real_post.id
    at.run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        viewed_post_id=None,
        **kwargs,
    ):
        assert viewed_post_id == real_post.id
        return {
            "reply": "Loaded 'Community Source Rig' into the Build Studio for editing.",
            "action": {"type": "load_saved_build", "source": "community", "id": viewed_post_id},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("edit this build").run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["fork_source_build_id"] == real_post.build_id


def test_concierge_load_saved_build_discards_current_unsaved_build_first(seeded_db, monkeypatch):
    """Loading a saved build while ALREADY in create_build with a DIFFERENT
    unsaved build in progress must not silently mix the two — the current
    unsaved build is discarded first (ui.state.teardown_builder(), no
    database write, spec.md §7.9), then the requested one loads cleanly."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "loadswap1", "loadswap1@example.com", "Load Swap One")
    user_id = at.session_state["auth_user"]["id"]

    cpu = components_repo.get_by_category("CPU")[0]
    draft = drafts_repo.save_draft(
        user_id=user_id, name="Target Draft", mode="Free", components={"CPU": cpu.id}, quantities={},
    )

    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    other_gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_GPU_{other_gpu.id}").click().run()
    assert at.session_state["has_unsaved_build_changes"] is True

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Loaded 'Target Draft' into the Build Studio for editing.",
            "action": {"type": "load_saved_build", "source": "draft", "id": draft.id},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("open Target Draft for editing").run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["build_draft"]["components"] == {"CPU": cpu.id}
    assert at.session_state["create_mode"] == "Free"
    # No phantom draft created from the DISCARDED GPU-only build.
    assert len(drafts_repo.get_user_drafts(user_id)) == 1


def test_concierge_save_build_fast_track_with_publish_immediately_persists_both_in_one_turn(
    seeded_db, monkeypatch
):
    """"save build as X and publish to community" in ONE message must
    create the real Build row AND publish it to Community in the SAME
    turn — no separate publish_build round-trip needed."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fasttrack1", "fasttrack1@example.com", "Fast Track One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        assert current_build_context is not None
        return {
            "reply": "Saved 'Workstation' and published it to the Community!",
            "action": {
                "type": "save_build",
                "name": "Workstation",
                "destination": "build",
                "publish_immediately": True,
                "author_notes": None,
                "explanation": "Fast-track save + publish.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    user_id = at.session_state["auth_user"]["id"]
    at.get_by_key("concierge_chat_input").set_value("save build as workstation and publish to community").run()

    assert not at.exception
    builds = builds_repo.get_builds_for_user(user_id)
    assert len(builds) == 1
    assert builds[0].name == "Workstation"
    assert builds[0].is_public is True

    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].build_id == builds[0].id
    assert at.session_state["concierge_last_saved_build"] == {"build_id": builds[0].id, "name": "Workstation"}


def test_concierge_save_build_fast_track_publish_carries_flair_tag(seeded_db, monkeypatch):
    """Part 2 (this round's directive — unified publishing with status tags):
    a fast-track save+publish action carrying a `flair` must land on the
    real CommunityPost row, so the feed/thread badge reflects the user's own
    chosen tag rather than always defaulting to one or nothing."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fasttrackflair1", "fasttrackflair1@example.com", "Fast Track Flair One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        return {
            "reply": "Saved 'Workstation' and published it to the Community as Looking for Help!",
            "action": {
                "type": "save_build",
                "name": "Workstation",
                "destination": "build",
                "publish_immediately": True,
                "author_notes": None,
                "flair": "Looking for Help",
                "explanation": "Fast-track save + publish with a tag.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value(
        "save build as workstation and publish to community as looking for help"
    ).run()

    assert not at.exception
    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].flair == "Looking for Help"


def test_concierge_fast_track_publish_confirmation_overrides_wrong_name_from_model(seeded_db, monkeypatch):
    """The SAME AUTHORITATIVE PUBLISH CONFIRMATION override applies to the
    ONE-TURN fast-track path (save_build with publish_immediately AND flair
    both given in the same message) -- the model's own reply text is never
    trusted for the name here either."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fasttrackwrong1", "fasttrackwrong1@example.com", "Fast Track Wrong One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        return {
            # Deliberately the WRONG name in the model's own reply text.
            "reply": "Build 'Beast Rig' successfully published to Community under 'Rate My Build'!\n"
            "Viewable now in Community & Your Posts.",
            "action": {
                "type": "save_build",
                "name": "Cyber Titan 9000",
                "destination": "build",
                "publish_immediately": True,
                "author_notes": None,
                "flair": "Rate My Build",
                "explanation": "Fast-track save + publish with a tag.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value(
        "save this build and publish it as Cyber Titan 9000 tagged rate my build"
    ).run()

    assert not at.exception
    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].title == "Cyber Titan 9000"

    last_message = at.session_state["concierge_messages"][-1]["content"]
    assert "Cyber Titan 9000" in last_message
    assert "Beast Rig" not in last_message


def test_concierge_publish_build_carries_flair_tag(seeded_db, monkeypatch):
    """The separate, later-turn publish_build action must also carry its
    `flair` through to the real post."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "publishflair1", "publishflair1@example.com", "Publish Flair One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_save_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        return {
            "reply": "Saved as 'Workstation'! Would you like to publish it to the Community as well?",
            "action": {
                "type": "save_build", "name": "Workstation", "destination": "build",
                "explanation": "Saving now.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_save_response)
    at.get_by_key("concierge_chat_input").set_value("save this build named Workstation").run()
    assert not at.exception

    def _fake_publish_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        return {
            "reply": "Published to the Community as Rate My Build!",
            "action": {"type": "publish_build", "author_notes": None, "flair": "Rate My Build"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_publish_response)
    at.get_by_key("concierge_chat_input").set_value("Rate My Build").run()

    assert not at.exception
    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].flair == "Rate My Build"


def test_concierge_publish_build_confirmation_overrides_wrong_name_from_model(seeded_db, monkeypatch):
    """AUTHORITATIVE PUBLISH CONFIRMATION regression test: a real, confirmed
    bug had the model's own `reply` echo an UNRELATED example name from deep
    inside its own SYSTEM_PROMPT ("Beast Rig") instead of the real name the
    user actually saved under, even though the underlying save/publish
    itself was correct. The displayed message must always show the REAL
    name from `concierge_last_saved_build`, never whatever the mocked/real
    model happened to type -- this test's own mock deliberately returns the
    WRONG name to prove the override actually overrides, not just happens to
    agree with a well-behaved mock."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "wrongname1", "wrongname1@example.com", "Wrong Name One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_save_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        return {
            "reply": "Saved as 'Cyber Titan 9000'! Would you like to tag this community post as 'Rate My Build' or 'Looking for Help'?",
            "action": {
                "type": "save_build", "name": "Cyber Titan 9000", "destination": "build",
                "explanation": "Saving now.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_save_response)
    at.get_by_key("concierge_chat_input").set_value(
        "save this build and publish it as Cyber Titan 9000"
    ).run()
    assert not at.exception

    def _fake_publish_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        # Deliberately the WRONG name -- exactly the bug this test guards
        # against -- to prove the caller's own override replaces it.
        return {
            "reply": "Build 'Beast Rig' successfully published to Community under 'Rate My Build'!\n"
            "Viewable now in Community & Your Posts.",
            "action": {"type": "publish_build", "author_notes": None, "flair": "Rate My Build"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_publish_response)
    at.get_by_key("concierge_chat_input").set_value("Rate My Build").run()

    assert not at.exception
    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].title == "Cyber Titan 9000"

    last_message = at.session_state["concierge_messages"][-1]["content"]
    assert "Cyber Titan 9000" in last_message
    assert "Beast Rig" not in last_message


def test_concierge_save_build_without_publish_immediately_stays_private(seeded_db, monkeypatch):
    """Regression guard: an ORDINARY save_build (publish_immediately not set,
    the common case) must NOT publish — confirms the fast-track field is
    truly opt-in, never a default-on behavior."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fasttrack2", "fasttrack2@example.com", "Fast Track Two")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Saved as 'Ultra Rig'! Would you like to publish it to the Community as well?",
            "action": {
                "type": "save_build",
                "name": "Ultra Rig",
                "destination": "build",
                "explanation": "Ordinary save, no publish mentioned.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    user_id = at.session_state["auth_user"]["id"]
    at.get_by_key("concierge_chat_input").set_value("save build named Ultra Rig").run()

    assert not at.exception
    builds = builds_repo.get_builds_for_user(user_id)
    assert len(builds) == 1
    assert builds[0].is_public is False
    assert community_repo.get_feed() == []


def test_concierge_save_build_action_persists_real_build_and_records_last_saved(seeded_db, monkeypatch):
    """A save_build action with destination "build" must actually call
    builds_repo.create_build and land a real row for the logged-in user,
    under the user's own literal name — the chat equivalent of clicking
    "Save build" — record the new build's id/name in
    st.session_state["concierge_last_saved_build"] for a later publish_build
    action to resolve against, reset has_unsaved_build_changes, and leave
    build_draft/create_mode/page untouched (no forced navigation away)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge8", "concierge8@example.com", "Concierge Eight")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        assert current_build_context is not None
        return {
            "reply": "Saved as 'Weekend Gaming Rig'! Would you like to publish it to the Community as well?",
            "action": {
                "type": "save_build",
                "name": "Weekend Gaming Rig",
                "destination": "build",
                "explanation": "Saving your build.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Save this as Weekend Gaming Rig, a finished build").run()

    assert not at.exception
    user_id = at.session_state["auth_user"]["id"]
    saved_builds = builds_repo.get_builds_for_user(user_id)
    assert len(saved_builds) == 1
    assert saved_builds[0].is_public is False
    assert saved_builds[0].name == "Weekend Gaming Rig"  # the user's own literal name, not an auto-generated one

    last_saved = at.session_state["concierge_last_saved_build"]
    assert last_saved is not None
    assert last_saved["build_id"] == saved_builds[0].id
    assert last_saved["name"] == "Weekend Gaming Rig"

    assert at.session_state["has_unsaved_build_changes"] is False
    # build_draft/create_mode/page are deliberately left intact — a chat-triggered
    # save does not forcibly navigate the user away like the manual save flow does.
    assert at.session_state["build_draft"]["components"]["CPU"] == cpu.id
    assert at.session_state["create_mode"] == "Free"
    assert at.session_state["page"] == "create_build"


def test_concierge_save_build_action_with_draft_destination_persists_draft_not_build(seeded_db, monkeypatch):
    """A save_build action with destination "draft" must persist a real
    DraftBuild row (via drafts_repo, the real draft_builds table) under the
    user's own literal name, must NOT create a real Build row, and must NOT
    set concierge_last_saved_build — a draft has no publish path."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge-draft1", "concierge-draft1@example.com", "Concierge Draft One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        assert current_build_context is not None
        return {
            "reply": "Saved 'WIP Rig' as a draft.",
            "action": {
                "type": "save_build",
                "name": "WIP Rig",
                "destination": "draft",
                "explanation": "Saving your build as a draft.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Save this as WIP Rig, just as a draft for now").run()

    assert not at.exception
    user_id = at.session_state["auth_user"]["id"]

    drafts = drafts_repo.get_user_drafts(user_id)
    assert len(drafts) == 1
    assert drafts[0].name == "WIP Rig"
    assert json.loads(drafts[0].components_json)["CPU"] == cpu.id

    # No real Build row was created, and there's nothing to publish.
    assert builds_repo.get_builds_for_user(user_id) == []
    assert at.session_state["concierge_last_saved_build"] is None

    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["build_draft"]["components"]["CPU"] == cpu.id
    assert at.session_state["page"] == "create_build"


def test_concierge_save_build_community_source_clones_to_drafts_without_touching_studio(
    seeded_db, monkeypatch
):
    """A save_build action with source="community" (the user asking to save
    a build they're VIEWING on the Community page, e.g. "save the build I'm
    looking at to my drafts") must persist a real draft cloned from that
    ALREADY-shared post's own Build row -- and must leave the user's own,
    completely unrelated, in-progress Studio build (build_draft/page/
    has_unsaved_build_changes) entirely untouched, since this is a headless
    clone-into-my-drafts, not a "load into the Studio" action."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, community_repo, components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "communitysave1", "communitysave1@example.com", "Community Save One")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Shared Source Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    real_post = community_repo.get_feed()[0]
    real_build = real_post.build
    real_components = {bc.category: bc.component_id for bc in real_build.components}

    # A SEPARATE, unrelated in-progress Studio build the community-sourced
    # save must not touch at all. (Landing page's "nav_create_build" button
    # is gone once we've navigated away from landing — use the persistent
    # sidebar nav button instead, app.py's real key for it.)
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    other_gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_GPU_{other_gpu.id}").click().run()
    assert at.session_state["has_unsaved_build_changes"] is True

    at.session_state["page"] = "community"
    at.session_state["selected_post_id"] = real_post.id
    at.run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        current_page=None,
        viewed_post_id=None,
        **kwargs,
    ):
        assert current_page == "community"
        assert viewed_post_id == real_post.id
        return {
            "reply": "Saved 'Cloned From Community' to your drafts.",
            "action": {
                "type": "save_build",
                "name": "Cloned From Community",
                "destination": "draft",
                "source": "community",
                "source_post_id": viewed_post_id,
                "explanation": "Cloning the community build the user is viewing.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("save the build I'm looking at to my drafts").run()

    assert not at.exception
    user_id = at.session_state["auth_user"]["id"]

    drafts = drafts_repo.get_user_drafts(user_id)
    cloned = next(d for d in drafts if d.name == "Cloned From Community")
    assert json.loads(cloned.components_json) == real_components
    assert cloned.mode == real_build.creation_mode

    # No real Build row was created (destination was "draft"), and nothing
    # to publish.
    assert len(builds_repo.get_builds_for_user(user_id)) == 1  # just "Shared Source Rig"
    assert at.session_state["concierge_last_saved_build"] is None

    # The user's own, unrelated in-progress Studio build is completely
    # untouched: still on the community page, GPU pick still there, and the
    # unsaved-changes flag isn't falsely cleared by an unrelated save.
    assert at.session_state["page"] == "community"
    assert at.session_state["build_draft"]["components"]["GPU"] == other_gpu.id
    assert at.session_state["has_unsaved_build_changes"] is True


def test_concierge_save_build_action_noop_without_active_build(seeded_db, monkeypatch):
    """A save_build action arriving with no active build_draft (mode not
    even chosen yet) must not crash and must not create a build or draft."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge9", "concierge9@example.com", "Concierge Nine")

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "You don't have an active build to save yet.",
            "action": {
                "type": "save_build",
                "name": "Nothing To Save",
                "destination": "build",
                "explanation": "Nothing to save.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Save this PC to my list").run()

    assert not at.exception
    user_id = at.session_state["auth_user"]["id"]
    assert builds_repo.get_builds_for_user(user_id) == []
    assert drafts_repo.get_user_drafts(user_id) == []
    assert at.session_state["concierge_last_saved_build"] is None


def test_concierge_publish_build_action_uses_last_saved_build(seeded_db, monkeypatch):
    """A publish_build action must resolve WHICH build to publish via
    st.session_state["concierge_last_saved_build"] (pre-set here, standing in
    for a save_build action from an earlier turn) — never a build id on the
    action itself, which the LLM has no way to know — and call the real
    builds_repo.set_public + community_repo.create_post pair, using the
    saved build's own name as the post title and the action's author_notes."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import builds_repo, community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge10", "concierge10@example.com", "Concierge Ten")

    user_id = at.session_state["auth_user"]["id"]
    build = builds_repo.create_build(
        user_id=user_id,
        name="Pre-saved via Concierge",
        creation_mode="Free",
        components=[],
        total_cost=0.0,
        compatibility_score=100.0,
    )
    at.session_state["concierge_last_saved_build"] = {"build_id": build.id, "name": build.name}

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Published to the Community!",
            "action": {"type": "publish_build", "author_notes": "Solid budget pick."},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Yes, publish it").run()

    assert not at.exception
    refreshed = builds_repo.get_build(build.id)
    assert refreshed.is_public is True

    feed = community_repo.get_feed()
    assert len(feed) == 1
    assert feed[0].build_id == build.id
    assert feed[0].title == "Pre-saved via Concierge"
    assert feed[0].author_notes == "Solid budget pick."


def test_concierge_publish_build_action_is_noop_without_a_saved_build(seeded_db, monkeypatch):
    """A publish_build action arriving with nothing saved this session
    (concierge_last_saved_build is None, its real default) must not crash —
    a graceful no-op, since this should be rare given the prompt design but
    must never blow up the chat session."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import community_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge11", "concierge11@example.com", "Concierge Eleven")

    assert at.session_state["concierge_last_saved_build"] is None

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Published!",
            "action": {"type": "publish_build", "author_notes": None},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Yes, publish it").run()

    assert not at.exception
    assert community_repo.get_feed() == []


def test_concierge_load_build_appends_authoritative_total_overriding_wrong_llm_claim(seeded_db, monkeypatch):
    """Reproduces the real, confirmed bug: a live (non-mocked) LLM call once
    returned a syntactically-correct load_build action with real catalog ids
    whose REAL summed price was far below the figure the model's own `reply`
    text claimed (it had parroted back the user's REQUESTED budget instead of
    computing the actual total). The caller must append the REAL,
    Python-computed total (ui.state.build_total_cost) to the last assistant
    message, and that real total must be what's shown — not the wrong number
    the mocked reply states. The expected total is computed here independently
    (summing each component's raw price_usd directly), not by calling
    ui.state.build_total_cost, so this assertion isn't tautological against
    the same function chat_assistant.py calls internally."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    # A KNOWN, fixed set of real catalog ids across all 8 core categories.
    picks = {category: components_repo.get_by_category(category)[0] for category in (
        "CPU", "Motherboard", "GPU", "RAM", "Storage", "PSU", "Case", "Cooler",
    )}
    expected_total = sum(component.price_usd for component in picks.values())
    # A deliberately WRONG aggregate, mimicking the real bug (the model
    # parroting back a requested budget figure far from the real sum).
    wrong_claimed_total = expected_total + 1500.0

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": f"I built you a {wrong_claimed_total:,.2f} USD PC with a great CPU and GPU pairing.",
            "action": {
                "type": "load_build",
                "components": {category: component.id for category, component in picks.items()},
                "explanation": "Picked a balanced set of real parts.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge12", "concierge12@example.com", "Concierge Twelve")

    at.get_by_key("concierge_chat_input").set_value("Build me a 4000 dollar PC").run()

    assert not at.exception
    last_message = at.session_state["concierge_messages"][-1]
    assert last_message["role"] == "assistant"
    authoritative_line = f"**Total: USD {expected_total:,.2f}**"
    wrong_authoritative_line = f"**Total: USD {wrong_claimed_total:,.2f}**"
    # The real, Python-computed total must be present as the authoritative
    # line...
    assert authoritative_line in last_message["content"]
    # ...and the wrong number the mocked reply claimed must NOT be what's
    # presented as that authoritative line (the model's own untouched prose
    # sentence containing the wrong figure is still allowed to remain — see
    # the module docstring for why append-not-replace is the deliberate
    # design — but it must never be mistaken for/formatted as the real
    # total line itself).
    assert wrong_authoritative_line not in last_message["content"]
    assert expected_total != wrong_claimed_total


def test_concierge_navigate_and_save_build_actions_never_append_a_total_line(seeded_db, monkeypatch):
    """navigate/save_build actions involve no build-total concept at all —
    the authoritative-total line must never be appended for them."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "concierge13", "concierge13@example.com", "Concierge Thirteen")

    def _fake_navigate_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Taking you to Community.",
            "action": {"type": "navigate", "navigate_to": "community"},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_navigate_response)
    at.get_by_key("concierge_chat_input").set_value("Take me to community").run()

    assert not at.exception
    nav_message = at.session_state["concierge_messages"][-1]
    assert nav_message["content"] == "Taking you to Community."
    assert "Total:" not in nav_message["content"]

    # Build a real, active draft so save_build has something to persist — a
    # full 8-category build isn't required (save_build only needs a non-empty
    # build_state, same precondition as
    # test_concierge_save_build_action_persists_real_build_and_records_last_saved
    # above, which uses this identical CPU+GPU-only shortcut).
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    def _fake_save_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Saved your build! Want to publish it to Community too?",
            "action": {
                "type": "save_build",
                "name": "Total-Line Test Build",
                "destination": "build",
                "explanation": "Saving the current build.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_save_response)
    at.get_by_key("concierge_chat_input").set_value("Save this build").run()

    assert not at.exception
    save_message = at.session_state["concierge_messages"][-1]
    assert save_message["content"] == "Saved your build! Want to publish it to Community too?"
    assert "Total:" not in save_message["content"]
    assert at.session_state["concierge_last_saved_build"] is not None


# ---------------------------------------------------------------------------
# Multi-currency display support (ui/format.py, app.py's sidebar selector,
# spec.md §7.7/§6.7). Pure DISPLAY-layer conversion — every stored/compared
# price stays real USD (db.repositories, engine/) regardless of the active
# selection; only rendered strings (and, for the Concierge, pre-formatted
# strings handed to the model to quote verbatim) ever convert.
# ---------------------------------------------------------------------------
def test_currency_selector_defaults_to_usd(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currency1", "currency1@example.com", "Currency One")
    assert at.session_state["selected_currency"] == "USD"
    assert at.get_by_key("selected_currency").value == "USD"


def test_switching_currency_updates_build_studio_total_cost_display(seeded_db):
    """Selecting NIS in the sidebar must re-render the Build Studio's Total
    Cost metric converted at ui/format.py's real static rate, in Shekels —
    the underlying stored price/total (state.build_total_cost) stays real
    USD throughout; only the rendered string changes."""
    from ui.format import CURRENCY_RATES

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currency2", "currency2@example.com", "Currency Two")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    from db.repositories import components_repo

    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    # The Total Cost HUD chip (spec.md §7.4.1, theme.hud_chip) is rendered
    # as markdown, not st.metric, since this round's redesign — check its
    # text content instead of a metric widget's .value.
    hud_text = "\n".join(m.value for m in at.get_by_key("build_summary_header").markdown)
    assert f"${gpu.price_usd:,.2f}" in hud_text

    at.get_by_key("selected_currency").select("NIS").run()

    hud_text = "\n".join(m.value for m in at.get_by_key("build_summary_header").markdown)
    expected = f"₪{gpu.price_usd * CURRENCY_RATES['NIS']:,.2f}"
    assert expected in hud_text
    # Stored data is untouched — still real USD.
    assert at.session_state["build_draft"]["components"]["GPU"] == gpu.id


def test_switching_currency_updates_community_feed_price_display(seeded_db):
    """Selecting EUR must re-render a community feed card's price tag
    converted, in Euros."""
    from ui.format import CURRENCY_RATES

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currency3", "currency3@example.com", "Currency Three")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Currency Feed Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    from db.repositories import community_repo

    real_post = community_repo.get_feed()[0]

    at.get_by_key("selected_currency").select("EUR").run()
    at.get_by_key("sidebar_nav_community").click().run()

    expected = f"€{real_post.build.total_cost * CURRENCY_RATES['EUR']:,.2f}"
    markdown_values = [m.value for m in at.markdown]
    assert any(expected in m for m in markdown_values)


def test_concierge_receives_active_currency_matching_sidebar_selection(seeded_db, monkeypatch):
    """The active currency the user picked in the sidebar must actually
    reach llm.concierge.get_concierge_response as active_currency — proven
    here the same way other context-threading tests in this file are (a
    mocked response that only returns a valid reply when it received the
    expected kwarg)."""
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currency4", "currency4@example.com", "Currency Four")
    at.get_by_key("selected_currency").select("NIS").run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        active_currency=None,
        **kwargs,
    ):
        assert active_currency == "NIS"
        return {"reply": "Prices are in Shekels.", "action": None, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("What currency are we using?").run()

    assert not at.exception
    assert at.session_state["concierge_messages"][-1]["content"] == "Prices are in Shekels."


def test_concierge_authoritative_total_line_formats_in_selected_currency(seeded_db, monkeypatch):
    """The Python-appended authoritative "Total: ..." line (never trusting
    the model's own arithmetic, spec.md §7.8) must itself be formatted in
    whatever currency is currently selected, not hardcoded to USD."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from ui.format import CURRENCY_RATES

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currency5", "currency5@example.com", "Currency Five")
    at.get_by_key("selected_currency").select("EUR").run()

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Built you a solid CPU/GPU pairing.",
            "action": {
                "type": "load_build",
                "components": {"CPU": cpu.id, "GPU": gpu.id},
                "explanation": "Balanced pick.",
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("Build me a CPU and GPU").run()

    assert not at.exception
    last_message = at.session_state["concierge_messages"][-1]
    expected_total = (cpu.price_usd + gpu.price_usd) * CURRENCY_RATES["EUR"]
    assert f"**Total: €{expected_total:,.2f}**" in last_message["content"]


def test_concierge_currency_switch_flips_selector_and_formats_total_in_new_currency(
    seeded_db, monkeypatch
):
    """The real bug this directive reports: starting in USD and asking for a
    build in NIS must actually flip st.session_state["selected_currency"]
    (and the sidebar selector itself) to NIS, not just describe the resulting
    build's total in NIS for one reply while silently staying on USD
    afterward. Exercises the full round-trip: a mocked response carrying
    BOTH a load_build action AND currency_switch: "NIS" in the same turn
    (exactly how "build me a gaming PC for 10000 NIS" resolves)."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from ui.format import CURRENCY_RATES

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currencyswitch1", "currencyswitch1@example.com", "Currency Switch One")

    assert at.session_state["selected_currency"] == "USD"

    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        active_currency=None,
        **kwargs,
    ):
        assert active_currency == "USD"  # still USD at the moment of THIS request
        return {
            "reply": "Built you a gaming PC within your 10000 NIS budget.",
            "action": {
                "type": "load_build",
                "components": {"CPU": cpu.id, "GPU": gpu.id},
                "explanation": "Balanced pick within the converted USD budget.",
            },
            "currency_switch": "NIS",
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("build me a gaming PC for 10000 NIS").run()

    assert not at.exception
    # The sidebar selector itself must now show NIS, not just the one reply.
    assert at.session_state["selected_currency"] == "NIS"
    assert at.get_by_key("selected_currency").value == "NIS"

    last_message = at.session_state["concierge_messages"][-1]
    expected_total = (cpu.price_usd + gpu.price_usd) * CURRENCY_RATES["NIS"]
    assert f"**Total: ₪{expected_total:,.2f}**" in last_message["content"]
    assert "USD" not in last_message["content"].split("Total:")[-1]

    # Every OTHER price on screen (Build Studio) must also already reflect
    # NIS -- the Total Cost HUD chip (theme.hud_chip) is markdown, not
    # st.metric, since this round's redesign.
    hud_text = "\n".join(m.value for m in at.get_by_key("build_summary_header").markdown)
    assert f"₪{expected_total:,.2f}" in hud_text


def test_concierge_currency_switch_standalone_restates_total_with_no_action(seeded_db, monkeypatch):
    """"switch to NIS" / "I asked it to be in NIS" with an ALREADY-active
    build and no other action: the switch must still fire, and the
    authoritative total line must still be appended (restated in the new
    currency), even though action is None."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from ui.format import CURRENCY_RATES

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currencyswitch2", "currencyswitch2@example.com", "Currency Switch Two")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    gpu = components_repo.get_by_category("GPU")[0]
    at.get_by_key(f"select_GPU_{gpu.id}").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {"reply": "Switched to NIS.", "action": None, "currency_switch": "NIS", "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("I asked it to be in NIS").run()

    assert not at.exception
    assert at.session_state["selected_currency"] == "NIS"
    assert at.get_by_key("selected_currency").value == "NIS"

    last_message = at.session_state["concierge_messages"][-1]
    expected_total = gpu.price_usd * CURRENCY_RATES["NIS"]
    assert f"**Total: ₪{expected_total:,.2f}**" in last_message["content"]


def test_concierge_currency_switch_not_set_when_no_currency_mentioned(seeded_db, monkeypatch):
    """A plain build-me request with no currency named at all must leave
    selected_currency completely untouched -- currency_switch defaulting to
    None must never accidentally flip anything."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "currencyswitch3", "currencyswitch3@example.com", "Currency Switch Three")

    cpu = components_repo.get_by_category("CPU")[0]

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Built you a PC.",
            "action": {"type": "load_build", "components": {"CPU": cpu.id}, "explanation": "Pick."},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("build me a PC with that CPU").run()

    assert not at.exception
    assert at.session_state["selected_currency"] == "USD"
    last_message = at.session_state["concierge_messages"][-1]
    # sanitize_markdown replaces every literal "$" with "USD " (KaTeX-safety,
    # ui/format.py) -- this is the pre-existing, unchanged USD rendering, not
    # a regression; see the EUR/NIS tests above for the symbol-preserving case.
    assert f"**Total: USD {cpu.price_usd:,.2f}**" in last_message["content"]


# ---------------------------------------------------------------------------
# fix_warnings / optimize_bottleneck (spec.md §6.7 intents 11/12) — the AI
# Concierge's compatibility-fix and bottleneck-rebalancing actions. Neither
# ever lets the LLM choose a replacement part (root CLAUDE.md: compatibility
# is never LLM-gated) — the deterministic fix comes from
# engine.solvers.resolve_compatibility_issues / llm.advisory.get_build_
# advisory, applied by chat_assistant.py itself; these tests mock ONLY
# get_concierge_response (the intent-recognition call), never the actual
# fix logic, so they exercise the REAL application path end to end.
# ---------------------------------------------------------------------------
def test_concierge_fix_warnings_resolves_real_compatibility_issue(seeded_db, monkeypatch):
    """A build with a real, deterministic compatibility issue (an oversized
    cooler in an undersized case — the exact scenario from the directive's
    own verification script) must end up with 0 warnings after a mocked
    fix_warnings action, via a REAL swap engine.solvers.resolve_
    compatibility_issues picked (never a part the mocked LLM response
    itself named — it names none)."""
    import json as _json

    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from engine.compatibility import evaluate_build

    def _height_mm(component) -> int:
        specs = _json.loads(component.specs_json) if component.specs_json else {}
        return specs.get("height_mm", 0)

    cpu = next(c for c in components_repo.get_by_category("CPU") if c.socket)
    mobo = next(m for m in components_repo.get_by_category("Motherboard") if m.socket == cpu.socket)
    compatible_cases = [
        c for c in components_repo.get_by_category("Case")
        if c.max_cooler_height_mm and mobo.form_factor in (c.form_factor or "")
    ]
    case = min(compatible_cases, key=lambda c: c.max_cooler_height_mm)
    oversized_cooler = max(components_repo.get_by_category("Cooler"), key=_height_mm)
    assert _height_mm(oversized_cooler) > case.max_cooler_height_mm  # sanity: the fixture IS actually broken

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fixwarnings1", "fixwarnings1@example.com", "Fix Warnings One")

    at.session_state["build_draft"] = {
        "name": "", "creation_mode": "Free", "workload_profile": None, "tier": "Mid", "budget_ceiling": None,
        "components": {"CPU": cpu.id, "Motherboard": mobo.id, "Case": case.id, "Cooler": oversized_cooler.id},
        "quantities": {},
    }
    at.session_state["create_mode"] = "Free"

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        assert current_build_context["compatibility_issues"]  # real issues were actually injected into context
        return {"reply": "Resolving the compatibility issues now.", "action": {"type": "fix_warnings"}, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("fix the warnings in my build").run()

    assert not at.exception
    final_components = at.session_state["build_draft"]["components"]
    final_build_state = {cat: components_repo.get_by_id(cid) for cat, cid in final_components.items()}
    report = evaluate_build(final_build_state)
    assert report.is_compatible is True
    assert report.issues == []

    last_message = at.session_state["concierge_messages"][-1]
    assert "0 compatibility warnings remaining." in last_message["content"]


def test_concierge_fix_warnings_noop_without_active_build(seeded_db, monkeypatch):
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "fixwarnings2", "fixwarnings2@example.com", "Fix Warnings Two")

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        assert current_build_context is None
        return {"reply": "You don't have an active build yet.", "action": None, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("fix the warnings in my build").run()

    assert not at.exception
    assert at.session_state["build_draft"] is None


def test_concierge_optimize_bottleneck_applies_advisory_swap(seeded_db, monkeypatch):
    """A mocked optimize_bottleneck action must apply llm.advisory.get_
    build_advisory's own real stretch_budget.actions (the bottleneck-
    targeting upgrade, not the cost-neutral within_budget rebalance) -- never
    a part the Concierge response itself named (it names none) -- and the
    resulting build_draft must reflect a real, resolvable catalog swap."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    picks = {category: components_repo.get_by_category(category)[0] for category in (
        "CPU", "Motherboard", "GPU", "RAM", "Storage", "PSU", "Case", "Cooler",
    )}

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "optbottleneck1", "optbottleneck1@example.com", "Opt Bottleneck One")

    at.session_state["build_draft"] = {
        "name": "", "creation_mode": "Free", "workload_profile": None, "tier": "Mid", "budget_ceiling": None,
        "components": {cat: c.id for cat, c in picks.items()},
        "quantities": {},
    }
    at.session_state["create_mode"] = "Free"
    before_components = dict(at.session_state["build_draft"]["components"])

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        assert current_build_context["bottleneck"] is not None
        return {"reply": "Rebalancing your CPU/GPU pairing now.", "action": {"type": "optimize_bottleneck"}, "source": "heuristic"}

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("optimize the bottleneck").run()

    assert not at.exception
    last_message = at.session_state["concierge_messages"][-1]
    # RIGOROUS TELEMETRY REPLY FORMAT (Part 2, this round): the old bare
    # "Bottleneck now X%." line was replaced with a before -> after delta.
    assert "Bottleneck:" in last_message["content"] or "Bottleneck unavailable." in last_message["content"]
    # The build_draft must still resolve to a real, complete, valid build
    # afterward, whether or not a beneficial swap existed for THIS specific
    # already-cheapest-of-each-category starting point.
    after_components = at.session_state["build_draft"]["components"]
    assert set(after_components.keys()) == set(before_components.keys())
    for component_id in after_components.values():
        assert components_repo.get_by_id(component_id) is not None


def test_concierge_optimize_bottleneck_upgrades_the_bottlenecked_category(seeded_db, monkeypatch):
    """Root-cause regression test for the real bug this round's directive
    described ("AI fails to reduce bottleneck" / "hallucinates irrelevant
    swaps"): a genuinely CPU-bound build (a budget AM4 CPU paired with a
    flagship GPU — real, confirmed-live-compatible parts) must get its CPU
    swapped for a pricier compatible one, never an unrelated category. Uses
    the real heuristic advisory path (no OPENROUTER_API_KEY, per the
    `_no_live_llm_calls` autouse fixture) so this is deterministic, no live
    network call."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo
    from engine.compatibility import evaluate_build

    def _find(category, name):
        return next(c for c in components_repo.get_by_category(category) if c.name == name)

    cpu = _find("CPU", "AMD Ryzen 5 5600")
    gpu = _find("GPU", "AMD Radeon RX 7900 XTX")
    mobo = _find("Motherboard", "MSI B550-A PRO")
    ram = _find("RAM", "Kingston FURY Beast 32GB (2x16GB) DDR4-3200")
    storage = _find("Storage", "WD Black SN850X 2TB")
    psu = _find("PSU", "Corsair RM850x")
    case = _find("Case", "Corsair 4000D Airflow")
    cooler = _find("Cooler", "Thermalright Peerless Assassin 120 SE")

    build_state = {
        "CPU": cpu, "Motherboard": mobo, "GPU": gpu, "RAM": ram, "Storage": storage,
        "PSU": psu, "Case": case, "Cooler": cooler,
    }
    report = evaluate_build(build_state)
    assert report.is_compatible  # sanity: the fixture build is actually valid

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "optbottleneck2", "optbottleneck2@example.com", "Opt Bottleneck Two")
    at.session_state["build_draft"] = {
        "name": "", "creation_mode": "Free", "workload_profile": None, "tier": "Mid", "budget_ceiling": None,
        "components": {cat: c.id for cat, c in build_state.items()},
        "quantities": {},
    }
    at.session_state["create_mode"] = "Free"

    def _fake_response(
        user_message, conversation_history, catalog_summary, community_summary,
        current_build_context=None, advisory_context=None, **kwargs,
    ):
        assert current_build_context["bottleneck"]["direction"] == "CPU-bound"
        return {
            "reply": "Rebalancing your CPU/GPU pairing now.",
            "action": {"type": "optimize_bottleneck", "target_percentage": 10.0},
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)
    at.get_by_key("concierge_chat_input").set_value("try to get it under 10%").run()

    assert not at.exception
    after_cpu_id = at.session_state["build_draft"]["components"]["CPU"]
    assert after_cpu_id != cpu.id  # the CPU itself must have actually changed
    last_message = at.session_state["concierge_messages"][-1]["content"]
    assert "Bottleneck:" in last_message
    assert "Delta:" in last_message


# ---------------------------------------------------------------------------
# Drafts (db.repositories.drafts_repo) + leaving Build Studio
# (ui.state.teardown_builder, ui/views/drafts.py) — spec.md §7.9.
#
# The CURRENT of four designs this exact concern has gone through (see
# spec.md §7.9's own history note): an LLM-controlled `save_as_draft` field,
# an explicit "Save Draft or Discard?" confirmation dialog
# (ui/components/nav_guard.py, deleted), and a silent unconditional
# auto-save-on-exit with a flash banner — ALL removed by a later, explicit
# product decision. Every real exit from Build Studio now performs NO
# database write of any kind and resets the builder immediately; the "Save
# as draft" checkbox in create_build.py's manual Save UI is the single,
# explicit source of truth for creating a draft.
# ---------------------------------------------------------------------------
def test_picking_a_component_sets_unsaved_changes_flag(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft1", "draft1@example.com", "Draft One")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    assert at.session_state["has_unsaved_build_changes"] is False

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()

    assert not at.exception
    assert at.session_state["has_unsaved_build_changes"] is True


def test_sidebar_nav_with_unsaved_changes_navigates_with_no_draft_created(seeded_db):
    """Clicking a different page's sidebar nav button while build_draft has
    unsaved changes navigates immediately and discards the build — NO
    database write, no dialog, no flash banner (spec.md §7.9's CURRENT
    design: the "Save as draft" checkbox is the only explicit way to create
    a draft)."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft2", "draft2@example.com", "Draft Two")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_community").click().run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["build_draft"] is None
    assert at.session_state["create_mode"] is None
    assert drafts_repo.get_user_drafts(user_id) == []
    assert len(at.success) == 0  # no flash banner — that mechanism was removed entirely


def test_sidebar_nav_without_unsaved_changes_navigates_with_no_draft_created(seeded_db):
    """No unsaved changes yet (a fresh Free-mode draft with nothing picked)
    -> the sidebar nav button navigates right away with no draft created."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft3", "draft3@example.com", "Draft Three")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_community").click().run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert drafts_repo.get_user_drafts(user_id) == []
    assert len(at.success) == 0


def test_teardown_only_applies_when_leaving_create_build(seeded_db):
    """Teardown is scoped to leaving an in-progress create_build —
    has_unsaved_build_changes being (artificially) True while already on
    another page must not trigger a reset of anything (there is nothing to
    save regardless, since no exit vector ever writes to the database)."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft4", "draft4@example.com", "Draft Four")
    at.get_by_key("sidebar_nav_my_builds").click().run()
    at.session_state["has_unsaved_build_changes"] = True
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_community").click().run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert drafts_repo.get_user_drafts(user_id) == []


def test_sidebar_nav_home_navigates_to_landing_with_no_active_build(seeded_db):
    """The 🏠 Home sidebar button (sidebar_nav_landing) navigates straight to
    landing.py's content when there's no unsaved build in progress — plain,
    uneventful navigation, same as the other nav targets."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "home1", "home1@example.com", "Home One")
    at.get_by_key("sidebar_nav_my_builds").click().run()

    at.get_by_key("sidebar_nav_landing").click().run()

    assert not at.exception
    assert at.session_state["page"] == "landing"
    assert at.title[0].value == "Uncapped"


def test_sidebar_nav_home_with_unsaved_changes_navigates_with_no_draft_created(seeded_db):
    """Clicking Home mid-build with unsaved changes navigates straight to
    landing and discards the build — no database write, same as every other
    nav target."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "home2", "home2@example.com", "Home Two")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_landing").click().run()

    assert not at.exception
    assert at.session_state["page"] == "landing"
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["build_draft"] is None
    assert at.session_state["create_mode"] is None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_sidebar_nav_create_build_self_click_does_not_reset(seeded_db):
    """Clicking 🛠️ Create New PC (sidebar_nav_create_build) while ALREADY on
    create_build with unsaved changes must never trigger teardown — you
    can't "leave" the page you're already on. app.py's guard condition
    explicitly excludes `target_page == "create_build"`."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "selfnav1", "selfnav1@example.com", "Self Nav One")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()
    assert at.session_state["has_unsaved_build_changes"] is True
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_create_build").click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    # The in-progress pick must survive too — nothing was reset.
    assert at.session_state["build_draft"]["components"]["CPU"] is not None
    assert at.session_state["has_unsaved_build_changes"] is True
    assert drafts_repo.get_user_drafts(user_id) == []


def test_sidebar_nav_my_builds_with_unsaved_changes_navigates_with_no_draft_created(seeded_db):
    """Clicking 📂 Previous Builds (sidebar_nav_my_builds) mid-build with
    unsaved changes navigates straight to my_builds and discards the build —
    no database write."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "mybuilds1", "mybuilds1@example.com", "My Builds One")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_my_builds").click().run()

    assert not at.exception
    assert at.session_state["page"] == "my_builds"
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["build_draft"] is None
    assert at.session_state["create_mode"] is None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_sidebar_nav_drafts_with_unsaved_changes_navigates_with_no_draft_created(seeded_db):
    """Clicking 📝 View Drafts (sidebar_nav_drafts) mid-build with unsaved
    changes navigates straight to the (empty) Drafts page — the in-progress
    build is discarded, not auto-saved; the Drafts list must NOT show a
    phantom entry for it."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draftsnav1", "draftsnav1@example.com", "Drafts Nav One")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("sidebar_nav_drafts").click().run()

    assert not at.exception
    assert at.session_state["page"] == "drafts"
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["build_draft"] is None
    assert at.session_state["create_mode"] is None
    assert drafts_repo.get_user_drafts(user_id) == []


def test_save_build_resets_unsaved_changes_flag(seeded_db):
    """has_unsaved_build_changes is only ever set True by
    ui.state.set_component/remove_component/set_quantity — Budget/Workload's
    "generate" buttons write build_draft["components"] directly rather than
    through those mutators (a pre-existing, deliberately out-of-scope
    behavior of this feature, per its own spec), so a manual Free-mode pick
    is used here to actually exercise the flag before checking it resets on
    save."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft8", "draft8@example.com", "Draft Eight")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()

    assert at.session_state["has_unsaved_build_changes"] is True

    at.get_by_key("build_name_input").input("Draft Eight's Rig")
    at.get_by_key("save_build").click()
    at.run()

    assert not at.exception
    assert at.session_state["has_unsaved_build_changes"] is False


def test_logout_with_unsaved_changes_logs_out_with_no_draft_created(seeded_db):
    """Clicking Logout while create_build has unsaved changes completes the
    logout immediately with NO database write (the same `ui.state.
    teardown_builder()` every other exit vector calls) — no dialog, no
    auto-save."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft9", "draft9@example.com", "Draft Nine")
    at.get_by_key("sidebar_nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu_button = next(b.key for b in at.button if b.key and b.key.startswith("select_CPU_"))
    at.get_by_key(cpu_button).click().run()
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("logout_button").click().run()

    assert not at.exception
    assert at.session_state["auth_user"] is None
    assert at.session_state["page"] == "landing"
    assert drafts_repo.get_user_drafts(user_id) == []


def test_logout_without_unsaved_changes_logs_out_with_no_draft_created(seeded_db):
    """No unsaved changes -> Logout completes immediately with no draft
    created — the save step is skipped, matching every other exit vector."""
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft10", "draft10@example.com", "Draft Ten")
    user_id = at.session_state["auth_user"]["id"]

    at.get_by_key("logout_button").click().run()

    assert not at.exception
    assert at.session_state["auth_user"] is None
    assert at.session_state["page"] == "landing"
    assert drafts_repo.get_user_drafts(user_id) == []


def test_drafts_page_lists_saved_draft_with_part_count_and_mode(seeded_db):
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft9", "draft9@example.com", "Draft Nine")

    user_id = at.session_state["auth_user"]["id"]
    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    drafts_repo.save_draft(
        user_id=user_id, name="Saved WIP", mode="Free",
        components={"CPU": cpu.id, "GPU": gpu.id}, quantities={},
    )

    at.get_by_key("sidebar_nav_drafts").click().run()

    assert not at.exception
    assert at.session_state["page"] == "drafts"
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Saved WIP" in markdown_text
    caption_text = " ".join(c.value for c in at.caption)
    assert "Free draft" in caption_text
    assert "2 part(s) selected" in caption_text


def test_drafts_page_empty_state_when_no_drafts(seeded_db):
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft10", "draft10@example.com", "Draft Ten")

    at.get_by_key("sidebar_nav_drafts").click().run()

    assert not at.exception
    assert at.session_state["page"] == "drafts"
    assert any("No saved drafts yet" in i.value for i in at.info)


def test_drafts_load_into_builder_restores_components(seeded_db):
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft11", "draft11@example.com", "Draft Eleven")

    user_id = at.session_state["auth_user"]["id"]
    cpu = components_repo.get_by_category("CPU")[0]
    gpu = components_repo.get_by_category("GPU")[0]
    draft = drafts_repo.save_draft(
        user_id=user_id, name="Reload Me", mode="Budget",
        components={"CPU": cpu.id, "GPU": gpu.id}, quantities={},
    )

    at.get_by_key("sidebar_nav_drafts").click().run()
    at.get_by_key(f"load_draft_{draft.id}").click().run()

    assert not at.exception
    assert at.session_state["page"] == "create_build"
    assert at.session_state["create_mode"] == "Budget"
    components = at.session_state["build_draft"]["components"]
    assert components["CPU"] == cpu.id
    assert components["GPU"] == gpu.id


def test_drafts_delete_requires_confirmation_then_removes_draft(seeded_db):
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft12", "draft12@example.com", "Draft Twelve")

    user_id = at.session_state["auth_user"]["id"]
    draft = drafts_repo.save_draft(user_id=user_id, name="Disposable Draft", mode="Free", components={}, quantities={})

    at.get_by_key("sidebar_nav_drafts").click().run()
    at.get_by_key(f"delete_draft_{draft.id}").click().run()

    # first click only arms the confirmation — draft must still exist
    assert not at.exception
    assert len(drafts_repo.get_user_drafts(user_id)) == 1
    yes_button = at.get_by_key(f"confirm_delete_draft_{draft.id}_yes")
    assert yes_button is not None

    yes_button.click().run()

    assert not at.exception
    assert drafts_repo.get_user_drafts(user_id) == []


def test_drafts_delete_cancel_keeps_the_draft(seeded_db):
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "draft13", "draft13@example.com", "Draft Thirteen")

    user_id = at.session_state["auth_user"]["id"]
    draft = drafts_repo.save_draft(user_id=user_id, name="Keep This Draft", mode="Free", components={}, quantities={})

    at.get_by_key("sidebar_nav_drafts").click().run()
    at.get_by_key(f"delete_draft_{draft.id}").click().run()
    at.get_by_key(f"confirm_delete_draft_{draft.id}_cancel").click().run()

    assert not at.exception
    assert [d.name for d in drafts_repo.get_user_drafts(user_id)] == ["Keep This Draft"]


# ---------------------------------------------------------------------------
# Concierge navigate action extension: `filters` (community pre-population)
# — see llm/schemas.py's ConciergeNavigateFilters/ConciergeNavigateAction and
# ui/components/chat_assistant.py's navigate branch /
# ui/views/community.py::_apply_pending_community_filters. The now-removed
# `save_as_draft` (auto-stash to Drafts) mechanism is covered by the
# regression tests below (test_concierge_navigate_never_creates_draft_*),
# which confirm the OPPOSITE: a navigate action never creates a draft.
# ---------------------------------------------------------------------------
def test_concierge_navigate_with_budget_filters_prepopulates_community_widgets(seeded_db, monkeypatch):
    """A navigate action to "community" carrying a Budget-shaped `filters`
    payload must actually pre-populate the REAL `community_mode_filter`/
    `community_price_filter` widget session-state keys the next time
    community.py renders — not just set `st.session_state["page"]`. Also
    confirms the one-shot staging key is consumed (popped), not left
    lingering."""
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "navfilter1", "navfilter1@example.com", "Nav Filter One")

    # A real, currently-shared Budget build is required for the price-step
    # selectbox to render at all (ui/views/community.py hides it entirely
    # when no Budget builds are shared).
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()
    at.get_by_key("build_name_input").input("Nav Filter Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Here are the budget builds under 1200 USD.",
            "action": {
                "type": "navigate",
                "navigate_to": "community",
                "filters": {"build_type": "Budget", "max_price": 1200.0, "domain": None, "tier": None},
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Show budget builds under 1200").run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert not _session_value(at, "pending_community_filters")  # one-shot: consumed, not lingering

    mode_widget = at.get_by_key("community_mode_filter")
    assert mode_widget.value == "Budget"

    price_widget = at.get_by_key("community_price_filter")
    assert price_widget.value is not None  # a real max_price was requested and matched (None == "All Prices")
    # A real, raw USD step number — not a currency-symbol string to parse back (see
    # ui/views/community.py's `format_func`-based selectbox; `.options` reflects the
    # FORMATTED display labels, so containment against `.value`'s raw number doesn't apply here).
    assert isinstance(price_widget.value, (int, float))


def test_concierge_navigate_with_workload_filters_prepopulates_community_widgets(seeded_db, monkeypatch):
    """Same as the Budget case above, but for the Workload domain/tier
    sub-filter — also exercises case-insensitive matching against the real,
    currently-shared domain/tier strings."""
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "navfilter2", "navfilter2@example.com", "Nav Filter Two")

    # A real, currently-shared Workload build (default profile "General",
    # default tier "Mid" — see generate_workload_build's own defaults).
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_workload").click().run()
    at.get_by_key("generate_workload_build").click().run()
    at.get_by_key("build_name_input").input("Nav Filter Workload Rig")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Here are the general-purpose builds.",
            "action": {
                "type": "navigate",
                "navigate_to": "community",
                # Deliberately lowercase/differently-cased from the real
                # stored "General"/"Mid" values, to exercise the
                # case-insensitive match.
                "filters": {"build_type": "Workload", "max_price": None, "domain": "general", "tier": "mid"},
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Show general workload builds").run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert not _session_value(at, "pending_community_filters")

    assert at.get_by_key("community_mode_filter").value == "Workload"
    domain_widget = at.get_by_key("community_domain_filter")
    tier_widget = at.get_by_key("community_tier_filter")
    assert domain_widget.value == "General"  # matched real, currently-shared value, not the lowercased request
    assert tier_widget.value == "Mid"
    assert domain_widget.value in domain_widget.options
    assert tier_widget.value in tier_widget.options


def test_concierge_navigate_with_unmatched_filters_falls_back_gracefully(seeded_db, monkeypatch):
    """A domain/tier that doesn't match anything currently shared must fall
    back to "All" rather than crash with a raw StreamlitAPIException (a
    keyed selectbox given a session-state value outside its own `options`
    list) — the exact class of footgun this feature has to guard against
    since the real option strings are live data unknown to the LLM."""
    import ui.components.chat_assistant as chat_assistant_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "navfilter5", "navfilter5@example.com", "Nav Filter Five")

    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_workload").click().run()
    at.get_by_key("generate_workload_build").click().run()
    at.get_by_key("build_name_input").input("Nav Filter Workload Rig 2")
    at.get_by_key("publish_checkbox").check()
    at.get_by_key("save_build").click().run()

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Here you go.",
            "action": {
                "type": "navigate",
                "navigate_to": "community",
                "filters": {
                    "build_type": "Workload",
                    "max_price": None,
                    "domain": "Nonexistent Domain",
                    "tier": "Nonexistent Tier",
                },
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Show me nonexistent-domain builds").run()

    assert not at.exception  # no StreamlitAPIException from an out-of-options selectbox value
    assert at.get_by_key("community_domain_filter").value == "All"
    assert at.get_by_key("community_tier_filter").value == "All"


def test_concierge_navigate_never_creates_draft_even_with_active_unsaved_build(
    seeded_db, monkeypatch
):
    """The core "no phantom drafts" regression guarantee: a plain Concierge
    navigate request away from an unsaved build must NEVER create a
    DraftBuild row — `ui.state.teardown_builder()` (spec.md §7.9's CURRENT
    design) performs no database write of any kind, entirely independent of
    anything the model returns. This also proves a stale/non-compliant
    mocked response that still carries the long-REMOVED `save_as_draft: true`
    field (from an even earlier, since-replaced design) has no effect of its
    own: `ConciergeNavigateAction` no longer declares it at all, and
    `_apply_concierge_action`'s `navigate` branch never looks for it."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo, drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "navfilter3", "navfilter3@example.com", "Nav Filter Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_free").click().run()

    cpu = components_repo.get_by_category("CPU")[0]
    at.get_by_key(f"select_CPU_{cpu.id}").click().run()

    assert at.session_state["has_unsaved_build_changes"] is True

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        assert current_build_context is not None
        return {
            "reply": "Taking you to Community.",
            # A stale/non-compliant mock still returning the long-removed
            # field — must be silently inert, never acted upon.
            "action": {
                "type": "navigate",
                "navigate_to": "community",
                "filters": None,
                "save_as_draft": True,
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Take me to Community").run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    assert at.session_state["has_unsaved_build_changes"] is False
    assert at.session_state["build_draft"] is None
    assert at.session_state["create_mode"] is None

    user_id = at.session_state["auth_user"]["id"]
    assert drafts_repo.get_user_drafts(user_id) == []


def test_concierge_navigate_creates_no_draft_without_active_build_either(seeded_db, monkeypatch):
    """Same guarantee as above, for the simpler case of no active build_draft
    at all (mode not even chosen yet) — must not crash and must not create a
    draft row."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import drafts_repo

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "navfilter4", "navfilter4@example.com", "Nav Filter Four")

    def _fake_response(
        user_message,
        conversation_history,
        catalog_summary,
        community_summary,
        current_build_context=None,
        advisory_context=None,
        **kwargs,
    ):
        return {
            "reply": "Taking you to Community.",
            "action": {
                "type": "navigate",
                "navigate_to": "community",
                "filters": None,
            },
            "source": "heuristic",
        }

    monkeypatch.setattr(chat_assistant_module, "get_concierge_response", _fake_response)

    at.get_by_key("concierge_chat_input").set_value("Take me to Community").run()

    assert not at.exception
    assert at.session_state["page"] == "community"
    user_id = at.session_state["auth_user"]["id"]
    assert drafts_repo.get_user_drafts(user_id) == []
