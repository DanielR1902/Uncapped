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
from engine.compatibility import BuildState, evaluate_build, resolve_quantity_limit
from engine.solvers import CATEGORY_ORDER, cheapest_fill_cost
from ui.format import format_currency
from ui.state import resolve_effective_quantity_limit

# UI-only conservative display cap used ONLY when engine.compatibility's
# resolve_quantity_limit returns (None, ...) — i.e. no real motherboard/
# catalog data exists to bound this category's quantity (no Motherboard
# picked, no RAM/Storage slot-count field, or a non-NVMe Storage interface
# with no real port-count data in this catalog). This number is never
# presented as a hardware fact — see _resolve_quantity_bound's docstring
# and render_part_picker's caption handling below.
_FALLBACK_QUANTITY_CAP = 4

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


def _resolve_quantity_bound(category: str, build_state: BuildState) -> tuple[int, str, bool]:
    """Thin wrapper around engine.compatibility.resolve_quantity_limit — the
    single source of truth for the real, motherboard-backed max quantity.
    Returns (max_value_for_widget, reason, is_real):
    - is_real=True: max_value_for_widget IS the real motherboard/catalog
      limit (e.g. real ram_slots // modules-per-kit, or real m2_slots for
      NVMe Storage) and `reason` is the engine's own honest explanation.
    - is_real=False: no real motherboard/catalog data exists to bound this
      category's quantity, so max_value_for_widget is this UI's own
      clearly-labeled conservative fallback cap (_FALLBACK_QUANTITY_CAP) —
      never presented to the user as a real hardware fact."""
    max_allowed, reason = resolve_quantity_limit(build_state, category)
    if max_allowed is not None:
        return max_allowed, reason, True
    return _FALLBACK_QUANTITY_CAP, reason, False


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


_QUANTITY_CATEGORIES = ("RAM", "Storage")


def render_part_picker(
    category: str,
    build_state: BuildState,
    candidates: list[Component],
    on_select: Callable[[Component], None],
    on_remove: Callable[[], None] | None = None,
    budget_ceiling: float | None = None,
    quantity: int = 1,
    on_quantity_change: Callable[[int], None] | None = None,
    quantities: dict[str, int] | None = None,
) -> None:
    current = build_state.get(category)
    currency = st.session_state.get("selected_currency", "USD")
    sort_criteria = st.session_state.get("sort_criteria", "Cost")
    ordered = sort_candidates(candidates, build_state, sort_criteria)
    icon = _CATEGORY_ICONS.get(category, "🔧")

    spent_elsewhere: float | None = None
    if budget_ceiling is not None:
        spent_elsewhere = sum(c.price_usd for cat, c in build_state.items() if cat != category)

    with st.container(border=True):
        header_cols = st.columns([3, 1.6, 0.6])
        with header_cols[0]:
            st.markdown(f"{icon} **{category}**")
            if current is not None:
                st.badge("Selected", icon=":material/check_circle:", color="green")
                if category in _QUANTITY_CATEGORIES and quantity > 1:
                    st.caption(
                        f"{current.name} · {format_currency(current.price_usd, currency)} × {quantity} = "
                        f"{format_currency(current.price_usd * quantity, currency)}"
                    )
                else:
                    st.caption(f"{current.name} · {format_currency(current.price_usd, currency)}")
                specs_line = " · ".join(_key_specs(current))
                if specs_line:
                    st.caption(specs_line)
                if category in _QUANTITY_CATEGORIES and on_quantity_change is not None:
                    # Combined physical + budget bound: resolve_effective_quantity_limit
                    # (ui/state.py) folds engine.compatibility.resolve_quantity_limit's
                    # real motherboard/catalog max together with a budget-affordability
                    # max (holding every other category's cost fixed), returning
                    # whichever is tighter as (effective_max, reason, limit_kind).
                    # limit_kind == "none" means NEITHER constraint has real data to
                    # bound this category (per that function's own logic this only
                    # happens when budget_ceiling is also None, since a real ceiling
                    # always yields a real financial max) — fall back to this UI's
                    # own conservative cap exactly as before.
                    effective_max, reason, limit_kind = resolve_effective_quantity_limit(
                        build_state, category, quantities or {}, budget_ceiling, currency
                    )
                    max_qty = effective_max if effective_max is not None else _FALLBACK_QUANTITY_CAP
                    qty_key = f"qty_{category}"

                    # Streamlit widget-key-persistence gotcha: a widget created
                    # with a stable key= persists its value in
                    # st.session_state across reruns, and on every rerun AFTER
                    # the first, Streamlit renders using the EXISTING
                    # session-state value for that key — the value= argument
                    # below is only honored the very first time this widget is
                    # ever created. Since resolve_quantity_limit now uses real
                    # per-module math, swapping in a RAM kit with more modules
                    # per kit (or a Storage pick with a tighter slot count) can
                    # make max_qty legitimately SHRINK between reruns. If the
                    # OLD quantity is still sitting in
                    # st.session_state[qty_key] from before the swap and now
                    # exceeds the NEW (smaller) max_qty, st.number_input raises
                    # a raw StreamlitAPIException ("the default value ... is
                    # outside the bounds") the moment it tries to render. The
                    # fix is to clamp the session-state value down BEFORE
                    # instantiating the widget with that same key (never
                    # after, which raises a different Streamlit exception) —
                    # this silently re-clamps the stepper instead of crashing.
                    number_input_kwargs: dict = {"min_value": 1, "max_value": max_qty, "step": 1, "key": qty_key}
                    if qty_key in st.session_state:
                        if st.session_state[qty_key] > max_qty:
                            st.session_state[qty_key] = max_qty
                    else:
                        # `value=` is only ever honored the very first time
                        # this key is created (see comment above) — passing it
                        # again on later reruns, once st.session_state already
                        # owns the key, is not just redundant but also trips
                        # Streamlit's own "widget had a default value but its
                        # value was also set via the Session State API"
                        # warning on the exact rerun where we clamp above, so
                        # it's only included for that true first-render case.
                        number_input_kwargs["value"] = min(quantity, max_qty)

                    new_quantity = st.number_input("Qty", **number_input_kwargs)
                    if new_quantity != quantity:
                        on_quantity_change(int(new_quantity))
                        st.rerun()

                    if limit_kind == "physical":
                        if new_quantity >= max_qty:
                            st.caption(
                                f"ℹ️ Motherboard limit reached: supports up to {max_qty} of this component type."
                            )
                        else:
                            st.caption(f"ℹ️ Motherboard limit: up to {max_qty} of this component. {reason}")
                    elif limit_kind == "budget":
                        if new_quantity >= max_qty:
                            st.caption("⚠️ Budget ceiling reached for additional units.")
                        else:
                            st.caption(
                                f"💰 Budget allows up to {max_qty} of this component at the current ceiling."
                            )
                    else:
                        # "drive type" matches the directive's own Storage/SATA
                        # wording verbatim; RAM gets the equivalent honest
                        # phrasing for its own no-data cases (e.g. no
                        # Motherboard picked yet).
                        noun = "drive type" if category == "Storage" else "component type"
                        st.caption(
                            f"No motherboard-specific slot data for this {noun} — "
                            "quantity capped at 4 as a general safety limit."
                        )
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
                            st.write(format_currency(candidate.price_usd, currency))
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
                                    st.caption(
                                        f"{format_currency(candidate.price_usd - max_slot_cost, currency)} over budget"
                                    )

        with header_cols[2]:
            if current is not None and on_remove is not None:
                if st.button("✕", key=f"clear_{category}", help=f"Clear {category}", use_container_width=True):
                    on_remove()
                    st.rerun()
