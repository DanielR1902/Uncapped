"""Phase 2 verification: compatibility rules, scoring, and the three build solvers.

compatibility.py / scoring.py tests build Component instances directly in memory
(no DB session, no seeded catalog) — per engine/CLAUDE.md, this package must be
unit-testable with no external services. solvers.py tests need the real catalog
(they read via db.repositories.components_repo), so they use a temp seeded DB.
"""
from __future__ import annotations

import json

import pytest

from db.models import Component
from engine import compatibility, scoring, solvers


def make_component(category: str, *, id: int = 1, name: str | None = None, price_usd: float = 100.0,
                    specs: dict | None = None, **kwargs) -> Component:
    return Component(
        id=id,
        category=category,
        name=name or f"Test {category} {id}",
        brand="TestBrand",
        price_usd=price_usd,
        specs_json=json.dumps(specs or {}),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# compatibility.py — socket mismatch
# ---------------------------------------------------------------------------
def test_socket_mismatch_fails():
    cpu = make_component("CPU", id=1, socket="LGA1700", tdp_watts=65, benchmark_score=60)
    motherboard = make_component("Motherboard", id=2, socket="AM5", ram_type="DDR5", form_factor="ATX")

    result = compatibility.check_cpu_motherboard_socket({"CPU": cpu, "Motherboard": motherboard})
    assert result is not None
    assert result.passed is False
    assert "Socket mismatch" in result.message

    report = compatibility.evaluate_build({"CPU": cpu, "Motherboard": motherboard})
    assert report.is_compatible is False
    assert any("Socket mismatch" in issue for issue in report.issues)


def test_socket_match_passes():
    cpu = make_component("CPU", id=1, socket="AM5", tdp_watts=65, benchmark_score=60)
    motherboard = make_component("Motherboard", id=2, socket="AM5", ram_type="DDR5", form_factor="ATX")

    result = compatibility.check_cpu_motherboard_socket({"CPU": cpu, "Motherboard": motherboard})
    assert result is not None
    assert result.passed is True


def test_socket_rule_not_applicable_when_incomplete():
    cpu = make_component("CPU", id=1, socket="AM5")
    assert compatibility.check_cpu_motherboard_socket({"CPU": cpu}) is None


# ---------------------------------------------------------------------------
# compatibility.py — RAM type / cooler socket / form factors
# ---------------------------------------------------------------------------
def test_ram_type_mismatch_fails():
    motherboard = make_component("Motherboard", id=1, ram_type="DDR4")
    ram = make_component("RAM", id=2, ram_type="DDR5", capacity_gb=32)
    result = compatibility.check_ram_motherboard_type({"Motherboard": motherboard, "RAM": ram})
    assert result.passed is False


def test_cooler_socket_support_fails_when_unlisted():
    cpu = make_component("CPU", id=1, socket="LGA1851")
    cooler = make_component("Cooler", id=2, socket="AM4,AM5,LGA1700")
    result = compatibility.check_cooler_socket_support({"CPU": cpu, "Cooler": cooler})
    assert result.passed is False


def test_case_motherboard_form_factor_fails_for_oversized_board():
    case = make_component("Case", id=1, form_factor="mATX,ITX")
    motherboard = make_component("Motherboard", id=2, form_factor="ATX")
    result = compatibility.check_case_motherboard_form_factor({"Case": case, "Motherboard": motherboard})
    assert result.passed is False


def test_case_psu_form_factor_fails_for_sfx_only_case():
    case = make_component("Case", id=1, psu_form_factor_support="SFX")
    psu = make_component("PSU", id=2, form_factor="ATX")
    result = compatibility.check_case_psu_form_factor({"Case": case, "PSU": psu})
    assert result.passed is False


# ---------------------------------------------------------------------------
# compatibility.py — dimension clearance
# ---------------------------------------------------------------------------
def test_gpu_too_long_for_case_fails():
    case = make_component("Case", id=1, max_gpu_length_mm=300)
    gpu = make_component("GPU", id=2, specs={"length_mm": 336})
    result = compatibility.check_gpu_case_clearance({"Case": case, "GPU": gpu})
    assert result.passed is False
    assert "too long" in result.message


def test_gpu_fits_case_passes():
    case = make_component("Case", id=1, max_gpu_length_mm=400)
    gpu = make_component("GPU", id=2, specs={"length_mm": 336})
    result = compatibility.check_gpu_case_clearance({"Case": case, "GPU": gpu})
    assert result.passed is True


def test_air_cooler_too_tall_for_case_fails():
    case = make_component("Case", id=1, max_cooler_height_mm=160)
    cooler = make_component("Cooler", id=2, specs={"cooler_type": "Air", "height_mm": 165})
    result = compatibility.check_cooler_case_clearance({"Case": case, "Cooler": cooler})
    assert result.passed is False
    assert "too tall" in result.message


def test_air_cooler_fits_case_passes():
    case = make_component("Case", id=1, max_cooler_height_mm=170)
    cooler = make_component("Cooler", id=2, specs={"cooler_type": "Air", "height_mm": 165})
    result = compatibility.check_cooler_case_clearance({"Case": case, "Cooler": cooler})
    assert result.passed is True


def test_aio_radiator_too_large_for_case_fails():
    case = make_component("Case", id=1, specs={"max_radiator_mm": 240})
    cooler = make_component("Cooler", id=2, specs={"cooler_type": "AIO", "radiator_size_mm": 360})
    result = compatibility.check_cooler_case_clearance({"Case": case, "Cooler": cooler})
    assert result.passed is False
    assert "Radiator too large" in result.message


def test_aio_radiator_fits_case_passes():
    case = make_component("Case", id=1, specs={"max_radiator_mm": 360})
    cooler = make_component("Cooler", id=2, specs={"cooler_type": "AIO", "radiator_size_mm": 280})
    result = compatibility.check_cooler_case_clearance({"Case": case, "Cooler": cooler})
    assert result.passed is True


# ---------------------------------------------------------------------------
# compatibility.py — PSU headroom
# ---------------------------------------------------------------------------
def test_psu_headroom_failure_high_tdp_on_450w():
    cpu = make_component("CPU", id=1, tdp_watts=125, benchmark_score=97)
    gpu = make_component("GPU", id=2, tdp_watts=450, benchmark_score=100, specs={"length_mm": 336})
    psu = make_component("PSU", id=3, wattage_capacity=450)

    result = compatibility.check_psu_headroom({"CPU": cpu, "GPU": gpu, "PSU": psu})
    assert result is not None
    assert result.passed is False
    assert "under-provisioned" in result.message


def test_psu_headroom_passes_with_ample_wattage():
    cpu = make_component("CPU", id=1, tdp_watts=125, benchmark_score=97)
    gpu = make_component("GPU", id=2, tdp_watts=450, benchmark_score=100, specs={"length_mm": 336})
    psu = make_component("PSU", id=3, wattage_capacity=1000)

    result = compatibility.check_psu_headroom({"CPU": cpu, "GPU": gpu, "PSU": psu})
    assert result.passed is True


def test_psu_headroom_uses_named_constants():
    cpu = make_component("CPU", id=1, tdp_watts=100, benchmark_score=80)
    psu_boundary = (100 + 0 + compatibility.SYSTEM_BASELINE_WATTS) * compatibility.PSU_HEADROOM_MULTIPLIER
    psu = make_component("PSU", id=2, wattage_capacity=int(psu_boundary))
    result = compatibility.check_psu_headroom({"CPU": cpu, "PSU": psu})
    assert result.passed is True  # exactly at the boundary (>=) should pass


# ---------------------------------------------------------------------------
# compatibility.py — full-build audit
# ---------------------------------------------------------------------------
def test_evaluate_build_fully_compatible_scores_100():
    build_state = {
        "CPU": make_component("CPU", id=1, socket="AM5", tdp_watts=105, benchmark_score=80),
        "Motherboard": make_component("Motherboard", id=2, socket="AM5", ram_type="DDR5", form_factor="ATX"),
        "RAM": make_component("RAM", id=3, ram_type="DDR5", capacity_gb=32),
        "GPU": make_component("GPU", id=4, tdp_watts=220, benchmark_score=74, specs={"length_mm": 267}),
        "PSU": make_component("PSU", id=5, wattage_capacity=750, form_factor="ATX"),
        "Case": make_component(
            "Case", id=6, form_factor="ATX,mATX,ITX", max_gpu_length_mm=380, max_cooler_height_mm=170,
            psu_form_factor_support="ATX",
        ),
        "Cooler": make_component("Cooler", id=7, socket="AM4,AM5,LGA1700", specs={"cooler_type": "Air", "height_mm": 160}),
    }
    report = compatibility.evaluate_build(build_state)
    assert report.is_compatible is True
    assert report.compatibility_score == 100.0
    assert report.issues == []


def test_evaluate_build_empty_state_scores_100():
    report = compatibility.evaluate_build({})
    assert report.is_compatible is True
    assert report.compatibility_score == 100.0


# ---------------------------------------------------------------------------
# scoring.py
# ---------------------------------------------------------------------------
def test_bottleneck_baseline_cpu_bound():
    build_state = {
        "CPU": make_component("CPU", id=1, benchmark_score=50),
        "GPU": make_component("GPU", id=2, benchmark_score=100),
    }
    pct, direction = scoring.bottleneck_percentage_baseline(build_state)
    assert pct == pytest.approx(50.0)
    assert direction == "CPU-bound"


def test_bottleneck_baseline_gpu_bound():
    build_state = {
        "CPU": make_component("CPU", id=1, benchmark_score=100),
        "GPU": make_component("GPU", id=2, benchmark_score=50),
    }
    pct, direction = scoring.bottleneck_percentage_baseline(build_state)
    assert pct == pytest.approx(50.0)
    assert direction == "GPU-bound"


def test_bottleneck_baseline_balanced_when_equal():
    build_state = {
        "CPU": make_component("CPU", id=1, benchmark_score=80),
        "GPU": make_component("GPU", id=2, benchmark_score=80),
    }
    pct, direction = scoring.bottleneck_percentage_baseline(build_state)
    assert pct == 0.0
    assert direction == "Balanced"


def test_bottleneck_baseline_missing_component_is_balanced_zero():
    pct, direction = scoring.bottleneck_percentage_baseline({"CPU": make_component("CPU", id=1, benchmark_score=80)})
    assert pct == 0.0
    assert direction == "Balanced"


def test_value_index_prefers_cheaper_equally_compatible_option():
    cheap_gpu = make_component("GPU", id=1, price_usd=300, tdp_watts=150, specs={"length_mm": 200})
    expensive_gpu = make_component("GPU", id=2, price_usd=900, tdp_watts=150, specs={"length_mm": 200})
    candidates = [cheap_gpu, expensive_gpu]

    cheap_value = scoring.value_index(cheap_gpu, candidates, {})
    expensive_value = scoring.value_index(expensive_gpu, candidates, {})
    assert cheap_value > expensive_value


def test_value_index_empty_candidates_is_zero():
    gpu = make_component("GPU", id=1, price_usd=300)
    assert scoring.value_index(gpu, [], {}) == 0.0


# ---------------------------------------------------------------------------
# solvers.py — needs a real seeded catalog
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded_db(tmp_path):
    from db import database
    from db.seed import run_seed

    db_path = tmp_path / "test_engine.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    run_seed()
    yield
    database.get_engine().dispose()


def test_budget_solver_stays_within_ceiling(seeded_db):
    ceiling = 2000.0
    build = solvers.initialize_budget_build(ceiling)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_budget_solver_converges_toward_ceiling_not_just_cheapest(seeded_db):
    ceiling = 2000.0
    build = solvers.initialize_budget_build(ceiling)
    total_cost = sum(c.price_usd for c in build.values())
    # a fully "max-cost-converged" build should spend a substantial fraction of
    # the ceiling, not degenerate to the cheapest possible parts.
    assert total_cost >= ceiling * 0.5


def test_budget_solver_degrades_gracefully_on_unrealistic_ceiling(seeded_db):
    # ceiling far below the true minimum full-build cost: every category must
    # still get filled (never left empty), even though this necessarily
    # exceeds the ceiling.
    build = solvers.initialize_budget_build(50.0)
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)


