"""Reusable build-summary card for my_builds and community views (spec.md §7.5/§7.6)."""
from __future__ import annotations

from typing import Callable

import streamlit as st

from db.models import Build
from ui.format import humanize_profile


def render_build_card(build: Build, actions: dict[str, Callable[[], None]] | None = None) -> None:
    with st.container(border=True):
        st.markdown(f"#### {build.name}")

        cols = st.columns(4)
        cols[0].metric("Cost", f"${build.total_cost:,.2f}")
        cols[1].metric("Compatibility", f"{build.compatibility_score:.0f}%")
        cols[2].metric(
            "Bottleneck",
            f"{build.bottleneck_percentage:.0f}%" if build.bottleneck_percentage is not None else "—",
        )
        cols[3].metric(
            "Synergy",
            f"{build.synergy_score:.0f}" if build.synergy_score is not None else "—",
        )

        caption = f"{build.creation_mode} build"
        if build.workload_profile:
            caption += f" · {humanize_profile(build.workload_profile)}"
        caption += f" · {build.created_at:%Y-%m-%d}"
        st.caption(caption)

        if actions:
            action_cols = st.columns(len(actions))
            for col, (label, callback) in zip(action_cols, actions.items()):
                if col.button(label, key=f"{label.replace(' ', '_')}_{build.id}", use_container_width=True):
                    callback()
