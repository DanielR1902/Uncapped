"""Uncapped — Streamlit entrypoint. Router-only; no business logic here (see
CLAUDE.md's module-boundary rules)."""
from __future__ import annotations

import streamlit as st

from auth.session import log_out
from ui import router, state, theme
from ui.components.chat_assistant import render_concierge_widget

st.set_page_config(page_title="Uncapped", page_icon="🖥️", layout="wide")

state.init_session_state()
theme.inject_css()

with st.sidebar:
    user = st.session_state.get("auth_user")
    if user is not None:
        st.markdown(f"**{user['full_name']}**")
        st.caption(f"@{user['username']}")
        st.markdown("---")

        nav_targets = (
            ("🛠️ Create New PC", "create_build"),
            ("📂 Previous Builds", "my_builds"),
            ("🌐 Community", "community"),
        )
        for label, target_page in nav_targets:
            if st.button(label, key=f"sidebar_nav_{target_page}", use_container_width=True):
                st.session_state["page"] = target_page
                st.rerun()

        render_concierge_widget()

        st.markdown('<div class="uncapped-sidebar-spacer"></div>', unsafe_allow_html=True)
        if st.button("Logout", key="logout_button", use_container_width=True):
            log_out()
            st.rerun()
    else:
        st.caption("Not logged in")

router.render()