def test_on_user_pins_component_respects_pin_and_budget(seeded_db):
    from db.repositories import components_repo

    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    # ceiling must comfortably exceed the pinned part's own price, or "stay
    # under budget" is unsatisfiable no matter what the solver does with the
    # rest — that's a UI-level pinning constraint, not something this solver
    # can fix retroactively.
    ceiling = priciest_gpu.price_usd + 800.0

    build = solvers.on_user_pins_component({}, "GPU", priciest_gpu, ceiling)
    assert build["GPU"].id == priciest_gpu.id
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert compatibility.evaluate_build(build).is_compatible is True


def test_budget_solver_downgrades_pinned_gpu_when_infeasible(seeded_db):
    """A user who pins an expensive part first, then sets a ceiling too tight
    to ever complete the other 7 slots, must still get a complete, in-budget
    build — the solver auto-downgrades the pin itself (highest tier that still
    fits) rather than refusing outright or silently exceeding the ceiling."""
    from db.repositories import components_repo

    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    # deliberately leaves only $10 for the other 7 categories combined, as pinned
    ceiling = priciest_gpu.price_usd + 10.0

    build = solvers.initialize_budget_build(ceiling, seed_selection={"GPU": priciest_gpu})

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert build["GPU"].id != priciest_gpu.id  # the pin itself had to be stepped down
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_budget_solver_downgrades_pin_when_pin_alone_exceeds_ceiling(seeded_db):
    from db.repositories import components_repo

    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    ceiling = priciest_gpu.price_usd - 1.0  # the pin ALONE already blows the ceiling

    build = solvers.initialize_budget_build(ceiling, seed_selection={"GPU": priciest_gpu})

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert build["GPU"].id != priciest_gpu.id
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)


