"""Drafts dashboard — in-progress builds saved (rather than fully committed
as a scored `Build` row) via the explicit "Save as draft" checkbox in
`ui/views/create_build.py`'s manual Save UI (spec.md §7.1/§7.9,
`drafts_repo.save_draft`) — the single, explicit way a draft is created;
leaving Build Studio any other way (`ui.state.teardown_builder`) discards an
unsaved build with no database write. Follows my_builds.py's general shape
(title, list of cards) but reuses a plain st.container(border=True) card
rather than ui/components/build_card.py's render_build_card, since a draft
is not a full scored Build row."""
from __future__ import annotations

import json

import streamlit as st

from auth.session import current_user
from db.models import DraftBuild
from db.repositories import drafts_repo
from ui import state


def _load_into_builder(draft: DraftBuild) -> None:
    new_draft = state.load_components_into_new_draft(
        mode=draft.mode,
        components=json.loads(draft.components_json),
        quantities=json.loads(draft.quantities_json),
        name=draft.name,
    )
    st.session_state["build_draft"] = new_draft
    st.session_state["create_mode"] = draft.mode
    st.session_state["build_draft_analysis"] = None
    st.session_state["page"] = "create_build"
    st.rerun()


def _delete_draft(draft: DraftBuild) -> None:
    drafts_repo.delete_draft(draft.id)
    st.rerun()


def _draft_card(draft: DraftBuild) -> None:
    components = json.loads(draft.components_json)
    confirm_key = f"confirm_delete_draft_{draft.id}"

    with st.container(border=True):
        st.markdown(f"#### {draft.name}")
        st.caption(
            f"{draft.mode} draft · {len(components)} part(s) selected · "
            f"Last saved {draft.updated_at:%Y-%m-%d %H:%M}"
        )

        if st.session_state.get(confirm_key):
            st.badge(f'Delete draft "{draft.name}"? This cannot be undone.', icon=":material/warning:", color="red")
            yes_col, cancel_col = st.columns(2)
            if yes_col.button("Yes, delete", key=f"{confirm_key}_yes", use_container_width=True, type="primary"):
                st.session_state[confirm_key] = False
                _delete_draft(draft)
            if cancel_col.button("Cancel", key=f"{confirm_key}_cancel", use_container_width=True):
                st.session_state[confirm_key] = False
                st.rerun()
            return

        cols = st.columns(2)
        if cols[0].button(
            "Load into Builder", key=f"load_draft_{draft.id}", use_container_width=True, type="primary"
        ):
            _load_into_builder(draft)
        if cols[1].button("Delete", key=f"delete_draft_{draft.id}", use_container_width=True):
            st.session_state[confirm_key] = True
            st.rerun()


def render() -> None:
    st.title("Drafts")

    user = current_user()
    drafts = drafts_repo.get_user_drafts(user["id"])

    if not drafts:
        st.info(
            "No saved drafts yet — check \"Save as draft\" in the Build Studio's "
            "Save section to checkpoint your progress before it's saved as a "
            "finished build."
        )
        return

    for draft in drafts:
        _draft_card(draft)
