"""Monotonic Improvement Gate (spec.md section 6.6.3) coverage for
llm/advisory.py: degrading / lateral / no-op swaps are discarded, strictly
improving ones are kept, stretch actions that don't improve are discarded
with added_cost_usd recomputed. HTTP is mocked; the seeded catalog is real."""
from __future__ import annotations

import json

import pytest

from engine import scoring, solvers
from llm import advisory
from tests.test_advisory import (  # noqa: F401  (seeded_db is a fixture)
    VALID_ADVISORY_PAYLOAD,
    _fake_openrouter_response,
    _set_env,
    make_build_state,
    seeded_db,
)


def _metrics(state, quantities=None):
    return solvers.build_metrics(state, quantities)


def _cpu_bound_state_with_upgradable_gpu():
    """Weak CPU + a mid-tier GPU that is NOT the priciest: CPU-bound, so a
    pricier (stronger) GPU strictly widens the gap -- a degrading action."""
    state = make_build_state()
    gpus = sorted(solvers.get_compatible_candidates("GPU", state), key=lambda c: c.price_usd)
    cpu_score = state["CPU"].benchmark_score or 0
    for i, gpu in enumerate(gpus[:-1]):
        if (gpu.benchmark_score or 0) > cpu_score:
            state = {**state, "GPU": gpu}
            pricier = next(g for g in gpus[i + 1 :] if (g.benchmark_score or 0) > (gpu.benchmark_score or 0))
            return state, pricier
    pytest.skip("seed catalog has no suitable mid-tier GPU fixture")


def _stronger_gpu(state):
    return _cpu_bound_state_with_upgradable_gpu()[1]


def test_is_monotonic_improvement_rules():
    imp = solvers.is_monotonic_improvement
    assert imp((0, 90.0, 5.0), (0, 92.0, 5.0))  # synergy better
    assert imp((0, 90.0, 5.0), (0, 90.0, 3.0))  # bottleneck better
    assert not imp((0, 95.0, 1.0), (0, 85.0, 10.0))  # both degrade
    assert not imp((0, 90.0, 5.0), (0, 95.0, 8.0))  # synergy up, bottleneck up
    assert not imp((0, 90.0, 5.0), (0, 90.0, 5.0))  # lateral
    assert not imp((0, 90.0, 5.0), (1, 92.0, 3.0))  # introduces a warning
    assert imp((2, 80.0, 5.0), (1, 82.0, 5.0))  # strictly fewer warnings
    assert not imp((2, 80.0, 5.0), (2, 82.0, 5.0))  # warnings neither 0 nor fewer
    assert not imp(None, (0, 90.0, 5.0))


def test_gate_discards_degrading_swap(seeded_db):
    state, stronger = _cpu_bound_state_with_upgradable_gpu()
    assert _metrics({**state, "GPU": stronger})[2] > _metrics(state)[2]  # bottleneck worsens
    kept, final_state, _q = advisory._gate_actions(
        state, None, [{"action": "swap", "category": "GPU", "replace_with_id": stronger.id}]
    )
    assert kept == []
    assert final_state["GPU"].id == state["GPU"].id


def test_gate_discards_noop_and_lateral(seeded_db):
    state = make_build_state()
    actions = [{"action": "swap", "category": "CPU", "replace_with_id": state["CPU"].id}]  # no-op
    lateral = [c for c in solvers.get_compatible_candidates("Motherboard", state) if c.id != state["Motherboard"].id]
    if lateral:
        actions.append({"action": "swap", "category": "Motherboard", "replace_with_id": lateral[0].id})
    kept, _state, _q = advisory._gate_actions(state, None, actions)
    assert kept == []


def test_gate_keeps_strictly_improving_swap(seeded_db):
    state = make_build_state()
    best = solvers.find_bottleneck_minimizing_swap(state)
    assert best is not None
    new_state, category = best
    action = {"action": "swap", "category": category, "replace_with_id": new_state[category].id}
    kept, final_state, _q = advisory._gate_actions(state, None, [action])
    assert kept == [action]
    assert solvers.is_monotonic_improvement(_metrics(state), _metrics(final_state))


def test_gate_drops_second_duplicate_category_swap(seeded_db):
    state = make_build_state()
    new_state, category = solvers.find_bottleneck_minimizing_swap(state)
    first = {"action": "swap", "category": category, "replace_with_id": new_state[category].id}
    kept, _s, _q = advisory._gate_actions(state, None, [first, dict(first)])
    assert kept == [first]