def test_budget_solver_never_exceeds_ceiling_with_expensive_pinned_component(seeded_db):
    """The core regression guard this task asked for: pin a pricey (but not
    the single most extreme) component, use a ceiling that comfortably fits
    it plus the rest, and confirm the final build both respects the ceiling
    and leaves the pin untouched."""
    from db.repositories import components_repo

    gpus_by_price = sorted(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    pinned_gpu = gpus_by_price[-3]  # pricey, not the single most extreme
    ceiling = pinned_gpu.price_usd + 800.0

    build = solvers.initialize_budget_build(ceiling, seed_selection={"GPU": pinned_gpu})

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert build["GPU"].id == pinned_gpu.id  # pinned component must remain immutable
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_on_user_pins_component_downgrades_when_infeasible(seeded_db):
    """The same auto-downgrade behavior applies through the
    on_user_pins_component entry point, not just initialize_budget_build —
    both route through the same _greedy_fill, so this should already hold,
    but it's worth locking in explicitly since it's a distinct public entry
    point."""
    from db.repositories import components_repo

    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    ceiling = priciest_gpu.price_usd + 10.0

    build = solvers.on_user_pins_component({}, "GPU", priciest_gpu, ceiling)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert build["GPU"].id != priciest_gpu.id
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)


def test_cheapest_fill_cost_skips_already_present_categories(seeded_db):
    from db.repositories import components_repo

    cpu = components_repo.get_by_category("CPU")[0]
    build_state = {"CPU": cpu}
    # CPU is already present, so it must not be double-counted in the fill cost
    cost_including_cpu_category = solvers.cheapest_fill_cost(build_state, ["CPU", "RAM"])
    cost_ram_only = solvers.cheapest_fill_cost(build_state, ["RAM"])
    assert cost_including_cpu_category == cost_ram_only


