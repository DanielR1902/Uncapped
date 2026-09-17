"""Session-state key definitions/defaults, and the build_draft <-> BuildState
bridge (spec.md §7.1). `router.py` owns page dispatch; this module owns state
shape, so every view reads/writes session state the same way.
"""
from __future__ import annotations

import copy

import streamlit as st

from db.models import Component
from db.repositories import components_repo
from engine.compatibility import BuildState, resolve_quantity_limit

DEFAULT_SORT_CRITERIA = "Cost"

_DEFAULTS = {
    "page": "landing",
    "auth_user": None,
    "auth_mode": None,
    "auth_error": {},
    "create_mode": None,
    "build_draft": None,  # set lazily via new_build_draft() once a mode is chosen
    "build_draft_analysis": None,
    "sort_criteria": DEFAULT_SORT_CRITERIA,
    "previous_builds_filter": {"workload_profile": None, "sort": "date"},
    "selected_post_id": None,
    "fork_source_build_id": None,
    "stretch_applied_keys": set(),  # per-cache-key lock for the advisory's one-time stretch-upgrade apply button
    "concierge_messages": [],  # sidebar AI Concierge chat history: [{"role": "user"|"assistant", "content": str}, ...]
}


def init_session_state() -> None:
    """`_DEFAULTS`' dict/set-valued entries (`auth_error`, `previous_builds_filter`,
    `stretch_applied_keys`) are module-level objects created once at import
    time — assigning them directly would hand every Streamlit session (every
    concurrent user, in a real multi-session server process) a reference to
    the SAME mutable object. `auth_error`/`previous_builds_filter` happen to
    always be wholesale-reassigned elsewhere rather than mutated in place, so
    that latent bug never surfaced for them, but `stretch_applied_keys.add(...)`
    genuinely mutates in place — without this copy, one user applying a
    stretch upgrade would silently lock the button for every other session
    too. `copy.deepcopy` on every default (not just the mutable ones) is
    simplest and harmless for the immutable ones (str/None)."""
    for key, default in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(default)


def new_build_draft(creation_mode: str | None = None) -> dict:
    return {
        "name": "",
        "creation_mode": creation_mode,
        "workload_profile": None,
        "tier": "Mid",
        "budget_ceiling": None,
        "components": {},  # category -> component_id
        "quantities": {},  # category -> count (meaningful only for RAM/Storage, default 1)
    }


def resolve_build_state(build_draft: dict | None) -> BuildState:
    """Fetch full Component rows for whatever's pinned in build_draft. Missing
    or since-removed component ids are silently skipped rather than raising —
    a stale pick shouldn't crash the whole build view."""
    build_state: BuildState = {}
    if not build_draft:
        return build_state
    for category, component_id in build_draft.get("components", {}).items():
        component = components_repo.get_by_id(component_id)
        if component is not None:
            build_state[category] = component
    return build_state


def set_component(build_draft: dict, category: str, component: Component) -> None:
    build_draft.setdefault("components", {})[category] = component.id
    _invalidate_analysis()


def remove_component(build_draft: dict, category: str) -> None:
    build_draft.get("components", {}).pop(category, None)
    # A freshly-emptied slot starts back at quantity 1 if it's ever re-filled,
    # rather than inheriting a stale multiplier from whatever was there before.
    build_draft.get("quantities", {}).pop(category, None)
    _invalidate_analysis()


def get_quantity(build_draft: dict, category: str) -> int:
    return build_draft.get("quantities", {}).get(category, 1)


def set_quantity(build_draft: dict, category: str, quantity: int) -> None:
    clamped = max(1, quantity)
    build_draft.setdefault("quantities", {})[category] = clamped
    # A quantity change affects cost/compatibility just like a component
    # swap, so the last synergy/bottleneck read must go stale too.
    _invalidate_analysis()


def _invalidate_analysis() -> None:
    """Any change to which components are picked makes the last synergy/
    bottleneck analysis stale — clear it so the UI doesn't keep showing scores
    for a build that no longer matches what's on screen (it'll say "select at
    least two components" or need a fresh "Analyze" click instead)."""
    st.session_state["build_draft_analysis"] = None


def build_total_cost(build_state: BuildState, quantities: dict[str, int] | None = None) -> float:
    return sum(component.price_usd * (quantities or {}).get(category, 1) for category, component in build_state.items())


def resolve_effective_quantity_limit(
    build_state: BuildState,
    category: str,
    quantities: dict[str, int],
    budget_ceiling: float | None,
) -> tuple[int | None, str, str]:
    """Combines the real physical slot limit (engine.compatibility.
    resolve_quantity_limit) with a budget-affordability limit, returning
    whichever is tighter. Returns (effective_max, reason, limit_kind) where
    limit_kind is "physical" | "budget" | "none" (neither constraint has
    real data to bound this category — caller applies its own UI-only
    fallback cap in that case, exactly as it already does for the
    physical-only function).

    budget_ceiling=None means no financial constraint applies (Workload/Free
    modes, or Budget mode with no ceiling set yet) — the same convention
    ui/components/part_picker.py already uses when calling render_part_picker
    (it passes build_draft.get("budget_ceiling"), which is None outside
    Budget mode). effective_max is always >= 1 when it is not None.

    The financial calculation: holding every OTHER category's cost fixed at
    its current (quantity-scaled) total, how many units of THIS category's
    currently-selected component can the remaining budget afford? This does
    NOT reserve budget for other still-empty categories (a more elaborate
    concern the existing "Minimum Reserve Threshold" logic in
    ui/components/part_picker.py already handles for NEW component
    selection) — this function only answers "would incrementing this
    quantity, with everything else held fixed, exceed the ceiling."
    """
    physical_max, physical_reason = resolve_quantity_limit(build_state, category)

    financial_max: int | None = None
    financial_reason = ""
    component = build_state.get(category)
    if budget_ceiling is not None and component is not None and component.price_usd > 0:
        current_qty = quantities.get(category, 1)
        total_cost = build_total_cost(build_state, quantities)
        other_components_cost = total_cost - (current_qty * component.price_usd)
        remaining_for_category = budget_ceiling - other_components_cost
        financial_max = max(1, int(remaining_for_category // component.price_usd))
        financial_reason = (
            f"Budget limit reached: cannot afford additional units without exceeding "
            f"{budget_ceiling:,.2f} USD."
        )

    candidates = [v for v in (physical_max, financial_max) if v is not None]
    if not candidates:
        return None, "", "none"

    effective_max = max(1, min(candidates))
    if physical_max is not None and effective_max == physical_max and (
        financial_max is None or physical_max <= financial_max
    ):
        return effective_max, physical_reason, "physical"
    return effective_max, financial_reason, "budget"
