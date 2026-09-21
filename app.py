"""Uncapped — Streamlit entrypoint. Router-only; no business logic here (see
CLAUDE.md's module-boundary rules)."""
from __future__ import annotations

import streamlit as st

from auth.session import log_out
from db.database import init_db
from ui import router, state, theme
from ui.components.chat_assistant import render_concierge_widget

st.set_page_config(page_title="Uncapped", page_icon="🖥️", layout="wide")

# Base.metadata.create_all() only creates TABLES that don't already exist —
# safe and idempotent to call on every boot, including against an existing,
# already-populated db/uncapped.db (e.g. from db/seed_demo.py run separately)
# whose schema predates a newly-added model (draft_builds, added this round,
# is the first real case). Without this, a fresh checkout or a DB file that
# was seeded before a schema change would only work after someone remembered
# to run a seed script by hand first — this makes the app self-healing on
# every startup instead of silently depending on that.
init_db()

state.init_session_state()
theme.inject_css()

with st.sidebar:
    user = st.session_state.get("auth_user")
    if user is not None:
        st.markdown(f"**{user['full_name']}**")
        st.caption(f"@{user['username']}")
        st.markdown("---")

        nav_targets = (
            ("🏠 Home", "landing"),
            ("🛠️ Create New PC", "create_build"),
            ("📂 Previous Builds", "my_builds"),
            ("📝 View Drafts", "drafts"),
            ("🌐 Community", "community"),
        )
        for label, target_page in nav_targets:
            if st.button(label, key=f"sidebar_nav_{target_page}", use_container_width=True):
                if st.session_state.get("page") == "create_build" and target_page != "create_build":
                    state.teardown_builder()
                state.navigate_to_page(target_page)
                st.rerun()

        st.markdown("---")

        render_concierge_widget()

        st.markdown('<div class="uncapped-sidebar-spacer"></div>', unsafe_allow_html=True)
        if st.button("Logout", key="logout_button", use_container_width=True):
            if st.session_state.get("page") == "create_build":
                state.teardown_builder()
            log_out()
            st.rerun()
    else:
        st.caption("Not logged in")

router.render()
