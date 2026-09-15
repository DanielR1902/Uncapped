"""Metrics & scoring: compatibility % (delegated), bottleneck baseline, value/ratio index.

Per spec.md §5.3-§5.5. Pure Python, no Streamlit/network/DB-write (engine/CLAUDE.md).
"""
from __future__ import annotations

from db.models import Component
from engine.compatibility import BuildState, evaluate_build


def compatibility_score(build_state: BuildState) -> float:
    """0-100. Delegates to compatibility.evaluate_build so the percentage formula
    lives in exactly one place (compatibility.py owns the pass/fail + score math;
    this is a thin, spec-mandated re-export from scoring.py)."""
    return evaluate_build(build_state).compatibility_score


def bottleneck_percentage_baseline(build_state: BuildState) -> tuple[float, str]:
    """(percentage, direction). direction is 'CPU-bound' | 'GPU-bound' | 'Balanced'.
    Returns (0.0, 'Balanced') when CPU or GPU isn't picked yet, or either lacks a
    benchmark_score — there's nothing to compare."""
    cpu = build_state.get("CPU")
    gpu = build_state.get("GPU")
    if cpu is None or gpu is None or cpu.benchmark_score is None or gpu.benchmark_score is None:
        return 0.0, "Balanced"

    cpu_score = cpu.benchmark_score
    gpu_score = gpu.benchmark_score
    higher = max(cpu_score, gpu_score)
    if higher == 0:
        return 0.0, "Balanced"

    percentage = abs(cpu_score - gpu_score) / higher * 100.0

    if cpu_score == gpu_score:
        direction = "Balanced"
    elif cpu_score < gpu_score:
        direction = "CPU-bound"
    else:
        direction = "GPU-bound"

    return percentage, direction


def heuristic_synergy_score(compatibility_score: float, bottleneck_percentage: float) -> float:
    """Deterministic, no-LLM synergy estimate: a compatible build with a big
    CPU/GPU imbalance still "works" but isn't well-matched, so bottleneck
    pulls the score down. Used by llm/client.py's fallback path and by
    db/seed_demo.py (mock data has no reason to hit a live API)."""
    return max(0.0, compatibility_score - bottleneck_percentage / 2)


def live_bottleneck_and_synergy(build_state: BuildState) -> tuple[float, float, str] | None:
    """Instant, local (no-LLM) synergy/bottleneck estimate for the Build
    Studio summary header — available the moment at least 2 components are
    picked, not gated behind the full analyze_build() call. Returns None when
    there aren't enough components yet; the caller shows a placeholder then.
    Returns (synergy_score, bottleneck_percentage, direction)."""
    if len(build_state) < 2:
        return None
    bottleneck_pct, direction = bottleneck_percentage_baseline(build_state)
    compat_score = compatibility_score(build_state)
    synergy = heuristic_synergy_score(compat_score, bottleneck_pct)
    return synergy, bottleneck_pct, direction


def value_index(component: Component, category_candidates: list[Component], build_state: BuildState) -> float:
    """Higher is better. Compares `component` against its category peers on
    (compatibility contribution if hypothetically slotted in) vs. price, per
    spec.md §5.5. Not clamped to 100 by design — a candidate that is both cheaper
    and more compatible than the category's median can legitimately score above
    the peer set's normalized ceiling."""
    if not category_candidates:
        return 0.0

    def contribution(candidate: Component) -> float:
        hypothetical = dict(build_state)
        hypothetical[candidate.category] = candidate
        return evaluate_build(hypothetical).compatibility_score

    peer_contributions = [contribution(c) for c in category_candidates]
    max_contribution = max(peer_contributions)
    max_price = max(c.price_usd for c in category_candidates)

    if max_contribution == 0 or max_price == 0:
        return 0.0

    normalized_compat = contribution(component) / max_contribution
    normalized_price = component.price_usd / max_price
    if normalized_price == 0:
        return 0.0

    return (normalized_compat / normalized_price) * 100.0
