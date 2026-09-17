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
    """AI Build Advisory: the "✨ Get AI Analysis & Upgrade Path" button
    (separate, click-gated feature from the auto-run synergy/bottleneck
    read) must exist once a build has at least two components, and clicking
    it must populate `advisory_cache` with a non-empty within_budget and
    stretch_budget suggestion list. `_no_live_llm_calls` (autouse) keeps
    this on the network-free heuristic path, same as the existing
    auto-analysis fallback test above."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "priya", "priya@example.com", "Priya Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("apply_budget_generate").click().run()

    advisory_button = at.get_by_key("get_advisory")
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

    at.get_by_key("get_advisory").click().run()
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

    at.get_by_key("get_advisory").click().run()
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

    Ceiling chosen with generous headroom above the solver's own spend (not
    just distinct from other tests' ceilings): the RAM/Storage quantity
    stepper is now budget-aware too (`ui.state.resolve_effective_quantity_limit`),
    and this stretch action deliberately pushes RAM quantity to 2 — a tight
    ceiling where the solver already spends ~99% of it would leave no
    budget-effective headroom for that 2nd unit, and the part-picker's own
    pre-clamp (correctly, generically) would immediately clamp the
    just-applied quantity back down to 1 on the very next render, which is
    a real product interaction but not what this test is exercising."""
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply3", "apply3@example.com", "Apply Three")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("budget_ceiling_input").set_value(2000.0).run()
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

    at.get_by_key("get_advisory").click().run()
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

    Ceiling chosen with generous headroom above the solver's own spend, not
    just distinct from other tests' ceilings — see
    test_apply_stretch_upgrade_locks_per_build's docstring: the RAM/Storage
    quantity stepper is now budget-aware, and a tight ceiling would let its
    own pre-clamp immediately re-clamp this test's just-applied qty=2 back
    down to 1 on the next render, which isn't what this test is about."""
    import ui.views.create_build as create_build_module

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "apply5", "apply5@example.com", "Apply Five")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    # Distinct ceiling so this cache key can't collide with another test's
    # default-$1500 budget build (see test_apply_stretch_upgrade_locks_per_build's
    # docstring for why that matters given the shared-default stretch_applied_keys set).
    at.get_by_key("budget_ceiling_input").set_value(5000.0).run()
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

    at.get_by_key("get_advisory").click().run()
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

    at.get_by_key("get_advisory").click().run()
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

    at.get_by_key("get_advisory").click().run()
    assert not at.exception

    assert at.get_by_key("btn_apply_in_budget").disabled is True
    assert at.get_by_key("btn_apply_stretch").disabled is True

    # clicking a disabled button is a no-op in AppTest terms (nothing to
    # click), so just re-confirm the build/components are untouched.
    components_before = dict(at.session_state["build_draft"]["components"])
    at.run()
    assert at.session_state["build_draft"]["components"] == components_before


def test_reset_all_fields_clears_build_and_budget_ceiling(seeded_db):
    """Studio redesign: the 'Sort candidates by' control was replaced by a
    single 'Reset All Fields' button that empties every slot and restores
    the budget ceiling to its clean default in one click. Budget ceiling
    commits are now apply-on-click (not live-synced as you type), so a
    fresh draft's budget_ceiling is None until "Apply budget & generate
    build" is clicked again — it's the *widget's displayed value* that
    resets to the clean default immediately, not the committed one."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()
    _register(at, "ulysses", "ulysses@example.com", "Ulysses Row")
    at.get_by_key("nav_create_build").click().run()
    at.get_by_key("mode_budget").click().run()
    at.get_by_key("budget_ceiling_input").set_value(900.0).run()
    at.get_by_key("apply_budget_generate").click().run()

    assert at.session_state["build_draft"]["components"]  # non-empty before reset
    assert at.session_state["build_draft"]["budget_ceiling"] == 900.0

    at.get_by_key("reset_all_fields").click().run()

    assert not at.exception
    assert at.session_state["build_draft"]["components"] == {}
    assert at.session_state["build_draft"]["budget_ceiling"] is None  # nothing committed yet on the fresh draft
    assert at.get_by_key("budget_ceiling_input").value == 1500.0  # widget redisplays the clean default


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

    def _fake_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None):
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


def test_concierge_load_build_action_applies_immediately_no_confirmation(seeded_db, monkeypatch):
    """A load_build action from a turn must land in build_draft/create_mode/
    page on the very same rerun the reply arrives — no button, no
    intermediate click, and no confirmation-button key exists anymore."""
    import ui.components.chat_assistant as chat_assistant_module
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]

    def _fake_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None):
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

    def _fake_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None):
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

    def _fake_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None):
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

    def _fake_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None):
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