def test_budget_solver_produces_complete_build_at_1500_from_empty(seeded_db):
    """Explicit check at the exact figure called out in the verification
    requirement: an empty build, $1,500 ceiling, "Generate starting build"
    equivalent (initialize_budget_build with no seed) must produce a
    complete, fully compatible 8-part build at or under that ceiling."""
    ceiling = 1500.0
    build = solvers.initialize_budget_build(ceiling)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_reserve_threshold_never_allows_a_deadlock(seeded_db):
    """The same math ui/components/part_picker.py now delegates to
    (engine.solvers.cheapest_fill_cost) for its Minimum Reserve Threshold:
    on an empty $700 budget build, picking the most expensive GPU that still
    passes the threshold must leave a mathematically completable remainder —
    i.e. the picker's "safe to select" boundary can never actually deadlock
    the other 7 slots."""
    from db.repositories import components_repo

    ceiling = 700.0
    other_categories = [c for c in solvers.CATEGORY_ORDER if c != "GPU"]
    min_reserve = solvers.cheapest_fill_cost({}, other_categories)
    max_gpu_cost = ceiling - min_reserve

    affordable_gpus = [c for c in components_repo.get_by_category("GPU") if c.price_usd <= max_gpu_cost]
    assert affordable_gpus  # at least one GPU must fit at this ceiling

    most_expensive_affordable_gpu = max(affordable_gpus, key=lambda c: c.price_usd)
    remaining_cost = solvers.cheapest_fill_cost({"GPU": most_expensive_affordable_gpu}, other_categories)
    assert most_expensive_affordable_gpu.price_usd + remaining_cost <= ceiling

    # and the boundary itself is tight: the cheapest DISALLOWED gpu (if any)
    # should genuinely not fit, confirming the threshold isn't overly loose
    disallowed_gpus = [c for c in components_repo.get_by_category("GPU") if c.price_usd > max_gpu_cost]
    if disallowed_gpus:
        cheapest_disallowed = min(disallowed_gpus, key=lambda c: c.price_usd)
        remaining_for_disallowed = solvers.cheapest_fill_cost({"GPU": cheapest_disallowed}, other_categories)
        assert cheapest_disallowed.price_usd + remaining_for_disallowed > ceiling


