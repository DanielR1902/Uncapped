"""Previous Builds dashboard (spec.md §7.5)."""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.models import WORKLOAD_PROFILES
from db.repositories import builds_repo, community_repo
from ui import state, theme
from ui.components.build_card import render_build_card
from ui.format import humanize_profile

_SORT_LABELS = {
    "cost_desc": "Cost (High to Low)",
    "cost_asc": "Cost (Low to High)",
    "date": "Date",
}


def _clone_into_studio(build) -> None:
    draft = state.new_build_draft(build.creation_mode)
    draft["name"] = f"{build.name} (copy)"
    draft["workload_profile"] = build.workload_profile
    draft["budget_ceiling"] = build.budget_ceiling
    draft["components"] = {bc.category: bc.component_id for bc in build.components}

    st.session_state["build_draft"] = draft
    st.session_state["create_mode"] = build.creation_mode
    st.session_state["build_draft_analysis"] = None
    # Same as ui/views/community.py::_fork_into_studio: writing
    # draft["components"] as a plain dict literal bypasses ui.state.
    # set_component (the usual place this flag gets set). Set explicitly so
    # a cloned/edited build is correctly tracked as having unsaved picks.
    st.session_state["has_unsaved_build_changes"] = True
    st.session_state["page"] = "create_build"
    st.rerun()


def _share_to_community(build) -> None:
    builds_repo.set_public(build.id, True)
    community_repo.create_post(build.id, current_user()["id"], build.name, None)
    st.success(f'"{build.name}" shared to Community!')
    st.rerun()


def _delete_build(build) -> None:
    builds_repo.delete_build(build.id)
    st.success(f'"{build.name}" deleted.')
    st.rerun()


_DELETE_LABEL = "Delete"


def _card_actions(build) -> dict:
    return {
        "Clone": lambda b=build: _clone_into_studio(b),
        "Edit": lambda b=build: _clone_into_studio(b),
        "Share to Community": lambda b=build: _share_to_community(b),
        _DELETE_LABEL: lambda b=build: _delete_build(b),
    }


def render() -> None:
    st.title("Previous Builds")

    user = current_user()

    view_mode = st.radio(
        "View",
        ("Group by Workload Profile", "Global Sort"),
        horizontal=True,
        key="my_builds_view_mode",
    )

    if view_mode == "Group by Workload Profile":
        builds = builds_repo.get_builds_for_user(user["id"], builds_repo.BuildsFilter(sort="date"))
        if not builds:
            st.info("You haven't saved any builds yet — head to the Build Studio to create one.")
            return

        grouped: dict[str, list] = {}
        for build in builds:
            group_name = humanize_profile(build.workload_profile) if build.workload_profile else "Unassigned"
            grouped.setdefault(group_name, []).append(build)

        for group_name, group_builds in grouped.items():
            st.markdown(theme.section_header(group_name), unsafe_allow_html=True)
            for build in group_builds:
                render_build_card(build, actions=_card_actions(build), confirm_labels=frozenset({_DELETE_LABEL}))
            st.divider()
    else:
        sort_label = st.selectbox("Sort by", list(_SORT_LABELS.values()), key="my_builds_sort")
        sort_key = next(key for key, label in _SORT_LABELS.items() if label == sort_label)

        builds = builds_repo.get_builds_for_user(user["id"], builds_repo.BuildsFilter(sort=sort_key))
        if not builds:
            st.info("You haven't saved any builds yet — head to the Build Studio to create one.")
            return

        cols = st.columns(2)
        for index, build in enumerate(builds):
            with cols[index % 2]:
                render_build_card(build, actions=_card_actions(build), confirm_labels=frozenset({_DELETE_LABEL}))
