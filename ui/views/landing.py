"""Landing page — explanatory banner + 3 entry buttons + inline auth modal
(spec.md §7.2/§7.3, intent.txt §2)."""
from __future__ import annotations

import streamlit as st

from ui.components.auth_modal import render_auth_modal


def render() -> None:
    st.title("Uncapped")
    st.caption(
        "AI-assisted, spec-driven PC build platform — compatibility, budget, "
        "and synergy, solved for you."
    )
    st.markdown(
        "Uncapped takes the guesswork out of building a PC. Pick a budget, a "
        "workload, or go fully custom — every part list is checked for "
        "compatibility in real time, scored for synergy, and analyzed for "
        "bottlenecks before you spend a cent."
    )

    authenticated = st.session_state.get("auth_user") is not None

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🛠️ Create New PC", key="nav_create_build", use_container_width=True, disabled=not authenticated):
            st.session_state["page"] = "create_build"
            st.rerun()
    with col2:
        if st.button("📂 View Previous Builds", key="nav_my_builds", use_container_width=True, disabled=not authenticated):
            st.session_state["page"] = "my_builds"
            st.rerun()
    with col3:
        if st.button("🌐 Community", key="nav_community", use_container_width=True, disabled=not authenticated):
            st.session_state["page"] = "community"
            st.rerun()

    if not authenticated:
        st.info("Log in or create an account to unlock these features.")
        render_auth_modal()
