"""One category's picker: current pick + expandable candidate list, sorted by
st.session_state['sort_criteria'] (spec.md §3 dynamic sorting / §7.4)."""
from __future__ import annotations

from typing import Callable

import streamlit as st

from db.models import Component
from engine import scoring
from engine.compatibility import BuildState, evaluate_build

SORT_OPTIONS = ("Cost", "Compatibility", "ValueIndex")
_MAX_CANDIDATES_SHOWN = 25


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


def render_part_picker(
    category: str,
    build_state: BuildState,
    candidates: list[Component],
    on_select: Callable[[Component], None],
    on_remove: Callable[[], None] | None = None,
) -> None:
    current = build_state.get(category)
    sort_criteria = st.session_state.get("sort_criteria", "Cost")
    ordered = sort_candidates(candidates, build_state, sort_criteria)

    header = f"**{category}**"
    if current is not None:
        header += f" — {current.name} (${current.price_usd:,.2f})"
    st.markdown(header)

    with st.expander(f"Choose {category} ({len(ordered)} option{'s' if len(ordered) != 1 else ''})"):
        if not ordered:
            st.warning("No compatible options for this category given your current picks.")
        for candidate in ordered[:_MAX_CANDIDATES_SHOWN]:
            cols = st.columns([4, 2, 2, 2])
            is_current = current is not None and candidate.id == current.id
            cols[0].write(("✅ " if is_current else "") + candidate.name)
            cols[1].write(f"${candidate.price_usd:,.2f}")
            cols[2].write(f"{scoring.value_index(candidate, ordered, build_state):.0f} val")
            if not is_current and cols[3].button("Select", key=f"select_{category}_{candidate.id}"):
                on_select(candidate)
                st.rerun()

        if current is not None and on_remove is not None:
            if st.button(f"Remove {category}", key=f"remove_{category}"):
                on_remove()
                st.rerun()
