"""Color/typography constants + CSS injection (spec.md §7.7). Single source of
truth — no hardcoded hex codes anywhere else in ui/."""
from __future__ import annotations

import html
import re

import streamlit as st


# Sci-Fi "Data Observatory / Cyber-Telemetry HUD" palette (spec.md §7.7).
# Values below ARE the palette — updating them here is the entire reskin,
# since every view in this app already imports color constants from this one
# module rather than hardcoding hex (db/CLAUDE.md-style single-source-of-
# truth discipline); no tests assert on any specific hex value (checked
# before this change), so swapping them in place is safe. Replaces the prior
# Cyber-Minimalist / High-Tech Industrial round's palette entirely.
BACKGROUND = "#050814"  # Deep Cyber Navy
SURFACE = "#0c1024"  # Surface panels / telemetry modules
SURFACE_BORDER = "rgba(0, 242, 254, 0.2)"
GLASS_SURFACE = "rgba(12, 16, 36, 0.75)"  # backdrop-filter-blurred panel fill
PRIMARY_ACCENT = "#00f2fe"  # Primary Telemetry Cyan — general CTA/accent color
MATRIX_GREEN = "#10b981"  # Optimal/Compatible status — deliberately distinct
# from PRIMARY_ACCENT now (the prior round's palette used ONE green for both
# "primary accent" and "compatible"; this round's Data Observatory look
# separates them: cyan is the general UI accent, green is reserved
# specifically for a compatibility-positive/optimal signal).
ACCENT_MAGENTA = "#d946ef"  # Secondary Flux Purple/Magenta — used for
# non-compatibility content tags (e.g. the "Rate My Build" flair badge, so
# it reads as "a content type" rather than "a compatibility signal").
WARNING_ACCENT = "#F2B134"  # near-budget / PSU-headroom-tight warnings
DANGER_ACCENT = "#f43f5e"  # Critical Alert — failed compatibility
TEXT_PRIMARY = "#E6EDF3"
TEXT_MUTED = "#64748b"  # Muted Industrial Slate

# Numeric HUD readouts (wattage, prices, synergy/bottleneck scores) use a
# monospace face so digits stay tabular/scannable at a glance; titles and body
# copy stay on a clean sans-serif. Both are loaded via inject_css()'s Google
# Fonts @import — real font FAMILY names, not Streamlit's config.toml generic
# "sans serif"/"serif"/"monospace" keywords, so this lives in CSS, not
# .streamlit/config.toml (which still only sets the base dark theme + accent
# for Streamlit's own native chrome).
FONT_MONO = "'JetBrains Mono', 'Fira Code', 'Roboto Mono', monospace"
FONT_SANS = "'Inter', system-ui, -apple-system, sans-serif"

# A small, deliberately distinct-from-the-semantic-accents palette for
# `avatar_html`'s per-user color-picking (spec.md §7.6, community post
# headers) — these are decorative/identity colors, not status colors, so
# they're kept separate from PRIMARY_ACCENT/MATRIX_GREEN/WARNING_ACCENT/
# DANGER_ACCENT above (which each mean something specific — reusing them
# here would blur that meaning).
AVATAR_PALETTE = ("#3DDC97", "#F2B134", "#5B8DEF", "#B15BEF", "#EF5BA1", "#4FC3D9")


def tag(text: str, kind: str = "danger") -> str:
    """Inline colored span, e.g. for red duplicate-field warnings or compatibility
    issue lists. `kind` must be one of "danger" | "success" | "warning"."""
    return f'<span class="uncapped-tag-{kind}">{text}</span>'


def avatar_html(name: str, size_px: int = 32) -> str:
    """A small circular initial-avatar placeholder (this app has no real
    profile-picture upload feature) — a deterministic color from
    `AVATAR_PALETTE`, picked from a hash of `name` so the SAME user always
    gets the SAME color across renders, not a real image. `name` is
    HTML-escaped (it's real user-supplied data — a full_name — never trusted
    raw inside `unsafe_allow_html=True` markup, the same discipline
    `sanitize_markdown` applies elsewhere in this package). Render via
    `st.markdown(avatar_html(...), unsafe_allow_html=True)`."""
    safe_name = html.escape(name) if name else "?"
    initial = html.escape(name[:1].upper()) if name else "?"
    color = AVATAR_PALETTE[sum(ord(c) for c in name) % len(AVATAR_PALETTE)] if name else TEXT_MUTED
    return (
        f'<div class="uncapped-avatar" title="{safe_name}" '
        f'style="width:{size_px}px; height:{size_px}px; background:{color}; '
        f'font-size:{size_px * 0.5}px;">{initial}</div>'
    )


_PULSE_STATUS_COLORS = {
    "good": MATRIX_GREEN,
    "warning": WARNING_ACCENT,
    "danger": DANGER_ACCENT,
    "flair": ACCENT_MAGENTA,
}


