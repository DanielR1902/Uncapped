"""Uncapped — Streamlit entrypoint. Router-only; no business logic here (see
CLAUDE.md's module-boundary rules)."""
from __future__ import annotations

import streamlit as st

from auth.session import log_out
from db.database import init_db
from ui import router, state, theme
from ui.components.chat_assistant import render_concierge_widget
from ui.format import CURRENCY_CODES, currency_label

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

        # One-shot Concierge-staged currency switch (ui/components/chat_assistant.py,
        # a `currency_switch` response field applied here — see spec.md §6.7/§7.10)
        # — MUST run before the selectbox below is instantiated: writing directly to
        # a widget's own session-state key (`selected_currency`) AFTER that widget
        # has already rendered in the current script pass raises
        # StreamlitWidgetAlreadyInstantiatedError, the same class of gotcha
        # `ui/components/part_picker.py`'s quantity-stepper pre-clamp already works
        # around (`ui/CLAUDE.md`). `st.session_state.pop` (not `.get`) makes this
        # strictly one-shot, matching `pending_community_filters`'s own precedent.
        pending_currency = st.session_state.pop("pending_currency_switch", None)
        if pending_currency is not None:
            st.session_state["selected_currency"] = pending_currency

        # Global currency display preference (spec.md §7.7/§6.7, ui/format.py). Pure
        # display-layer: every stored/compared price stays USD regardless of this
        # choice. `key="selected_currency"` writes directly into the canonical
        # session-state key every view/the Concierge already reads — a plain
        # `st.selectbox` already triggers a full script rerun on its own the instant
        # its value changes, so no separate explicit st.rerun() is needed here.
        #
        # Deliberately rendered BEFORE the nav-buttons loop below (not after it):
        # a nav button's own click handler calls st.rerun() immediately upon a
        # match, which aborts the script before reaching anything further down —
        # a keyed widget that's skipped that way on one pass and only reached again
        # on the NEXT (rerun-triggered) pass was observed, live, losing track of its
        # prior session-state value and re-showing its index=0 default instead of
        # what the user had actually selected. Placing it ahead of any early-exit
        # button sidesteps that entirely, since it's always reached every pass.
        st.selectbox(
            "Currency",
            CURRENCY_CODES,
            key="selected_currency",
            format_func=currency_label,
        )

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