@pytest.mark.parametrize("ceiling", [800.0, 1200.0, 1500.0, 2000.0])
def test_budget_solver_never_exceeds_ceiling_across_limits_from_empty(seeded_db, ceiling):
    """Hard invariant, checked at every ceiling the verification directive
    named: an empty from-scratch Budget build must never total more than the
    ceiling, whatever that ceiling is."""
    build = solvers.initialize_budget_build(ceiling)
    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


@pytest.mark.parametrize("ceiling", [800.0, 1200.0, 1500.0, 2000.0])
def test_budget_solver_never_exceeds_ceiling_across_limits_with_pin(seeded_db, ceiling):
    """Same hard invariant, but seeded with a pinned expensive GPU at each
    ceiling — exercises the pin-preserved-when-possible path as well as the
    final all-categories-adjustable safety net for ceilings too tight for
    the pin to survive."""
    from db.repositories import components_repo

    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    build = solvers.initialize_budget_build(ceiling, seed_selection={"GPU": priciest_gpu})
    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)


def test_minimum_possible_build_cost_never_exceeds_greedy_fill_cost(seeded_db):
    """minimum_possible_build_cost() is a real exhaustive search, not a
    delegation to the greedy cheapest_fill_cost({}, CATEGORY_ORDER) — greedy
    can get stuck locking in an early category's own cheapest option even
    when a slightly pricier choice there would unlock a much cheaper LATER
    category (verified against this seed catalog: greedy finds $661, the
    true minimum is $632). The true minimum must never exceed what greedy
    finds (greedy's own path is always in the exhaustive search space, so
    the true minimum is always <=), and the current seed catalog's known
    gap must actually manifest (proving this isn't a no-op)."""
    true_min = solvers.minimum_possible_build_cost()
    greedy = solvers.cheapest_fill_cost({}, list(solvers.CATEGORY_ORDER))
    assert true_min <= greedy
    assert true_min < greedy  # the known Case/Cooler joint-optimum gap in this catalog is real


def test_true_minimum_build_is_compatible_and_complete(seeded_db):
    """The exact combination _true_minimum_build()/minimum_possible_build_cost()
    relies on must itself be a complete, 100% compatible 8-part build — not
    just a number that happens to be lower."""
    build = solvers._true_minimum_build()
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True
    assert sum(c.price_usd for c in build.values()) == solvers.minimum_possible_build_cost()


def test_budget_solver_at_minimum_floor_completes_build(seeded_db):
    """A ceiling set exactly at the floor must still produce a complete,
    compatible 8-part build whose total sits at (not above) that floor —
    proving the floor value itself is achievable, not just a lower bound."""
    floor = solvers.minimum_possible_build_cost()
    build = solvers.initialize_budget_build(floor)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= floor
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_ceiling_below_minimum_floor_still_completes_build_gracefully(seeded_db):
    """Entering a ceiling below the true minimum floor is mathematically
    unsatisfiable by definition of `floor` — no cheaper complete build
    exists. The solver must still degrade gracefully (never leave a
    category unfilled, never raise) rather than error out. It does NOT
    necessarily land exactly at the floor cost here — the floor's exact
    combination also doesn't fit this ceiling (that's the whole point of
    "below the floor"), so the solver falls back to whatever its own
    single-category-swap repair pass can best achieve, per
    _greedy_fill's docstring."""
    floor = solvers.minimum_possible_build_cost()
    below_floor_ceiling = floor - 1.0

    build = solvers.initialize_budget_build(below_floor_ceiling)

    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