def pulse_badge(text: str, status: str = "good") -> str:
    """A sleek pill badge with a glowing dot indicator (spec.md §7.4.1) — e.g.
    "100% Compatible" (`status="good"`, Matrix Green pulse — a compatibility/
    optimal signal specifically, not the general `PRIMARY_ACCENT`) or
    "2 Warnings Detected" (`status="warning"`, amber). `status` must be one
    of "good"|"warning"|"danger"|"flair" — "flair" (magenta) is for a
    non-compatibility content tag (e.g. the "Rate My Build" post flair,
    spec.md §3.6/§7.6), deliberately a different color family so it never
    reads as a compatibility signal. `text` is treated as trusted,
    pre-formatted display text (never raw user input) — same convention as
    `tag()`. Render via `st.markdown(pulse_badge(...), unsafe_allow_html=True)`."""
    color = _PULSE_STATUS_COLORS[status]
    return (
        f'<span class="uncapped-pulse-badge" style="border-color:{color}55;">'
        f'<span class="uncapped-pulse-dot" style="background:{color}; box-shadow:0 0 6px {color};"></span>'
        f"{text}</span>"
    )


def hud_chip(label: str, value: str, help_text: str | None = None, value_color: str | None = None) -> str:
    """One monospace-value telemetry chip for the Build Studio's HUD
    (spec.md §7.4.1) — a small caption label over a large monospace readout
    (wattage, converted price, synergy/bottleneck score), matching the
    industrial "numeric HUD readout" typography rule (`FONT_MONO`). `value`
    is trusted, pre-formatted display text. `value_color`, when given,
    overrides the default `TEXT_PRIMARY` value color (e.g. the Power/
    Headroom chip turns `WARNING_ACCENT` when a PSU is under-provisioned).
    Render via `st.markdown(hud_chip(...), unsafe_allow_html=True)`."""
    title_attr = f' title="{html.escape(help_text)}"' if help_text else ""
    color_style = f" style=\"color:{value_color};\"" if value_color else ""
    return (
        f'<div class="uncapped-hud-chip"{title_attr}>'
        f'<div class="uncapped-hud-chip-label">{label}</div>'
        f'<div class="uncapped-hud-chip-value"{color_style}>{value}</div>'
        f"</div>"
    )


