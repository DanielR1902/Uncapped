"""Page dispatcher — the auth hard-gate lives here (spec.md §7.2)."""
from __future__ import annotations

import streamlit as st

from ui.views import community, create_build, drafts, landing, my_builds

_VIEWS = {
    "landing": landing.render,
    "create_build": create_build.render,
    "my_builds": my_builds.render,
    "community": community.render,
    "drafts": drafts.render,
}

# Every page except landing requires auth (intent.txt: "all buttons locked" pre-login).
_GATED_PAGES = {"create_build", "my_builds", "community", "drafts"}


def render() -> None:
    if st.session_state.get("auth_user") is None and st.session_state.get("page") in _GATED_PAGES:
        st.session_state["page"] = "landing"

    page = st.session_state.get("page", "landing")
    view = _VIEWS.get(page, landing.render)
    view()