@pytest.mark.parametrize("ceiling_offset", [0.0, 5.0, 15.0, 28.0])
def test_ceiling_between_true_floor_and_greedy_local_optimum_still_satisfied(seeded_db, ceiling_offset):
    """The core regression this round's fix targets: _enforce_ceiling only
    ever swaps one category at a time, so it can get permanently stuck
    above a ceiling that's mathematically achievable but requires changing
    two categories together (e.g. Case+Cooler in this seed catalog — see
    _true_minimum_build's docstring). For every ceiling between the true
    floor and the greedy local optimum it would otherwise get stuck at, the
    solver must still land at or under the ceiling — this used to fail
    outright (total_cost > ceiling) before the true-minimum fallback was
    added to _greedy_fill."""
    floor = solvers.minimum_possible_build_cost()
    ceiling = floor + ceiling_offset

    build = solvers.initialize_budget_build(ceiling)
    total_cost = sum(c.price_usd for c in build.values())

    assert total_cost <= ceiling
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_category_max_cap_permits_candidate_exactly_at_boundary(seeded_db):
    """Mirrors ui/components/part_picker.py's per-candidate Category Max Cap
    check using only engine.solvers primitives (part_picker.py itself isn't
    unit-tested per this project's UI testing convention). A candidate
    priced exactly at the cap — price + reserve-with-this-candidate == the
    ceiling — must be permitted (price_usd <= Category_Max_Cap), not
    incorrectly treated as over budget. This is the exact boundary the
    verification directive's "$99 motherboard, $282 headroom" example
    describes."""
    from db.repositories import components_repo

    category = "Motherboard"
    other_categories = [c for c in solvers.CATEGORY_ORDER if c != category]
    candidate = min(components_repo.get_by_category(category), key=lambda c: c.price_usd)

    reserve_with_candidate = solvers.cheapest_fill_cost({category: candidate}, other_categories)
    ceiling = candidate.price_usd + reserve_with_candidate  # constructed to land exactly on the boundary

    spent_locked = 0.0
    category_max_cap = ceiling - spent_locked - reserve_with_candidate

    assert candidate.price_usd == pytest.approx(category_max_cap)
    assert candidate.price_usd <= category_max_cap  # must be selectable, not disabled


def test_get_compatible_candidates_narrows_by_socket(seeded_db):
    from db.repositories import components_repo

    am5_cpu = next(c for c in components_repo.get_by_category("CPU") if c.socket == "AM5")
    compatible_boards = solvers.get_compatible_candidates("Motherboard", {"CPU": am5_cpu})

    assert len(compatible_boards) > 0
    assert all(board.socket == "AM5" for board in compatible_boards)


@pytest.mark.parametrize("profile", ["General", "Gaming", "VideoEditing", "Design", "Programming"])
def test_workload_baseline_generation_across_profiles(seeded_db, profile):
    build = solvers.allocate_workload_baseline(profile, target_tier="Mid")

    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(build).is_compatible is True


def test_workload_baseline_respects_tier_when_available(seeded_db):
    build = solvers.allocate_workload_baseline("Gaming", target_tier="High")
    assert build["CPU"].benchmark_score is not None
    # a "High" gaming tier pick should clearly outperform an "Entry" one
    entry_build = solvers.allocate_workload_baseline("Gaming", target_tier="Entry")
    assert build["CPU"].benchmark_score >= entry_build["CPU"].benchmark_score


@pytest.mark.parametrize("profile", ["General", "Gaming", "VideoEditing", "Design", "Programming"])
def test_workload_tier_costs_are_strictly_monotonic(seeded_db, profile):
    """The core regression this guards against: a tier's own median-priced
    per-category picks (from workload_mappings' curated tier tags) could
    sum to no more — or even less — than a cheaper tier's, since those tags
    are a per-component "fits this workload at this tier" judgment call,
    not a strict price partition. Verified empirically before the fix: 4 of
    5 profiles violated this. Every profile must now satisfy
    Cost(Entry) < Cost(Mid) < Cost(High) < Cost(Enthusiast), strictly."""
    costs = {}
    for tier in solvers.WORKLOAD_TIERS:
        build = solvers.allocate_workload_baseline(profile, target_tier=tier)
        costs[tier] = sum(c.price_usd for c in build.values())
        assert set(build.keys()) == set(solvers.CATEGORY_ORDER)
        assert compatibility.evaluate_build(build).is_compatible is True

    assert costs["Entry"] < costs["Mid"] < costs["High"] < costs["Enthusiast"]


@pytest.mark.parametrize("profile", ["General", "Gaming", "VideoEditing", "Design", "Programming"])
def test_workload_tier_costs_stay_monotonic_with_peripherals(seeded_db, profile):
    """Same invariant, with include_peripherals=True (the flag "Generate
    baseline build" now always passes) — peripheral cost is folded into
    each tier's total from the start (see _build_workload_tier), so the
    ordering guarantee must hold with them included too, not just for the
    8 core categories alone."""
    costs = {}
    for tier in solvers.WORKLOAD_TIERS:
        build = solvers.allocate_workload_baseline(profile, target_tier=tier, include_peripherals=True)
        costs[tier] = sum(c.price_usd for c in build.values())
        assert set(solvers.CATEGORY_ORDER).issubset(build.keys())
        assert compatibility.evaluate_build(build).is_compatible is True

    assert costs["Entry"] < costs["Mid"] < costs["High"] < costs["Enthusiast"]


