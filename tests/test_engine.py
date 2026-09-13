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
