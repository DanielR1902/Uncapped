"""Previous Builds dashboard (spec.md §7.5)."""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.models import WORKLOAD_PROFILES
from db.repositories import builds_repo, community_repo
from ui import state, theme
from ui.components.build_card import render_build_card
from ui.format import humanize_profile, sanitize_markdown

_SORT_LABELS = {
    "cost_desc": "Cost (High to Low)",
    "cost_asc": "Cost (Low to High)",
    "date": "Date",
}


def _clone_into_studio(build) -> None:
    draft = state.load_components_into_new_draft(
        mode=build.creation_mode,
        components={bc.category: bc.component_id for bc in build.components},
        # A real, confirmed gap fixed alongside this refactor (same as
        # community.py::_fork_into_studio): the previous direct-dict-literal
        # version never carried quantities over, so a RAM/Storage build with
        # 2x+ units silently reverted to 1x on clone.
        quantities={bc.category: bc.quantity for bc in build.components},
        name=f"{build.name} (copy)",
    )
    draft["workload_profile"] = build.workload_profile
    draft["budget_ceiling"] = build.budget_ceiling

    st.session_state["build_draft"] = draft
    st.session_state["create_mode"] = build.creation_mode
    st.session_state["build_draft_analysis"] = None
    st.session_state["page"] = "create_build"
    st.rerun()


_FLAIR_OPTIONS = ("Rate My Build", "Looking for Help")
_SHARE_LABEL = "Share to Community"
_PUBLISH_FORM_KEY_PREFIX = "publish_form_open_"


def _open_publish_form(build) -> None:
    """Clicking "Share to Community" no longer publishes immediately — it
    opens the inline description/flair form below the card (rendered by
    `_maybe_render_publish_form`), matching the same "confirm before this
    real, public-facing write happens" discipline `_DELETE_LABEL`'s own
    confirm step already uses on this same card."""
    st.session_state[f"{_PUBLISH_FORM_KEY_PREFIX}{build.id}"] = True
    st.rerun()


def _confirm_publish(build, description: str, flair: str) -> None:
    builds_repo.set_public(build.id, True)
    community_repo.create_post(build.id, current_user()["id"], build.name, description or None, flair=flair)
    st.session_state[f"{_PUBLISH_FORM_KEY_PREFIX}{build.id}"] = False
    st.success(f'"{build.name}" published to Community!')
    st.rerun()


def _maybe_render_publish_form(build) -> None:
    """Rendered directly below `build`'s card (never inside `build_card.py`
    itself — that component is shared with `community.py` and has no notion
    of publishing) whenever `_open_publish_form` staged this build's form
    open. Publishing here leaves the build's own `Previous Builds` row
    completely untouched (`builds_repo.set_public` only flips a visibility
    flag) — a real, separate `community_posts` row is what actually gets
    created (spec.md §3.6/§7.5)."""
    form_key = f"{_PUBLISH_FORM_KEY_PREFIX}{build.id}"
    if not st.session_state.get(form_key):
        return
    with st.container(border=True):
        st.markdown(sanitize_markdown(f'**Publish "{build.name}" to Community**'))
        description = st.text_area(
            "Post Description",
            placeholder="Share your thoughts, use-case, or a question about this build...",
            key=f"publish_description_{build.id}",
        )
        flair = st.selectbox(
            "Category / Intent", _FLAIR_OPTIONS, key=f"publish_flair_{build.id}",
        )
        st.markdown(theme.flair_badge(flair), unsafe_allow_html=True)
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button(
            "Confirm & Publish", key=f"publish_confirm_{build.id}", type="primary", use_container_width=True,
        ):
            _confirm_publish(build, description, flair)
        if cancel_col.button("Cancel", key=f"publish_cancel_{build.id}", use_container_width=True):
            st.session_state[form_key] = False
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
        _SHARE_LABEL: lambda b=build: _open_publish_form(b),
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
                _maybe_render_publish_form(build)
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
                _maybe_render_publish_form(build)