def test_workload_baseline_default_excludes_peripherals(seeded_db):
    """include_peripherals defaults to False — every existing caller
    (including every other workload test in this file) must see
    byte-identical, core-only behavior unless it explicitly opts in."""
    build = solvers.allocate_workload_baseline("Gaming", target_tier="Mid")
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)


# ---------------------------------------------------------------------------
# Bidirectional dynamic budgeting for optional peripherals
# ---------------------------------------------------------------------------
def test_removing_peripheral_releases_its_exact_price_as_core_headroom(seeded_db):
    """Peripherals never contribute to the Core Empty Reserve (they're 100%
    optional, whether present or absent), so removing a selected peripheral
    must free up EXACTLY its own price as headroom for a core slot — no
    reserve recalculation needed, since the peripheral was never counted in
    that reserve to begin with. Mirrors ui/components/part_picker.py's
    Max_Allowed_Price formula using only engine.solvers primitives (ui/ isn't
    unit-tested per this project's convention)."""
    from db.repositories import components_repo

    ceiling = 1000.0
    network_card = components_repo.get_by_category("NetworkCard")[0]
    other_core_categories = [c for c in solvers.CATEGORY_ORDER if c != "GPU"]

    build_state_with_peripheral = {"NetworkCard": network_card}
    spent_with_peripheral = network_card.price_usd
    reserve = solvers.cheapest_fill_cost(build_state_with_peripheral, other_core_categories)
    max_allowed_gpu_with_peripheral = ceiling - spent_with_peripheral - reserve

    # remove the peripheral -> empty build
    reserve_after_removal = solvers.cheapest_fill_cost({}, other_core_categories)
    max_allowed_gpu_after_removal = ceiling - 0.0 - reserve_after_removal

    # the peripheral was never part of the core reserve either way, so the
    # ONLY thing that changes on removal is spent_elsewhere dropping by its
    # exact price
    assert reserve == reserve_after_removal
    assert max_allowed_gpu_after_removal - max_allowed_gpu_with_peripheral == pytest.approx(network_card.price_usd)


def test_expensive_peripheral_exceeding_core_floor_is_flagged_over_budget(seeded_db):
    """A peripheral priced above `Budget_Ceiling - Core_Empty_Reserve` must be
    mathematically over budget (picking it really would leave less than the
    minimum required to complete the 8 core slots) — and one priced at or
    under that cap must genuinely still fit. Catalog-agnostic: proves the
    boundary itself is sound rather than depending on specific seeded
    prices."""
    from db.repositories import components_repo

    ceiling = 700.0
    core_floor = solvers.cheapest_fill_cost({}, list(solvers.CATEGORY_ORDER))
    max_allowed_peripheral = ceiling - core_floor

    for category in ("NetworkCard", "SoundCard", "OpticalDrive"):
        for card in components_repo.get_by_category(category):
            if card.price_usd > max_allowed_peripheral:
                assert card.price_usd + core_floor > ceiling
            else:
                assert card.price_usd + core_floor <= ceiling


def test_downgrade_pinned_until_feasible_preserves_peripheral_while_downgrading_core_pin(seeded_db):
    """A pinned peripheral is a fixed, off-the-top budget deduction — the
    solver's best-effort 'preserve intent' downgrade pass
    (_downgrade_pinned_until_feasible) must never swap it out as long as a
    pinned CORE category can absorb the adjustment instead. Mirrors the
    already-established test_budget_solver_downgrades_pinned_gpu_when_infeasible
    scenario, with a pinned peripheral riding along unaffected."""
    from db.repositories import components_repo

    priciest_gpu = max(components_repo.get_by_category("GPU"), key=lambda c: c.price_usd)
    priciest_optical_drive = max(components_repo.get_by_category("OpticalDrive"), key=lambda c: c.price_usd)

    # the peripheral's price appears identically on both sides (pinned_cost
    # and ceiling), so it cancels out and this triggers the downgrade pass
    # under exactly the same condition as the GPU-only version of this test.
    ceiling = priciest_optical_drive.price_usd + priciest_gpu.price_usd + 10.0

    seed = {"GPU": priciest_gpu, "OpticalDrive": priciest_optical_drive}
    build = solvers.initialize_budget_build(ceiling, seed_selection=seed)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling
    assert build["OpticalDrive"].id == priciest_optical_drive.id  # peripheral pin preserved untouched
    assert build["GPU"].id != priciest_gpu.id  # the core pin absorbed the downgrade instead
    assert set(cat for cat in build if cat in solvers.CATEGORY_ORDER) == set(solvers.CATEGORY_ORDER)


