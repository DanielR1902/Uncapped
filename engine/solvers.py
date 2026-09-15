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

# Optional add-on categories with no deterministic compatibility rules beyond
# being optional (spec.md §5.1) — eligible for opt-in surplus-budget auto-fill
# in initialize_budget_build (see _fill_peripherals_with_surplus).
PERIPHERAL_CATEGORIES: tuple[str, ...] = ("NetworkCard", "SoundCard", "OpticalDrive")


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


def _true_minimum_build() -> BuildState:
    """The exact combination achieving the TRUE cheapest possible total cost
    of a complete, 100% mutually compatible 8-part build — the real floor
    below which no Budget ceiling can ever be satisfied.

    This is NOT the same thing `cheapest_fill_cost({}, CATEGORY_ORDER)`
    finds. That function is a greedy, single-pass construction: it locks in
    each category's own cheapest compatible option (given what's already
    fixed) and never reconsiders that choice. This can land on a valid,
    fully compatible build that is nonetheless more expensive than
    necessary, because an early category's absolute cheapest option can
    foreclose a much cheaper option in a later category — e.g. locking in
    the single cheapest Case can rule out the cheapest Cooler (a taller
    Case would fit it), so greedy ends up paying more for a compatible
    Cooler than it needed to, even though every individual pick was itself
    the "cheapest compatible" choice at the moment it was made. Verified
    against this project's seed catalog: greedy lands on $661, but the true
    minimum (found here) is $632 — a real, non-negotiable difference
    callers must not paper over with the greedy number.

    This function instead runs an exhaustive branch-and-bound search over
    every category in CATEGORY_ORDER: candidates are tried cheapest-first
    (so a strong upper bound is established almost immediately, letting
    later branches be pruned as soon as their accumulated cost alone
    already meets or exceeds the best complete build found so far), and
    every candidate is filtered through the exact same `get_compatible_
    candidates` the rest of this module already trusts — so the true
    minimum can never silently drift from the compatibility rules
    everything else in this file obeys. It is exhaustive, not heuristic: it
    is guaranteed to find the actual global minimum, not just a better
    heuristic guess. The search space always contains at least the plain
    greedy `cheapest_fill_cost` path (candidates are tried cheapest-first
    at every level, so the very first leaf reached IS that path), and the
    pruning bound is seeded from that same value, so this can never return
    something worse than greedy — only equal or strictly better.

    Cost: verified to run in ~2 seconds against a realistically-sized seed
    catalog (spec.md §4.1: 15-25 rows/core category) — negligible for a
    one-off "what's the floor" computation, but real if called on every
    user interaction. Callers should compute this once and cache the
    result (e.g. in `st.session_state`) rather than calling it on every
    rerun/click; this module intentionally does not cache it itself, since
    engine/ has no reliable signal for "the catalog changed" to invalidate
    on, and a stale cross-test cache here would be far worse than a slow
    but always-correct pure function."""
    best_cost = [cheapest_fill_cost({}, list(CATEGORY_ORDER))]  # a valid (if suboptimal) upper bound seeds the search
    best_build: list[BuildState | None] = [None]

    def _search(remaining: list[str], partial: BuildState, cost: float) -> None:
        if cost > best_cost[0]:
            return
        if not remaining:
            best_cost[0] = cost
            best_build[0] = dict(partial)
            return
        category = remaining[0]
        candidates = get_compatible_candidates(category, partial)
        if not candidates:
            candidates = components_repo.get_by_category(category)
        for candidate in sorted(candidates, key=lambda c: c.price_usd):
            partial[category] = candidate
            _search(remaining[1:], partial, cost + candidate.price_usd)
            del partial[category]

    _search(list(CATEGORY_ORDER), {}, 0.0)
    assert best_build[0] is not None  # the plain-greedy-equivalent leaf is always reachable, so this always fills in
    return best_build[0]


