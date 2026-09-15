"""One category's slot: a status-badged card (Selected vs Empty) with a
st.popover picker drawer of sortable, spec-rich candidate cards. Per spec.md
§3 (dynamic sorting), §7.4 (Build Studio), and the Studio redesign task
(structured slots + refined picker drawer).
"""
from __future__ import annotations

import json
from typing import Callable

import streamlit as st

from db.models import Component
from engine import scoring
from engine.compatibility import BuildState, evaluate_build
from engine.solvers import CATEGORY_ORDER, cheapest_fill_cost

SORT_OPTIONS = ("Cost", "Compatibility", "ValueIndex")
_MAX_CANDIDATES_SHOWN = 25

_CATEGORY_ICONS = {
    "CPU": "🧠",
    "GPU": "🎮",
    "Motherboard": "🔌",
    "RAM": "🧬",
    "Storage": "💾",
    "PSU": "🔋",
    "Case": "🗄️",
    "Cooler": "❄️",
    "NetworkCard": "📶",
    "SoundCard": "🔊",
    "OpticalDrive": "💿",
}


def _compatibility_contribution(candidate: Component, build_state: BuildState) -> float:
    hypothetical = dict(build_state)
    hypothetical[candidate.category] = candidate
    return evaluate_build(hypothetical).compatibility_score


def sort_candidates(candidates: list[Component], build_state: BuildState, sort_criteria: str) -> list[Component]:
    if sort_criteria == "Compatibility":
        return sorted(candidates, key=lambda c: _compatibility_contribution(c, build_state), reverse=True)
    if sort_criteria == "ValueIndex":
        return sorted(candidates, key=lambda c: scoring.value_index(c, candidates, build_state), reverse=True)
    return sorted(candidates, key=lambda c: c.price_usd)  # "Cost", and the default


def _key_specs(component: Component) -> list[str]:
    """Short "at a glance" spec chips (TDP, form factor, key dimensions) —
    whichever apply to this component's category, capped at 3 to stay
    scannable in a compact card."""
    specs = json.loads(component.specs_json) if component.specs_json else {}
    chips: list[str] = []

    if component.socket:
        chips.append(f"Socket {component.socket}")
    if component.tdp_watts:
        chips.append(f"{component.tdp_watts}W TDP")
    if component.wattage_capacity:
        chips.append(f"{component.wattage_capacity}W")
    if component.ram_type:
        chips.append(component.ram_type)
    if component.capacity_gb:
        chips.append(f"{component.capacity_gb}GB")
    if component.interface:
        chips.append(component.interface)
    if component.form_factor:
        chips.append(component.form_factor)
    if component.chipset:
        chips.append(component.chipset)
    if "vram_gb" in specs:
        chips.append(f"{specs['vram_gb']}GB VRAM")
    if "length_mm" in specs:
        chips.append(f"{specs['length_mm']}mm long")
    if "height_mm" in specs:
        chips.append(f"{specs['height_mm']}mm tall")
    if "radiator_size_mm" in specs:
        chips.append(f"{specs['radiator_size_mm']}mm radiator")
    if component.max_gpu_length_mm:
        chips.append(f"fits GPU ≤{component.max_gpu_length_mm}mm")
    if component.max_cooler_height_mm:
        chips.append(f"cooler ≤{component.max_cooler_height_mm}mm")

    return chips[:3]


def _min_reserve_for_other_slots(category: str, build_state: BuildState, candidate: Component) -> float:
    """Cheapest possible cost to fill every OTHER core category that's still
    empty (excluding `category`, the slot currently being picked), GIVEN
    `candidate` is the pick for `category`. Compatibility constraints
    (socket, form factor, wattage, max lengths) can shift what's cheapest
    for the remaining categories depending on which specific candidate is
    chosen here — an ITX Case forces a pricier SFX PSU than an ATX Case
    would, for instance — so the reserve must be recomputed per-candidate,
    not once per slot using whatever's currently (or not) selected there.
    Delegates to engine.solvers.cheapest_fill_cost — the exact same
    "cheapest possible fill" computation the Budget solver itself uses —
    so this threshold can never silently drift from what the solver
    actually considers achievable. Ensures picking an expensive/constraining
    part for THIS slot can't leave too little to ever complete the other 7 —
    the "Minimum Reserve Threshold" build-safety check."""
    hypothetical = dict(build_state)
    hypothetical[category] = candidate
    other_categories = [c for c in CATEGORY_ORDER if c != category]
    return cheapest_fill_cost(hypothetical, other_categories)


def render_part_picker(
    category: str,
    build_state: BuildState,
    candidates: list[Component],
    on_select: Callable[[Component], None],
    on_remove: Callable[[], None] | None = None,
    budget_ceiling: float | None = None,
) -> None:
    current = build_state.get(category)
    sort_criteria = st.session_state.get("sort_criteria", "Cost")
    ordered = sort_candidates(candidates, build_state, sort_criteria)
    icon = _CATEGORY_ICONS.get(category, "🔧")

    spent_elsewhere: float | None = None
    if budget_ceiling is not None:
        spent_elsewhere = sum(c.price_usd for cat, c in build_state.items() if cat != category)

    with st.container(border=True):
        header_cols = st.columns([3, 2])
        with header_cols[0]:
            st.markdown(f"{icon} **{category}**")
            if current is not None:
                st.badge("Selected", icon=":material/check_circle:", color="green")
                st.caption(f"{current.name} · ${current.price_usd:,.2f}")
                specs_line = " · ".join(_key_specs(current))
                if specs_line:
                    st.caption(specs_line)
            else:
                st.badge("Empty", icon=":material/radio_button_unchecked:", color="gray")
                st.caption("Not selected yet")

        with header_cols[1]:
            popover_label = f"Change {category}" if current is not None else f"Choose {category}"
            with st.popover(popover_label, use_container_width=True):
                st.caption(
                    f"Sorted by {sort_criteria} · {len(ordered)} option{'s' if len(ordered) != 1 else ''}"
                )
                if not ordered:
                    st.warning("No compatible options for this category given your current picks.")

                for candidate in ordered[:_MAX_CANDIDATES_SHOWN]:
                    is_current = current is not None and candidate.id == current.id
                    with st.container(border=True):
                        row = st.columns([5, 2, 2])
                        with row[0]:
                            label = ("✅ " if is_current else "") + f"**{candidate.name}**"
                            st.markdown(label)
                            specs_line = " · ".join(_key_specs(candidate))
                            if specs_line:
                                st.caption(specs_line)
                        with row[1]:
                            st.write(f"${candidate.price_usd:,.2f}")
                            st.caption(f"Value {scoring.value_index(candidate, ordered, build_state):.0f}")
                        with row[2]:
                            max_slot_cost = None
                            if spent_elsewhere is not None:
                                min_reserve = _min_reserve_for_other_slots(category, build_state, candidate)
                                max_slot_cost = budget_ceiling - spent_elsewhere - min_reserve
                            over_budget = max_slot_cost is not None and candidate.price_usd > max_slot_cost
                            if not is_current:
                                if st.button(
                                    "Select",
                                    key=f"select_{category}_{candidate.id}",
                                    use_container_width=True,
                                    disabled=over_budget,
                                ):
                                    on_select(candidate)
                                    st.rerun()
                                if over_budget:
                                    st.caption(f"${candidate.price_usd - max_slot_cost:,.2f} over budget")

                if current is not None and on_remove is not None:
                    st.divider()
                    if st.button(f"Remove {category}", key=f"remove_{category}", use_container_width=True):
                        on_remove()
                        st.rerun()
