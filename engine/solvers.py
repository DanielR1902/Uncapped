"""Build engines for the three creation modes (spec.md §5.2, §5.6, intent.txt §3):

- Mode A: Budget Constrained Build   -> initialize_budget_build / on_user_pins_component
- Mode B: Workload Profile Build     -> allocate_workload_baseline
- Mode C: Free Custom Build          -> get_compatible_candidates / get_all_compatible_candidates

All three share `filter_compatible`, which hypothetically slots each catalog
candidate into the current build_state and keeps only those that leave the
build fully compatible (engine.compatibility.evaluate_build). Pure Python,
read-only catalog access via db.repositories (engine/CLAUDE.md).
"""
from __future__ import annotations

from db.models import Component
from db.repositories import components_repo
from engine.compatibility import BuildState, evaluate_build

# GPU/CPU dominate both cost and performance, so they're locked in first —
# every other category then negotiates around whatever budget is left.
# Case is resolved before PSU (not the intuitive order) so that PSU's
# form-factor compatibility (rule 5, ATX vs SFX) is always checked against an
# already-fixed Case: picking a PSU first can strand the build with an SFX
# unit that no compatible Case is left to pair with once Case's turn comes,
# since SFX PSUs only pair with the catalog's ITX-only cases.
CATEGORY_ORDER: tuple[str, ...] = (
    "GPU",
    "CPU",
    "Motherboard",
    "RAM",
    "Storage",
    "Case",
    "PSU",
    "Cooler",
)


def filter_compatible(candidates: list[Component], build_state: BuildState, category: str) -> list[Component]:
    """Keep only candidates that, if slotted into `category`, leave the whole
    build_state fully compatible (not just compatible with one other part)."""
    compatible = []
    for candidate in candidates:
        hypothetical = dict(build_state)
        hypothetical[category] = candidate
        if evaluate_build(hypothetical).is_compatible:
            compatible.append(candidate)
    return compatible


def get_compatible_candidates(category: str, build_state: BuildState) -> list[Component]:
    """Mode C (Free Custom Build): the catalog for one category, narrowed to
    only what remains compatible with whatever is already selected."""
    all_candidates = components_repo.get_by_category(category)
    return filter_compatible(all_candidates, build_state, category)


def get_all_compatible_candidates(build_state: BuildState) -> dict[str, list[Component]]:
    """Same as get_compatible_candidates, for every core category at once —
    convenient for rendering all part-pickers together."""
    return {category: get_compatible_candidates(category, build_state) for category in CATEGORY_ORDER}


def _cheapest_price(category: str, build_state: BuildState) -> float:
    """Cheapest option for `category` compatible with the current build_state,
    falling back to the category's global cheapest if nothing compatible exists
    yet (keeps budget reservation from ever going undefined)."""
    candidates = get_compatible_candidates(category, build_state)
    if not candidates:
        candidates = components_repo.get_by_category(category)
    if not candidates:
        return 0.0
    return min(c.price_usd for c in candidates)


def cheapest_fill_cost(build_state: BuildState, categories: list[str]) -> float:
    """Cheapest possible additional cost to fill every category in
    `categories` (skipping ones already present in build_state) with its own
    cheapest compatible option, given what's already selected. Locks in each
    choice before evaluating the next category, so later categories' cheapest
    compatible option correctly accounts for earlier ones (e.g. socket match).
    Shared by the feasibility check in _greedy_fill and by
    ui/components/part_picker.py's Minimum Reserve Threshold filtering."""
    working = dict(build_state)
    total = 0.0
    for category in categories:
        if category in working:
            continue
        candidates = get_compatible_candidates(category, working)
        if not candidates:
            candidates = components_repo.get_by_category(category)
        if not candidates:
            continue
        cheapest = min(candidates, key=lambda c: c.price_usd)
        total += cheapest.price_usd
        working[category] = cheapest
    return total


def minimum_possible_build_cost() -> float:
    """The cheapest possible total cost of a complete, compatible 8-part
    build — the true floor below which no Budget ceiling can ever be
    satisfied. Delegates to cheapest_fill_cost so it can never silently
    drift from what the solver's own feasibility logic considers the
    minimum. Used by ui/views/create_build.py to clamp the Budget ceiling
    input's min_value and to auto-correct/warn when a user types a ceiling
    below it."""
    return cheapest_fill_cost({}, list(CATEGORY_ORDER))