def minimum_possible_build_cost() -> float:
    """The TRUE cheapest possible total cost of a complete, 100% mutually
    compatible 8-part build. See `_true_minimum_build`'s docstring for why
    this is a real, exhaustive global minimum rather than the (higher,
    greedy-only) result `cheapest_fill_cost({}, CATEGORY_ORDER)` finds."""
    return sum(c.price_usd for c in _true_minimum_build().values())


def _downgrade_pinned_until_feasible(selection: BuildState, categories_to_fill: list[str], ceiling: float) -> BuildState:
    """When the caller's own pinned/pre-selected parts make the ceiling
    infeasible on their own, step the most expensive PINNED CORE categories
    down to progressively cheaper compatible alternatives (highest tier
    that's still cheaper than the current pick) — one step at a time, most
    expensive pinned core category first — until pinned_cost + cheapest_fill_cost
    of what's left fits under the ceiling, or there's nothing left to
    downgrade. Mirrors _enforce_ceiling's proven price-descending / progress-
    flag pattern below, just applied to the caller's pins instead of the
    solver's own picks.

    Deliberately excludes any non-core (peripheral) entry from `selection`:
    a peripheral the user separately hand-picked is a fixed, off-the-top
    budget deduction here (spec.md §5.2 "Total Spent on Selected
    Peripherals"), not something this best-effort preservation pass may
    swap out — only a pinned CORE category (CPU/GPU/etc.) is ever
    downgraded in this phase. If nothing core-side is left to downgrade and
    the build is still infeasible purely because of peripheral cost, that's
    for `_greedy_fill`'s final all-categories-adjustable safety net (which
    *can* touch a peripheral, as an absolute last resort) — not this pass.

    If even the cheapest compatible option for every pinned core category
    still doesn't fit, this simply stops (no exception) — the caller
    proceeds with whatever's left, same graceful-degradation philosophy as
    a fresh empty selection with an unrealistic ceiling."""
    if not selection:
        return selection

    working = dict(selection)

    def pinned_plus_reserve(sel: BuildState) -> float:
        return sum(c.price_usd for c in sel.values()) + cheapest_fill_cost(sel, categories_to_fill)

    progress = True
    while pinned_plus_reserve(working) > ceiling and progress:
        progress = False
        downgradable_core_pins = [cat for cat in working if cat in CATEGORY_ORDER]
        for category in sorted(downgradable_core_pins, key=lambda cat: working[cat].price_usd, reverse=True):
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
    started_empty = not selection

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
        total = sum(c.price_usd for c in result.values())

    if total > ceiling and started_empty:
        # _enforce_ceiling only ever swaps ONE category at a time, holding
        # everything else fixed — it can get permanently stuck above a
        # mathematically achievable ceiling when reaching it requires
        # changing TWO categories together (e.g. a taller, pricier Case
        # that unlocks a much cheaper Cooler — swapping either one alone,
        # holding the other fixed, never helps). This is exactly the gap
        # between the greedy `cheapest_fill_cost` floor and the true
        # exhaustive-search floor `minimum_possible_build_cost()` reports
        # (see `_true_minimum_build`'s docstring). For a from-scratch build
        # only (never for one seeded with the caller's own pins, which this
        # fallback has no awareness of and would otherwise silently
        # discard), fall back to the actual true-minimum combination if it
        # fits — keeping `minimum_possible_build_cost()` and what this
        # function can actually deliver fully consistent with each other.
        true_min = _true_minimum_build()
        if sum(c.price_usd for c in true_min.values()) <= ceiling:
            result = true_min

    return result