def test_llm_degrading_within_budget_swap_is_discarded(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    state, stronger = _cpu_bound_state_with_upgradable_gpu()
    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["within_budget"]["swaps"] = [{"category": "GPU", "replace_with_id": stronger.id}]
    payload["within_budget"]["explanation"] = "Swap the GPU for a stronger one."
    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = advisory.get_build_advisory(state, mode="Free", current_budget_or_cost=1000.0)

    swaps = result["within_budget"]["swaps"]
    assert all(s["replace_with_id"] != stronger.id for s in swaps)
    assert result["within_budget"]["explanation"] != "Swap the GPU for a stronger one."
    gated = [{"action": "swap", **s} for s in swaps]
    if gated:  # anything surviving (from the also-gated heuristic) is strictly better
        _k, final_state, _q = advisory._gate_actions(state, None, gated)
        assert solvers.is_monotonic_improvement(_metrics(state), _metrics(final_state))
    else:
        assert "already optimal" in result["within_budget"]["explanation"]


def test_within_budget_all_dropped_and_heuristic_empty_gives_honest_empty(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    state, stronger = _cpu_bound_state_with_upgradable_gpu()
    monkeypatch.setattr(
        advisory,
        "_heuristic_within_budget",
        lambda *a, **k: {"explanation": advisory._NO_IN_BUDGET_IMPROVEMENT, "swaps": [], "can_optimize_further": False},
    )
    out = advisory._sanitize_within_budget(
        state,
        {
            "explanation": "x",
            "swaps": [{"action": "swap", "category": "GPU", "replace_with_id": stronger.id}],
            "can_optimize_further": True,
        },
        "CPU-bound",
        "Free",
        None,
        None,
    )
    assert out["swaps"] == []
    assert "already optimal" in out["explanation"]


def test_stretch_that_degrades_is_discarded_and_cost_zeroed(seeded_db):
    state, pricier = _cpu_bound_state_with_upgradable_gpu()
    stretch = {
        "explanation": f"Upgrade GPU to {pricier.name}.",
        "actions": [{"action": "swap", "category": "GPU", "replace_with_id": pricier.id}],
        "added_cost_usd": pricier.price_usd - state["GPU"].price_usd,
    }
    out = advisory._sanitize_stretch(state, stretch, None)
    assert out["actions"] == []
    assert out["added_cost_usd"] == 0.0
    assert "strictly improves" in out["explanation"]


def test_stretch_quantity_only_action_is_discarded(seeded_db):
    state = make_build_state()
    state["RAM"] = solvers.get_compatible_candidates("RAM", state)[0]
    slots = advisory._real_slot_count(state, "RAM")
    if not slots or slots < 2:
        pytest.skip("fixture RAM/board has no second slot")
    stretch = {
        "explanation": "Add a RAM kit.",
        "actions": [{"action": "set_quantity", "category": "RAM", "quantity": 2}],
        "added_cost_usd": state["RAM"].price_usd,
    }
    out = advisory._sanitize_stretch(state, stretch, None)
    assert out["actions"] == [] and out["added_cost_usd"] == 0.0


def test_stretch_improving_action_kept_and_cost_recomputed(seeded_db):
    state = make_build_state()
    new_state, category = solvers.find_bottleneck_minimizing_swap(state)
    new = new_state[category]
    real_delta = round(new.price_usd - state[category].price_usd, 2)
    stretch = {
        "explanation": "LLM text",
        "actions": [{"action": "swap", "category": category, "replace_with_id": new.id}],
        "added_cost_usd": real_delta + 0.3,
    }
    out = advisory._sanitize_stretch(state, stretch, None)
    assert len(out["actions"]) == 1
    assert out["added_cost_usd"] == real_delta


def test_heuristic_stretch_only_returns_gated_actions(seeded_db):
    state = make_build_state()
    _pct, direction = scoring.bottleneck_percentage_baseline(state)
    result = advisory._heuristic_stretch_budget(state, direction, "Free", None)
    kept, _s, _q = advisory._gate_actions(state, None, result["actions"])
    assert kept == result["actions"]
    if not result["actions"]:
        assert result["added_cost_usd"] == 0.0


def test_heuristic_within_budget_only_returns_gated_swaps(seeded_db):
    state = make_build_state()
    _pct, direction = scoring.bottleneck_percentage_baseline(state)
    result = advisory._heuristic_within_budget(state, direction, "Free", None)
    swaps = [{"action": "swap", **s} for s in result["swaps"]]
    kept, final_state, _q = advisory._gate_actions(state, None, swaps)
    assert len(kept) == len(swaps)
    if swaps:
        assert solvers.is_monotonic_improvement(_metrics(state), _metrics(final_state))


def _resolver():
    from db.repositories import components_repo

    return components_repo.get_by_id


def test_optimize_in_place_rejects_degrading_advisory_swap(seeded_db, monkeypatch):
    from llm import concierge

    state = make_build_state()
    weakest = state["CPU"]
    mid_cpu = next(
        c
        for c in solvers.get_compatible_candidates("CPU", state)
        if c.id != weakest.id and c.price_usd > weakest.price_usd
    )
    state = {**state, "CPU": mid_cpu}
    assert solvers.is_monotonic_improvement(_metrics({**state, "CPU": weakest}), _metrics(state))

    monkeypatch.setattr(
        concierge,
        "get_build_advisory",
        lambda *a, **k: {
            "within_budget": {"swaps": [{"action": "swap", "category": "CPU", "replace_with_id": weakest.id}]}
        },
    )
    monkeypatch.setattr(solvers, "find_bottleneck_minimizing_swap", lambda _s: None)
    out = concierge._optimize_in_place(state, {}, "Free", 5000.0, None, _resolver())
    assert out["CPU"].id == mid_cpu.id  # the degrading swap is never applied


def test_optimize_build_full_never_worsens_and_is_monotonic(seeded_db, monkeypatch):
    from llm import concierge

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_build_state()
    before = _metrics(state)
    result = concierge.optimize_build_full(state, {}, "Free", 2200.0, _resolver())
    after = solvers.build_metrics(result.build_state, result.quantities)
    assert after is not None
    assert after[0] <= before[0]
    assert after[1] >= before[1] - 1e-6
    assert after[2] <= before[2] + 1e-6


def test_bottleneck_solver_is_monotonic_and_terminates(seeded_db):
    """find_bottleneck_minimizing_swap applied repeatedly never cycles: each
    step strictly reduces the bottleneck, so it must reach None."""
    state = make_build_state()
    seen = []
    for _ in range(50):
        result = solvers.find_bottleneck_minimizing_swap(state)
        if result is None:
            break
        new_state, _cat = result
        assert solvers.is_monotonic_improvement(_metrics(state), _metrics(new_state))
        assert all(new_state != old for old in seen)
        seen.append(state)
        state = new_state
    else:
        pytest.fail("solver did not converge")


# ---- Stretch scope: CPU / GPU / Motherboard only (spec.md section 6.6.3) ----
def _full_cpu_bound_build():
    from db.repositories import components_repo

    build = solvers.initialize_budget_build(2500)
    build["GPU"] = sorted(components_repo.get_by_category("GPU"), key=lambda c: c.benchmark_score or 0)[-1]
    weakest = sorted(solvers.get_compatible_candidates("CPU", build), key=lambda c: c.benchmark_score or 0)[0]
    build["CPU"] = weakest
    return build


def test_stretch_scope_constant():
    assert advisory.STRETCH_CATEGORIES == ("CPU", "GPU", "Motherboard")


def test_platform_upgrade_pairs_cpu_and_motherboard_and_passes_gate(seeded_db):
    build = _full_cpu_bound_build()
    result = advisory._try_platform_upgrade(build, {}, "CPU")
    assert result is not None
    assert [a["category"] for a in result["actions"]] == ["CPU", "Motherboard"]
    kept, final_state, _q = advisory._gate_actions(build, {}, result["actions"])
    assert len(kept) == 2
    before, after = _metrics(build), _metrics(final_state)
    assert solvers.is_monotonic_improvement(before, after)
    assert after[0] == 0  # fully compatible again after the joint swap
    assert result["added_cost_usd"] > 0


def test_motherboard_swap_that_improves_metrics_is_not_rejected_as_non_scoring(seeded_db):
    """A board swap that resolves a compatibility warning improves the build
    (warnings strictly fewer, synergy up), so the gate must accept it."""
    build = solvers.initialize_budget_build(2000)
    board = build["Motherboard"]
    others = {c: v for c, v in build.items() if c != "Motherboard"}
    wrong = next(
        (
            m for m in advisory.solvers.components_repo.get_by_category("Motherboard")
            if m.id != board.id and m.socket != board.socket
        ),
        None,
    )
    if wrong is None:
        pytest.skip("no differing-socket board in seed catalog")
    broken = {**others, "Motherboard": wrong}
    assert _metrics(broken)[0] > 0
    action = {"action": "swap", "category": "Motherboard", "replace_with_id": board.id}
    kept, final_state, _q = advisory._gate_actions(broken, {}, [action])
    assert kept == [action]
    assert final_state["Motherboard"].id == board.id


def test_sanitize_stretch_drops_out_of_scope_actions(seeded_db):
    build = _full_cpu_bound_build()
    cooler = solvers.get_compatible_candidates("Cooler", build)[0]
    stretch = {
        "explanation": "Upgrade cooler and add RAM.",
        "actions": [
            {"action": "swap", "category": "Cooler", "replace_with_id": cooler.id},
            {"action": "set_quantity", "category": "RAM", "quantity": 2},
        ],
        "added_cost_usd": 99.0,
    }
    result = advisory._sanitize_stretch(build, stretch, {})
    assert result["actions"] == [] and result["added_cost_usd"] == 0.0


def test_heuristic_stretch_only_proposes_scope_categories(seeded_db):
    build = _full_cpu_bound_build()
    result = advisory._heuristic_stretch_budget(build, "CPU-bound", "Free", None, {})
    assert result["actions"]
    assert {a["category"] for a in result["actions"]} <= set(advisory.STRETCH_CATEGORIES)