def test_budget_solver_accounts_for_preselected_peripherals(seeded_db):
    """A user who's already picked a (possibly pricey) peripheral before
    clicking "Generate starting build" must still get a complete core build
    where TOTAL cost (core + peripheral) respects the ceiling — the
    peripheral eats into the same budget pool, it doesn't get a free pass.
    Exercises the exact path ui/views/create_build.py's button handler uses:
    a peripheral riding along in `seed_selection`."""
    from db.repositories import components_repo

    priciest_network_card = max(components_repo.get_by_category("NetworkCard"), key=lambda c: c.price_usd)
    # comfortably above the true achievable minimum (core floor + this
    # peripheral), not an arbitrary fixed margin — otherwise the ceiling could
    # be mathematically infeasible regardless of solver correctness.
    ceiling = priciest_network_card.price_usd + solvers.minimum_possible_build_cost() + 100.0
    seed = {"NetworkCard": priciest_network_card}

    build = solvers.initialize_budget_build(ceiling, seed_selection=seed)

    total_cost = sum(c.price_usd for c in build.values())
    assert total_cost <= ceiling

    core_only = {cat: c for cat, c in build.items() if cat in solvers.CATEGORY_ORDER}
    assert set(core_only.keys()) == set(solvers.CATEGORY_ORDER)
    assert compatibility.evaluate_build(core_only).is_compatible is True


def test_default_initialize_budget_build_never_adds_peripherals(seeded_db):
    """fill_peripherals_with_surplus defaults to False — every existing
    caller (including every other test in this file) must see byte-
    identical, core-only behavior unless it explicitly opts in. This is
    the regression guard for that default."""
    build = solvers.initialize_budget_build(5000.0)
    assert set(build.keys()) == set(solvers.CATEGORY_ORDER)


def test_budget_solver_fills_peripherals_with_surplus_when_opted_in(seeded_db):
    """On a high budget where the 8 core parts max out well below the
    ceiling, initialize_budget_build(..., fill_peripherals_with_surplus=True)
    must spend the leftover on optional peripherals (NetworkCard/SoundCard/
    OpticalDrive — the only peripheral categories that actually exist in
    this catalog) while total cost strictly stays <= ceiling."""
    ceiling = 5000.0
    core_only_build = solvers.initialize_budget_build(ceiling)
    core_total = sum(c.price_usd for c in core_only_build.values())
    assert core_total < ceiling  # sanity: there really is surplus to spend at this ceiling

    build = solvers.initialize_budget_build(ceiling, fill_peripherals_with_surplus=True)
    total_cost = sum(c.price_usd for c in build.values())

    assert total_cost <= ceiling
    assert set(solvers.CATEGORY_ORDER).issubset(build.keys())  # every core slot still filled
    peripherals_added = [cat for cat in solvers.PERIPHERAL_CATEGORIES if cat in build]
    assert peripherals_added  # at least one peripheral was added given this much surplus
    assert compatibility.evaluate_build(build).is_compatible is True


def test_peripheral_surplus_fill_never_exceeds_ceiling_across_limits(seeded_db):
    """Hard invariant across a range of ceilings: total cost (core +
    whatever peripherals got auto-filled) must never exceed the ceiling,
    whether or not there's enough surplus for every peripheral, or any at
    all."""
    from db.repositories import components_repo

    cheapest_peripheral_total = sum(
        min(components_repo.get_by_category(cat), key=lambda c: c.price_usd).price_usd
        for cat in solvers.PERIPHERAL_CATEGORIES
    )
    core_floor = solvers.minimum_possible_build_cost()

    for ceiling in (core_floor + 5.0, core_floor + cheapest_peripheral_total, 3000.0, 5000.0):
        build = solvers.initialize_budget_build(ceiling, fill_peripherals_with_surplus=True)
        total_cost = sum(c.price_usd for c in build.values())
        assert total_cost <= ceiling
        assert set(solvers.CATEGORY_ORDER).issubset(build.keys())
