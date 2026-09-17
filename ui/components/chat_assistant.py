"""Sidebar AI Concierge & Site Navigator widget (spec.md §7.7 / §6.7).

Renders a chat-style expander in the authenticated sidebar backed by
`llm.concierge.get_concierge_response`. This module pre-fetches the full
catalog and community feed (the only two things `llm/concierge.py` itself
is forbidden from querying) and hands them to the LLM on every turn. It owns
no compatibility/scoring math and no SQL beyond calling the two allowed
repository read functions.

`load_build`/`modify_build`/`navigate` actions returned by a turn are applied
IMMEDIATELY, with no confirmation click — this only ever mutates the
ephemeral, uncommitted `build_draft` session state, nothing is persisted to
the database until the user later explicitly clicks "Save build" in
`ui/views/create_build.py`'s normal flow, so there is nothing here that isn't
already fully visible and re-editable the instant it lands.
"""
from __future__ import annotations

import streamlit as st

from db.models import COMPONENT_CATEGORIES
from db.repositories import community_repo, components_repo
from llm.concierge import get_concierge_response
from ui import state
from ui.format import sanitize_markdown

# A couple of cheap, generically-useful key specs per component, when present
# — matching the compact style of llm/advisory.py's own _component_summary,
# not a full spec dump.
_SUMMARY_SPEC_FIELDS = ("socket", "ram_type", "capacity_gb", "interface")


def _catalog_summary() -> list[dict]:
    """Full catalog (~143 real components) — compact enough to embed whole in
    one concierge payload every message."""
    summary: list[dict] = []
    for category in COMPONENT_CATEGORIES:
        for component in components_repo.get_by_category(category):
            entry = {
                "id": component.id,
                "category": category,
                "name": component.name,
                "price_usd": component.price_usd,
            }
            for field in _SUMMARY_SPEC_FIELDS:
                value = getattr(component, field, None)
                if value is not None:
                    entry[field] = value
            summary.append(entry)
    return summary


def _community_summary() -> list[dict]:
    posts = community_repo.get_feed()
    return [
        {
            "post_id": post.id,
            "build_id": post.build_id,
            "title": post.title,
            "creation_mode": post.build.creation_mode,
            "workload_profile": post.build.workload_profile,
            "workload_tier": post.build.workload_tier,
            "total_cost": post.build.total_cost,
            "author_notes": post.author_notes,
        }
        for post in posts
    ]


def _current_build_context() -> dict | None:
    """Snapshot of the user's currently active build draft, handed to the
    LLM so it can support incremental `modify_build` requests. `None` when
    there's no draft in progress at all (mode not chosen yet) or it resolves
    to no real components (e.g. every previously-picked id has since been
    removed from the catalog)."""
    build_draft = st.session_state.get("build_draft")
    if not build_draft:
        return None
    build_state = state.resolve_build_state(build_draft)
    if not build_state:
        return None
    return {
        "mode": build_draft.get("creation_mode"),
        "components": {
            category: {"id": component.id, "name": component.name, "price_usd": component.price_usd}
            for category, component in build_state.items()
        },
        "quantities": build_draft.get("quantities", {}),
    }


def _apply_concierge_action(action: dict | None) -> None:
    """Applies a `load_build`/`modify_build`/`navigate` action immediately —
    no confirmation step. `load_build` always starts a fresh Free-mode draft
    (mirroring `ui/views/community.py::_fork_into_studio`'s shape); `modify_build`
    patches the existing draft in place, leaving every unmentioned category
    untouched. Every id in `action["components"]` is already guaranteed real
    by `llm/concierge.py`'s own zero-hallucination guard, so the defensive
    `components_repo.get_by_id` re-check here is belt-and-suspenders, not
    load-bearing — mirroring `ui/views/create_build.py::_apply_swaps`'s
    identical defensive pattern. Requested quantities are NOT trusted
    verbatim: `llm/concierge.py` has no `engine`/`db` access to clamp them to
    a real physical/budget limit, so this function re-derives the same
    effective ceiling the manual quantity stepper already enforces
    (`ui.state.resolve_effective_quantity_limit`) before ever writing one."""
    if action is None:
        return
    action_type = action.get("type")

    if action_type == "navigate":
        page = action.get("navigate_to")
        if page in ("create_build", "my_builds", "community"):
            st.session_state["page"] = page
        return

    if action_type in ("load_build", "modify_build"):
        build_draft = st.session_state.get("build_draft")
        # A modify_build with nothing to modify against starts a fresh Free
        # draft too, same as load_build — a defensive fallback for the (should
        # be rare, since the prompt tells the model not to do this) case of a
        # modify_build arriving with no real active draft.
        if action_type == "load_build" or not build_draft:
            build_draft = state.new_build_draft("Free")

        for category, component_id in action.get("components", {}).items():
            component = components_repo.get_by_id(component_id)
            if component is not None:
                state.set_component(build_draft, category, component)

        requested_quantities = action.get("quantities", {})
        if requested_quantities:
            build_state = state.resolve_build_state(build_draft)
            budget_ceiling = build_draft.get("budget_ceiling")
            for category, requested_qty in requested_quantities.items():
                effective_max, _reason, _kind = state.resolve_effective_quantity_limit(
                    build_state, category, build_draft.get("quantities", {}), budget_ceiling
                )
                clamped = min(requested_qty, effective_max) if effective_max is not None else requested_qty
                state.set_quantity(build_draft, category, max(1, clamped))

        st.session_state["build_draft"] = build_draft
        st.session_state["create_mode"] = build_draft.get("creation_mode") or "Free"
        st.session_state["build_draft_analysis"] = None
        st.session_state["page"] = "create_build"


def render_concierge_widget() -> None:
    """Called from app.py's sidebar, only when a user is authenticated."""
    with st.expander("💬 AI Concierge & Site Navigator", expanded=False):
        for message in st.session_state["concierge_messages"]:
            with st.chat_message(message["role"]):
                st.markdown(sanitize_markdown(message["content"]))

        user_input = st.chat_input(
            "Ask about parts, community builds, or say 'build me a PC with...'",
            key="concierge_chat_input",
        )
        if user_input:
            st.session_state["concierge_messages"].append({"role": "user", "content": user_input})
            history = [
                {"role": m["role"], "content": m["content"]}
                for m in st.session_state["concierge_messages"][:-1]
            ]
            with st.spinner("Thinking..."):
                result = get_concierge_response(
                    user_input,
                    history,
                    _catalog_summary(),
                    _community_summary(),
                    current_build_context=_current_build_context(),
                )
            st.session_state["concierge_messages"].append({"role": "assistant", "content": result["reply"]})
            _apply_concierge_action(result.get("action"))
            st.rerun()