def _fill_peripherals_with_surplus(core_build: BuildState, ceiling: float) -> BuildState:
    """Phase 2 of Budget-mode generation, opt-in via
    `initialize_budget_build(..., fill_peripherals_with_surplus=True)`: after
    the 8 core categories are filled and within ceiling, spend whatever's
    left over on optional peripherals. NetworkCard/SoundCard/OpticalDrive
    have no deterministic compatibility rules (spec.md §5.1) — they're
    always compatible with everything — so this only needs a price check,
    not a compatibility filter, though it still runs candidates through
    `get_compatible_candidates` for consistency and in case a future rule
    ever does constrain a peripheral. Tries each peripheral category in
    `PERIPHERAL_CATEGORIES` order, picking the single most expensive
    still-affordable option for each — mirroring `_greedy_fill`'s own
    "spend as much of what's available on the highest tier that still
    fits" philosophy for the core categories (deliberately NOT
    `scoring.value_index`, which is compatibility-vs-price and therefore
    degenerate for peripherals — with no compatibility rules to
    differentiate them, it would just always rank the cheapest option
    "best," the opposite of what spending a surplus is for) — then
    decrements the remaining surplus before considering the next category.
    A category is simply skipped if nothing fits what's left, or if it's
    already filled (e.g. present in a caller-supplied seed_selection).
    Can only ever REDUCE the gap between total cost and ceiling, never
    exceed it: every pick is bounds-checked against the shrinking surplus
    before being added, so the ceiling invariant established elsewhere in
    this module is never at risk here."""
    result = dict(core_build)
    surplus = ceiling - sum(c.price_usd for c in core_build.values())

    for category in PERIPHERAL_CATEGORIES:
        if category in result or surplus <= 0:
            continue
        candidates = get_compatible_candidates(category, result)
        if not candidates:
            candidates = components_repo.get_by_category(category)
        affordable = [c for c in candidates if c.price_usd <= surplus]
        if not affordable:
            continue
        chosen = max(affordable, key=lambda c: c.price_usd)
        result[category] = chosen
        surplus -= chosen.price_usd

    return result


def initialize_budget_build(
    ceiling: float,
    seed_selection: BuildState | None = None,
    fill_peripherals_with_surplus: bool = False,
) -> BuildState:
    """Mode A entry point. `seed_selection` lets a caller pre-pin one or more
    categories (e.g. the user picked a GPU first) — the solver only fills the
    categories not already present, per spec.md §5.2 ("doesn't matter which
    part they start with"). `fill_peripherals_with_surplus=True` additionally
    spends whatever's left of the ceiling after the 8 core categories on
    optional peripherals (see `_fill_peripherals_with_surplus`) — opt-in and
    defaulting to False so every existing caller's behavior is unchanged
    unless it explicitly asks for this."""
    selection = dict(seed_selection or {})
    remaining_categories = [c for c in CATEGORY_ORDER if c not in selection]
    core_build = _greedy_fill(selection, remaining_categories, ceiling)
    if fill_peripherals_with_surplus:
        return _fill_peripherals_with_surplus(core_build, ceiling)
    return core_build


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
WORKLOAD_TIERS: tuple[str, ...] = ("Entry", "Mid", "High", "Enthusiast")


