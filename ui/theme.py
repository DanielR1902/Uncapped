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


def section_header(text: str) -> str:
    """Left-accent-bordered section heading (e.g. grouping build cards by
    workload profile in my_builds.py) — a step up from a bare `####` without
    hardcoding a color outside this module. Render via
    `st.markdown(section_header(...), unsafe_allow_html=True)`."""
    return f'<div class="uncapped-section-header">{text}</div>'


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
        .uncapped-section-header {{
            border-left: 4px solid {PRIMARY_ACCENT};
            padding: 0.35rem 0.9rem;
            margin: 1.5rem 0 0.75rem;
            font-size: 1.15rem;
            font-weight: 600;
            color: {TEXT_PRIMARY};
        }}
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

        /* Lock the sidebar to a fixed width and remove the native
        collapse/expand control — the app's own nav (Home/Create/Drafts/
        Previous Builds/Community) and the AI Concierge live only in the
        sidebar, so letting a user accidentally collapse or resize it away
        would strand them with no way back short of a manual page reload.
        Verified live (both selectors match Streamlit 1.63.0's real sidebar
        DOM) — degrades gracefully (no-op, not a crash) if a future
        Streamlit version renames either testid.

        `width` (not just `min-width`/`max-width`) must be set here too —
        confirmed live that Streamlit's own draggable-resize logic writes a
        plain (non-!important) inline `width: <Npx>` on this exact element,
        and `min-width`/`max-width` ALONE (without an accompanying `width`
        override) did NOT clamp the rendered box in this Streamlit version
        despite being `!important` — only overriding `width` itself here,
        with `!important` so it beats the non-important inline value,
        actually changed the rendered size. */
        /* No tag qualifier — confirmed live this element is actually a
        <div>, not a <button> as its testid name might suggest. */
        [data-testid="stSidebarCollapseButton"] {{ display: none !important; }}
        section[data-testid="stSidebar"] {{
            width: 320px !important;
            min-width: 320px !important;
            max-width: 320px !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