def _downgrade_pinned_until_feasible(selection: BuildState, categories_to_fill: list[str], ceiling: float) -> BuildState:
    """When the caller's own pinned/pre-selected parts make the ceiling
    infeasible on their own, step the most expensive PINNED categories down
    to progressively cheaper compatible alternatives (highest tier that's
    still cheaper than the current pick) — one step at a time, most
    expensive pinned category first — until pinned_cost + cheapest_fill_cost
    of what's left fits under the ceiling, or there's nothing left to
    downgrade. Mirrors _enforce_ceiling's proven price-descending / progress-
    flag pattern below, just applied to the caller's pins instead of the
    solver's own picks. If even the cheapest compatible option for every
    pinned category still doesn't fit, this simply stops (no exception) —
    the caller proceeds with whatever's left, same graceful-degradation
    philosophy as a fresh empty selection with an unrealistic ceiling."""
    if not selection:
        return selection

    working = dict(selection)

    def pinned_plus_reserve(sel: BuildState) -> float:
        return sum(c.price_usd for c in sel.values()) + cheapest_fill_cost(sel, categories_to_fill)

    progress = True
    while pinned_plus_reserve(working) > ceiling and progress:
        progress = False
        for category in sorted(working, key=lambda cat: working[cat].price_usd, reverse=True):
            others = {c: v for c, v in working.items() if c != category}
            candidates = get_compatible_candidates(category, others)
            if not candidates:
                candidates = components_repo.get_by_category(category)
            if not candidates:
                continue

            current_price = working[category].price_usd
            cheaper = [c for c in candidates if c.price_usd < current_price]
            if not cheaper:
                continue  # this pinned category is already at its cheapest compatible option

            working[category] = max(cheaper, key=lambda c: c.price_usd)  # highest tier that's still cheaper
            progress = True
            if pinned_plus_reserve(working) <= ceiling:
                break

    return working


def _enforce_ceiling(result: BuildState, adjustable_categories: list[str], ceiling: float) -> BuildState:
    """Repair pass: the reservation estimate in the main loop can still
    undershoot when a downstream category's true minimum cost depends jointly
    on two categories that aren't both fixed yet (PSU headroom depends on both
    CPU and GPU, but they're chosen one at a time) — so the loop above is a
    heuristic, not a proof. This closes the gap: while over budget, repeatedly
    swap the current most expensive *adjustable* category to its own cheapest
    compatible alternative (holding everything else fixed) until it fits, or
    no further trim is possible. Categories outside `adjustable_categories`
    (already pinned by the caller) are never touched."""
    if not adjustable_categories:
        return result

    total = sum(c.price_usd for c in result.values())
    progress = True
    while total > ceiling and progress:
        progress = False
        for category in sorted(adjustable_categories, key=lambda cat: result[cat].price_usd, reverse=True):
            others = {c: v for c, v in result.items() if c != category}
            candidates = get_compatible_candidates(category, others)
            if not candidates:
                continue

            current_price = result[category].price_usd
            cost_of_everything_else = total - current_price
            budget_for_slot = ceiling - cost_of_everything_else

            # Prefer the most expensive candidate that still fits (stays close to
            # the ceiling); only fall back to the category's cheapest option if
            # nothing fits within budget_for_slot at all.
            affordable = [c for c in candidates if c.price_usd <= budget_for_slot]
            best = max(affordable, key=lambda c: c.price_usd) if affordable else min(
                candidates, key=lambda c: c.price_usd
            )

            if best.price_usd < current_price:
                total -= current_price - best.price_usd
                result[category] = best
                progress = True
                if total <= ceiling:
                    break
    return result