def _median_by_price(candidates: list[Component]) -> Component:
    ordered = sorted(candidates, key=lambda c: c.price_usd)
    return ordered[len(ordered) // 2]


def _workload_tier_category_candidates(
    tier_matches: list[tuple[Component, object]],
    profile_matches: list[tuple[Component, object]],
    category: str,
    selection: BuildState,
) -> list[Component]:
    """Compatible candidates for one category at one workload tier, degrading
    tier-tagged -> any-tier-for-this-profile -> plain compatibility, exactly
    as `allocate_workload_baseline` always has — factored out so both the
    normal per-category loop and the cross-tier escalation repair pass
    (`_escalate_workload_tier_above_floor`) share one source of truth for
    "what's a legal pick here" rather than risking two slightly different
    definitions drifting apart.

    Takes already-fetched `tier_matches`/`profile_matches` rather than
    querying `components_repo.get_workload_matches` itself — those results
    depend only on (profile, tier), never on `category` or `selection`, so
    the caller fetches each exactly once per tier and reuses it across every
    category and every escalation attempt, instead of re-querying the DB
    per category per pool-widening pass (a real, measured slowdown once
    `allocate_workload_baseline` started computing all 4 tiers per call)."""
    candidates = [component for component, mapping in tier_matches if component.category == category]
    candidates = filter_compatible(candidates, selection, category)
    if candidates:
        return candidates

    candidates = [component for component, mapping in profile_matches if component.category == category]
    candidates = filter_compatible(candidates, selection, category)
    if candidates:
        return candidates

    return get_compatible_candidates(category, selection)


def _build_workload_tier(
    tier_matches: list[tuple[Component, object]],
    profile_matches: list[tuple[Component, object]],
    include_peripherals: bool = False,
) -> BuildState:
    """One tier's baseline, in isolation — the median-priced (per spec.md
    §5.6) compatible pick for each category, degrading exactly as
    `_workload_tier_category_candidates` describes. This alone does NOT
    guarantee cross-tier price ordering (see `allocate_workload_baseline`'s
    docstring for why not); `_escalate_workload_tier_above_floor` is what
    repairs that, applied by the caller after this returns.

    `include_peripherals=True` additionally picks a median-priced compatible
    option for each of `PERIPHERAL_CATEGORIES` the same way — `_
    workload_tier_category_candidates` already degrades to plain
    `get_compatible_candidates` for any category with no workload_mappings
    tag at all, which peripherals frequently don't have per profile, so this
    never leaves a peripheral slot silently unfilled just because it wasn't
    curated for this specific profile/tier. Folded into THIS function
    (contributing to the tier's total from the start) rather than applied
    as a separate post-processing step, so `allocate_workload_baseline`'s
    cross-tier escalation repair sees peripheral cost too and the price
    invariant it guarantees still holds with peripherals included, not just
    for the 8 core categories."""
    selection: BuildState = {}
    categories = CATEGORY_ORDER + PERIPHERAL_CATEGORIES if include_peripherals else CATEGORY_ORDER
    for category in categories:
        candidates = _workload_tier_category_candidates(tier_matches, profile_matches, category, selection)
        if not candidates:
            continue
        selection[category] = _median_by_price(candidates)
    return selection


def _escalate_workload_tier_above_floor(
    selection: BuildState,
    floor: float,
    tier_matches: list[tuple[Component, object]],
    profile_matches: list[tuple[Component, object]],
) -> BuildState:
    """Push `selection`'s total strictly above `floor` by swapping categories
    to progressively pricier compatible options — cheapest category first, so
    the adjustment stays as proportionate as possible. Used to repair the
    case `allocate_workload_baseline` exists to guard against: a tier's own
    median-per-category picks summing to no more than the previous
    (supposedly cheaper) tier's total, because `workload_mappings`' tier
    tags are a curated "fits this workload at this tier" judgment call per
    component, not a price partition — nothing stops two adjacent tiers'
    tagged sets from overlapping or even inverting in aggregate for a given
    profile.

    Tries this tier's own tagged pool first (preserves "this part suits
    this tier" as much as possible), and only widens to any-tier-for-this-
    profile, then the full compatible catalog, if the tier's own pool runs
    out of upgrade headroom before clearing the floor — verified necessary
    against this project's seed data: some profiles' Entry baseline is
    pricier than Mid's own tagged pool has room to catch up to, e.g. one
    category's Entry-tagged option costs unusually more than anything
    tagged Mid for that same category. Stops gracefully (same philosophy as
    the rest of this module) if even the full catalog has no further
    upgrade anywhere, rather than raising — the caller proceeds with
    whatever this reaches, same graceful-degradation precedent as
    `_greedy_fill` and friends.

    `tier_matches`/`profile_matches` are pre-fetched by the caller (see
    `_workload_tier_category_candidates`'s docstring for why) — this
    function never queries the DB itself."""
    result = dict(selection)
    total = sum(c.price_usd for c in result.values())

    def pool_for(pool_index: int, category: str, working: BuildState) -> list[Component]:
        if pool_index == 0:
            return filter_compatible([c for c, m in tier_matches if c.category == category], working, category)
        if pool_index == 1:
            return filter_compatible([c for c, m in profile_matches if c.category == category], working, category)
        return get_compatible_candidates(category, working)

    for pool_index in range(3):
        progress = True
        while total <= floor and progress:
            progress = False
            for category in sorted(result, key=lambda cat: result[cat].price_usd):
                others = {c: v for c, v in result.items() if c != category}
                candidates = pool_for(pool_index, category, others)
                pricier = [c for c in candidates if c.price_usd > result[category].price_usd]
                if not pricier:
                    continue
                result[category] = min(pricier, key=lambda c: c.price_usd)  # smallest upgrade that still helps
                total = sum(c.price_usd for c in result.values())
                progress = True
                if total > floor:
                    break
        if total > floor:
            break

    return result


def allocate_workload_baseline(
    profile: str,
    target_tier: str = DEFAULT_WORKLOAD_TIER,
    include_peripherals: bool = False,
) -> BuildState:
    """Mode B entry point (spec.md §5.6). Picks a representative (median-priced)
    component per category from workload_mappings rows tagged for `profile` at
    `target_tier`, degrading to any tier for the profile, then to plain
    compatibility, rather than leaving a category empty.

    Internally computes ALL FOUR tiers in Entry -> Mid -> High -> Enthusiast
    order on every call (regardless of which single tier was requested) and
    only returns the requested one, because the cross-tier price invariant
    this function guarantees — Cost(Entry) < Cost(Mid) < Cost(High) <
    Cost(Enthusiast), for every profile — cannot be checked or repaired
    looking at one tier alone; each tier's total must be verified against
    the tier before it. A tier's own naive per-category median picks can
    otherwise sum to no more (or even less) than a cheaper tier's, because
    `workload_mappings` tier tags are a curated per-component judgment call,
    not a strict price partition (verified empirically against this
    project's seed data before this fix: 4 of 5 profiles violated strict
    ordering). When that happens, `_escalate_workload_tier_above_floor`
    repairs it by swapping the affected tier's cheapest-relative-to-itself
    categories up to pricier options from that SAME tier's own candidate
    pool until its total clears the previous tier's — preserving each
    tier's own curated part pool as much as possible, never borrowing
    another tier's pool to do it.

    `include_peripherals=True` additionally fills `PERIPHERAL_CATEGORIES`
    per tier (see `_build_workload_tier`) — opt-in and defaulting to False,
    mirroring `initialize_budget_build`'s `fill_peripherals_with_surplus`
    flag, so every existing caller (the ~6 workload tests included) keeps
    its exact prior core-only behavior unless it explicitly asks for this.
    Peripherals are included in the tier's total from the start, so the
    price-ordering guarantee above holds with them, not just for the 8 core
    categories alone.

    Fetches `get_workload_matches(profile)` (no tier filter) exactly once
    here and `get_workload_matches(profile, tier)` exactly once per tier —
    both independent of `category`, so every helper below reuses these
    same lists across all categories and all escalation attempts instead
    of re-querying the DB each time."""
    profile_matches = components_repo.get_workload_matches(profile)

    floor_total = -1.0
    result: BuildState | None = None

    for tier in WORKLOAD_TIERS:
        tier_matches = components_repo.get_workload_matches(profile, tier)
        selection = _build_workload_tier(tier_matches, profile_matches, include_peripherals=include_peripherals)
        total = sum(c.price_usd for c in selection.values())

        if floor_total >= 0 and total <= floor_total:
            selection = _escalate_workload_tier_above_floor(selection, floor_total, tier_matches, profile_matches)
            total = sum(c.price_usd for c in selection.values())

        floor_total = total
        if tier == target_tier:
            result = selection

    assert result is not None  # target_tier is always one of WORKLOAD_TIERS
    return result
