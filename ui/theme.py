"""Color/typography constants + CSS injection (spec.md §7.7). Single source of
truth — no hardcoded hex codes anywhere else in ui/."""
from __future__ import annotations

import streamlit as st

BACKGROUND = "#0E1117"
SURFACE = "#161B22"
PRIMARY_ACCENT = "#3DDC97"
WARNING_ACCENT = "#F2B134"
DANGER_ACCENT = "#E5484D"
TEXT_PRIMARY = "#E6EDF3"
TEXT_MUTED = "#8B949E"

_TAG_COLORS = {
    "danger": DANGER_ACCENT,
    "success": PRIMARY_ACCENT,
    "warning": WARNING_ACCENT,
}


def tag(text: str, kind: str = "danger") -> str:
    """Inline colored span, e.g. for red duplicate-field warnings or compatibility
    issue lists. `kind` must be one of "danger" | "success" | "warning"."""
    return f'<span class="uncapped-tag-{kind}">{text}</span>'


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .uncapped-card {{
            background-color: {SURFACE};
            border-radius: 10px;
            padding: 1rem 1.25rem;
            margin-bottom: 0.75rem;
            border: 1px solid rgba(255,255,255,0.06);
        }}
        .uncapped-tag-danger {{ color: {DANGER_ACCENT}; font-weight: 600; }}
        .uncapped-tag-success {{ color: {PRIMARY_ACCENT}; font-weight: 600; }}
        .uncapped-tag-warning {{ color: {WARNING_ACCENT}; font-weight: 600; }}
        .uncapped-muted {{ color: {TEXT_MUTED}; font-size: 0.85rem; }}
        /* Best-effort: push the sidebar's last element (Logout) toward the
        bottom via flex layout. Streamlit's internal sidebar DOM structure can
        shift between versions, so this degrades gracefully to a normal
        top-down stack if the selector doesn't match. */
        section[data-testid="stSidebar"] > div:first-child {{
            display: flex;
            flex-direction: column;
            min-height: 100vh;
        }}
        .uncapped-sidebar-spacer {{ flex-grow: 1; }}
        </style>
        """,
        unsafe_allow_html=True,
    )
