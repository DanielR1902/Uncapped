"""Session-state key definitions/defaults, and the build_draft <-> BuildState
bridge (spec.md §7.1). `router.py` owns page dispatch; this module owns state
shape, so every view reads/writes session state the same way.
"""
from __future__ import annotations

import streamlit as st

from db.models import Component
from db.repositories import components_repo
from engine.compatibility import BuildState

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
}


def init_session_state() -> None:
    for key, default in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = default


def new_build_draft(creation_mode: str | None = None) -> dict:
    return {
        "name": "",
        "creation_mode": creation_mode,
        "workload_profile": None,
        "tier": "Mid",
        "budget_ceiling": None,
        "components": {},  # category -> component_id
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
    _invalidate_analysis()


def _invalidate_analysis() -> None:
    """Any change to which components are picked makes the last synergy/
    bottleneck analysis stale — clear it so the UI doesn't keep showing scores
    for a build that no longer matches what's on screen (it'll say "select at
    least two components" or need a fresh "Analyze" click instead)."""
    st.session_state["build_draft_analysis"] = None


def build_total_cost(build_state: BuildState) -> float:
    return sum(component.price_usd for component in build_state.values())