def _greedy_fill(selection: BuildState, categories_to_fill: list[str], ceiling: float) -> BuildState:
    """Mode A core loop (spec.md §5.2): for each open category, reserve enough
    budget for every OTHER open category's cheapest compatible option, then take
    the most expensive still-affordable, compatible candidate. Degrades to the
    category's cheapest compatible option rather than ever leaving it unfilled.
    Finishes with _enforce_ceiling so the ceiling invariant holds even where the
    per-category reservation heuristic underestimated a joint constraint, plus
    a final all-categories-adjustable safety net (see below) for the rare
    residual case where that repair pass alone still isn't enough.

    If `selection` (the caller's pinned/pre-selected parts) alone makes the
    ceiling infeasible — either those parts alone exceed it, or they leave
    less than the cheapest possible cost to fill every remaining category —
    this auto-downgrades the most expensive pinned categories to
    progressively cheaper compatible alternatives via
    _downgrade_pinned_until_feasible rather than raising. A fresh, empty
    selection is never touched by this (there's no prior user choice to
    step down); it degrades exactly as before via the main loop + repair
    pass below."""
    if selection:
        pinned_cost = sum(c.price_usd for c in selection.values())
        min_remaining = cheapest_fill_cost(selection, categories_to_fill)
        if pinned_cost > ceiling or pinned_cost + min_remaining > ceiling:
            selection = _downgrade_pinned_until_feasible(selection, categories_to_fill, ceiling)

    result = dict(selection)
    spent = sum(c.price_usd for c in result.values())
    remaining_budget = ceiling - spent
    open_categories = list(categories_to_fill)

    for category in open_categories:
        candidates = get_compatible_candidates(category, result)
        if not candidates:
            candidates = components_repo.get_by_category(category)
        if not candidates:
            continue  # nothing seeded in this category — skip rather than fail the whole build

        other_open = [c for c in open_categories if c != category and c not in result]
        future_min_cost = sum(_cheapest_price(c, result) for c in other_open)

        affordable = [c for c in candidates if c.price_usd <= remaining_budget - future_min_cost]
        if not affordable:
            affordable = [min(candidates, key=lambda c: c.price_usd)]

        chosen = max(affordable, key=lambda c: c.price_usd)
        result[category] = chosen
        remaining_budget -= chosen.price_usd

    result = _enforce_ceiling(result, categories_to_fill, ceiling)

    total = sum(c.price_usd for c in result.values())
    if total > ceiling:
        # Absolute last resort: cheapest_fill_cost's fixed-order estimate
        # (used by the feasibility pre-check above and by
        # _downgrade_pinned_until_feasible) and _enforce_ceiling's own
        # descending-price greedy repair order can, in rare cases where
        # compatibility constraints chain non-trivially across categories,
        # converge on slightly different totals. If the normal repair pass
        # (pinned categories excluded) still leaves the build over the
        # ceiling, re-run it treating EVERY category — including ones the
        # caller pinned — as adjustable. The ceiling is a hard invariant
        # (spec.md §5.2: "never exceeds the ceiling"); a pin only ever
        # yields to it once nothing else is left to trim.
        result = _enforce_ceiling(result, list(result.keys()), ceiling)

    return result


def initialize_budget_build(ceiling: float, seed_selection: BuildState | None = None) -> BuildState:
    """Mode A entry point. `seed_selection` lets a caller pre-pin one or more
    categories (e.g. the user picked a GPU first) — the solver only fills the
    categories not already present, per spec.md §5.2 ("doesn't matter which
    part they start with")."""
    selection = dict(seed_selection or {})
    remaining_categories = [c for c in CATEGORY_ORDER if c not in selection]
    return _greedy_fill(selection, remaining_categories, ceiling)


def on_user_pins_component(
    selection: BuildState,
    pinned_category: str,
    pinned_component: Component,
    ceiling: float,
) -> BuildState:
    """Mode A re-solve: user pins/changes one category; remaining budget is
    re-partitioned across whatever categories are still open."""
    new_selection = dict(selection)
    new_selection[pinned_category] = pinned_component
    remaining_categories = [c for c in CATEGORY_ORDER if c not in new_selection]
    return _greedy_fill(new_selection, remaining_categories, ceiling)


DEFAULT_WORKLOAD_TIER = "Mid"


def _median_by_price(candidates: list[Component]) -> Component:
    ordered = sorted(candidates, key=lambda c: c.price_usd)
    return ordered[len(ordered) // 2]


def allocate_workload_baseline(profile: str, target_tier: str = DEFAULT_WORKLOAD_TIER) -> BuildState:
    """Mode B entry point (spec.md §5.6). Picks a representative (median-priced)
    component per category from workload_mappings rows tagged for `profile` at
    `target_tier`, degrading to any tier for the profile, then to plain
    compatibility, rather than leaving a category empty."""
    selection: BuildState = {}
    tier_matches = components_repo.get_workload_matches(profile, target_tier)
    all_profile_matches = components_repo.get_workload_matches(profile)

    for category in CATEGORY_ORDER:
        candidates = [component for component, mapping in tier_matches if component.category == category]
        candidates = filter_compatible(candidates, selection, category)

        if not candidates:
            candidates = [component for component, mapping in all_profile_matches if component.category == category]
            candidates = filter_compatible(candidates, selection, category)

        if not candidates:
            candidates = get_compatible_candidates(category, selection)

        if not candidates:
            continue

        selection[category] = _median_by_price(candidates)

    return selection
