"""Reusable build-summary card for my_builds and community views (spec.md §7.5/§7.6)."""
from __future__ import annotations

from typing import Callable

import streamlit as st

from db.models import Build
from ui.format import format_currency, humanize_profile, sanitize_markdown


def render_build_card(
    build: Build,
    actions: dict[str, Callable[[], None]] | None = None,
    confirm_labels: frozenset[str] = frozenset(),
) -> None:
    """`confirm_labels` names actions (e.g. "Delete") that must show a
    Yes/Cancel step before firing their callback — a destructive action
    should never be a single misclick away."""
    with st.container(border=True):
        st.markdown(sanitize_markdown(f"#### {build.name}"))

        currency = st.session_state.get("selected_currency", "USD")
        cols = st.columns(4)
        cols[0].metric("Cost", format_currency(build.total_cost, currency), border=True)
        cols[1].metric("Compatibility", f"{build.compatibility_score:.0f}%", border=True)
        cols[2].metric(
            "Bottleneck",
            f"{build.bottleneck_percentage:.0f}%" if build.bottleneck_percentage is not None else "—",
            border=True,
        )
        cols[3].metric(
            "Synergy",
            f"{build.synergy_score:.0f}" if build.synergy_score is not None else "—",
            border=True,
        )

        caption = f"{build.creation_mode} build"
        if build.workload_profile:
            caption += f" · {humanize_profile(build.workload_profile)}"
        if build.workload_tier is not None:
            caption += f" · Tier: {build.workload_tier}"
        caption += f" · {build.created_at:%Y-%m-%d}"
        st.caption(caption)

        if not actions:
            return

        pending = next(
            (
                (label, f"confirm_{label}_{build.id}")
                for label in confirm_labels
                if st.session_state.get(f"confirm_{label}_{build.id}")
            ),
            None,
        )
        if pending is not None:
            pending_label, confirm_key = pending
            st.badge(f'Delete "{build.name}"? This cannot be undone.', icon=":material/warning:", color="red")
            yes_col, cancel_col = st.columns(2)
            if yes_col.button("Yes, delete", key=f"{confirm_key}_yes", use_container_width=True, type="primary"):
                st.session_state[confirm_key] = False
                actions[pending_label]()
            if cancel_col.button("Cancel", key=f"{confirm_key}_cancel", use_container_width=True):
                st.session_state[confirm_key] = False
                st.rerun()
            return

        action_cols = st.columns(len(actions))
        for col, (label, callback) in zip(action_cols, actions.items()):
            button_key = f"{label.replace(' ', '_')}_{build.id}"
            is_danger = label in confirm_labels
            if col.button(label, key=button_key, use_container_width=True, type="secondary"):
                if is_danger:
                    st.session_state[f"confirm_{label}_{build.id}"] = True
                    st.rerun()
                else:
                    callback()