def section_header(text: str) -> str:
    """Left-accent-bordered section heading (e.g. grouping build cards by
    workload profile in my_builds.py, or the community hardware blueprint's
    "Core Components"/"Storage & Cooling" groups) — a step up from a bare
    `####` without hardcoding a color outside this module. Data Observatory
    "tech label" formatting (spec.md §7.7): `text` is uppercased, non-
    alphanumeric runs collapsed to a single underscore, and prefixed
    `"// "` — e.g. "Core Components" -> "// CORE_COMPONENTS", matching a
    mission-control panel title. Render via
    `st.markdown(section_header(...), unsafe_allow_html=True)`."""
    tech_label = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return f'<div class="uncapped-section-header">// {tech_label}</div>'


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

        html, body, [class*="css"] {{ font-family: {FONT_SANS}; }}
        /* Streamlit's own chrome (sidebar, main container) painted to match
        the Matte Carbon base — .streamlit/config.toml sets the same base
        colors for native widgets, this covers the surrounding containers
        config.toml's [theme] table doesn't reach. */
        [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: {BACKGROUND};
        }}
        .uncapped-mono {{ font-family: {FONT_MONO}; }}

        .uncapped-card {{
            background-color: {SURFACE};
            border-radius: 10px;
            padding: 1rem 1.25rem;
            margin-bottom: 0.75rem;
            border: 1px solid {SURFACE_BORDER};
        }}
        /* Industrial glass panels (spec.md §7.4.1) — any KEYED
        st.container(border=True) card (the HUD, each hardware picker slot)
        gets a blurred, translucent fill plus a hairline border. Targets
        `[data-testid="stVerticalBlock"][class*="st-key-"]` directly, NOT a
        separate wrapper div — confirmed live (Streamlit 1.63.0) that
        `st.container(key=...)`'s own `st-key-<key>` class lands on the
        `stVerticalBlock` element itself (the SAME element Streamlit's own
        `border=True` styling is already on), unlike `st.button(key=...)`
        where that class lands 5 DOM levels above the actual `<button>` —
        an earlier version of this rule targeted a
        `stVerticalBlockBorderWrapper` testid that, verified live just now,
        does not exist ANYWHERE in this Streamlit version's DOM and so never
        matched anything; a plain (non-keyed) `st.container(border=True)`
        elsewhere in this app (e.g. the mode-selector cards) keeps
        Streamlit's native solid-border look, out of scope for this pass.
        Real `backdrop-filter` (not a plain solid fill) — needs the
        -webkit- prefix for Safari/older WebKit-based embedded browsers;
        harmless no-op elsewhere. */
        [data-testid="stVerticalBlock"][class*="st-key-"] {{
            background: {GLASS_SURFACE} !important;
            -webkit-backdrop-filter: blur(12px);
            backdrop-filter: blur(12px);
            border: 1px solid {SURFACE_BORDER} !important;
            border-radius: 6px !important;
            box-shadow: 0 0 15px rgba(0, 242, 254, 0.05), inset 0 0 15px rgba(0, 242, 254, 0.02) !important;
        }}

        /* Real st.metric widgets this app still uses (build_card.py's Cost/
        Compatibility/Synergy/Bottleneck cards, the Rate My Build dialog's
        snapshot) — Telemetry Observatory look: glowing cyan monospace value,
        uppercase muted-slate label. No `delta=` is used anywhere in this
        app (checked before adding a color override here), so this can't
        clash with Streamlit's own delta-arrow red/green semantics. */
        div[data-testid="stMetricValue"] {{
            font-family: {FONT_MONO};
            color: {PRIMARY_ACCENT} !important;
            text-shadow: 0 0 8px rgba(0, 242, 254, 0.35);
        }}
        /* NOT `div[data-testid="stMetricLabel"]` — verified live this
        element actually renders as a <label>, not a <div>, despite most
        other Streamlit "st*" testid elements being divs; a tag-qualified
        selector requiring `div` here silently matched nothing. */
        [data-testid="stMetricLabel"] p {{
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: {TEXT_MUTED} !important;
        }}
        .uncapped-tag-danger {{ color: {DANGER_ACCENT}; font-weight: 600; }}
        .uncapped-tag-success {{ color: {MATRIX_GREEN}; font-weight: 600; }}
        .uncapped-tag-warning {{ color: {WARNING_ACCENT}; font-weight: 600; }}
        .uncapped-muted {{ color: {TEXT_MUTED}; font-size: 0.85rem; }}
        .uncapped-avatar {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            color: {BACKGROUND};
            font-weight: 700;
            flex-shrink: 0;
        }}
        /* Threaded comment reply indentation/connector (spec.md §7.6) — applied
        to a comment's own content block via st.markdown(unsafe_allow_html=True);
        the ACTUAL indentation of the surrounding interactive widgets (the Reply
        button, the reply text_area) is handled separately via st.columns in
        ui/views/community.py, since arbitrary injected HTML cannot wrap
        subsequently-rendered native Streamlit widgets — this class only styles
        the static text content, never anything interactive. */
        .uncapped-comment-reply {{
            border-left: 2px solid rgba(255,255,255,0.15);
            padding-left: 12px;
            margin-bottom: 0.5rem;
        }}
        .uncapped-comment-top {{ margin-bottom: 0.5rem; }}
        .uncapped-section-header {{
            border-left: 4px solid {PRIMARY_ACCENT};
            padding: 0.35rem 0.9rem;
            margin: 1.5rem 0 0.75rem;
            font-size: 1.15rem;
            font-weight: 600;
            color: {TEXT_PRIMARY};
        }}

        /* Pulse-dot pill badges (spec.md §7.4.1, theme.pulse_badge) — the
        Compatibility Matrix pill ("100% Compatible" / "X Warnings Detected")
        and the "Rate My Build" flair badge. */
        .uncapped-pulse-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 0.25rem 0.75rem;
            border-radius: 999px;
            border: 1px solid;
            background: rgba(255,255,255,0.03);
            font-size: 0.8rem;
            font-weight: 600;
            color: {TEXT_PRIMARY};
        }}
        .uncapped-pulse-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            animation: uncapped-pulse 1.8s ease-in-out infinite;
        }}
        @keyframes uncapped-pulse {{
            0%, 100% {{ opacity: 1; transform: scale(1); }}
            50% {{ opacity: 0.5; transform: scale(0.75); }}
        }}

        /* Telemetry HUD chips (spec.md §7.4.1, theme.hud_chip) — a small
        label over a large monospace numeric readout, used for the Build
        Studio's Power/Headroom meter and any other HUD-style KPI. */
        .uncapped-hud-chip {{
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .uncapped-hud-chip-label {{
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: {TEXT_MUTED};
        }}
        .uncapped-hud-chip-value {{
            font-family: {FONT_MONO};
            font-size: 1.15rem;
            font-weight: 700;
            color: {TEXT_PRIMARY};
        }}

        /* Sharp, tactile button styling (spec.md §7.4.1) — thin micro-border
        and a soft accent glow on the active/primary state, replacing
        Streamlit's flat default. Applies app-wide since every button already
        goes through Streamlit's own type="primary"/"secondary" mechanism. */
        .stButton button, .stFormSubmitButton button {{
            border-radius: 6px;
            border: 1px solid {SURFACE_BORDER};
            transition: box-shadow 0.15s ease, border-color 0.15s ease;
        }}
        .stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {{
            box-shadow: 0 0 10px {PRIMARY_ACCENT}55;
            border-color: {PRIMARY_ACCENT};
        }}
        .stButton button[kind="primary"]:hover, .stFormSubmitButton button[kind="primary"]:hover {{
            box-shadow: 0 0 16px {PRIMARY_ACCENT}88;
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

        /* Hardware picker slots (spec.md §7.4.1, ui/components/part_picker.py)
        — an empty slot gets a technical dashed wireframe border instead of
        the glass-panel look the keyed-container rule above already gives
        every OTHER keyed card (including a FILLED picker slot); this rule
        only needs to override the empty case. */
        div[class*="picker_slot_"][class*="_empty"] {{
            border-style: dashed !important;
            background: rgba(255,255,255,0.015) !important;
            -webkit-backdrop-filter: none !important;
            backdrop-filter: none !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
