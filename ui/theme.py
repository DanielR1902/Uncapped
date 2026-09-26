"""Color/typography constants + CSS injection (spec.md §7.7). Single source of
truth — no hardcoded hex codes anywhere else in ui/."""
from __future__ import annotations

import html
import re

import streamlit as st


# High-Tech "Blueprint / Steel-Navy" palette (spec.md §7.7). Values below ARE
# the palette — updating them here is the entire reskin, since every view in
# this app already imports color constants from this one module rather than
# hardcoding hex (db/CLAUDE.md-style single-source-of-truth discipline); no
# tests assert on any specific hex value (checked before this change), so
# swapping them in place is safe. Replaces the prior Data Observatory round's
# palette entirely.
BACKGROUND = "#070d14"  # Deep Blue-Grey Obsidian
SURFACE = "#0c1724"  # Module panels / cards — Dark Steel Navy
SURFACE_BORDER = "rgba(0, 229, 255, 0.22)"
GLASS_SURFACE = "rgba(12, 23, 36, 0.75)"  # backdrop-filter-blurred panel fill
BUTTON_SURFACE = "#102235"  # dark steel button background, distinct from SURFACE
STEEL_MUTED = "#1e3247"  # Circuit Muted Steel — inactive button backgrounds / card headers
PRIMARY_ACCENT = "#00e5ff"  # Blueprint Cyan/Teal — active borders, hover states, primary CTAs
MATRIX_GREEN = "#00f090"  # Electric Mint/Green — Optimal/Compatible status, deliberately
# distinct from PRIMARY_ACCENT (a prior round's palette used one green for both
# "primary accent" and "compatible"; this separation is kept here too: cyan is
# the general UI accent, green is reserved specifically for a compatibility-
# positive/"system online" signal).
ACCENT_MAGENTA = "#d946ef"  # Secondary Flux Purple/Magenta — used for
# non-compatibility content tags (e.g. the "Rate My Build" flair badge, so
# it reads as "a content type" rather than "a compatibility signal").
WARNING_ACCENT = "#F2B134"  # near-budget / PSU-headroom-tight warnings
DANGER_ACCENT = "#ff4757"  # Alert Red — failed compatibility, "must be logged in" notices
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


def profile_badge(full_name: str, username: str) -> str:
    """Sidebar "cyber identity" card (spec.md §7.7/§7.8) replacing the plain
    `**{full_name}**` + `@{username}` caption — a bold, cyan display name
    with an inline "ARCHITECT" role tag on line 1, and a muted monospace
    "@{username} • Online" handle on line 2. Both `full_name`/`username` are
    real user-supplied text (registration fields) — HTML-escaped before
    interpolation, the same discipline `avatar_html`/`tag` already follow.
    Render via `st.markdown(profile_badge(...), unsafe_allow_html=True)`."""
    safe_name = html.escape(full_name) if full_name else "Architect"
    safe_username = html.escape(username) if username else ""
    return (
        '<div class="uncapped-profile-badge">'
        f'<div class="uncapped-profile-badge-name">⚡ {safe_name}'
        f'<span class="uncapped-profile-badge-role">ARCHITECT</span></div>'
        f'<div class="uncapped-profile-badge-handle">@{safe_username} • Online</div>'
        "</div>"
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


# The two real, currently-selectable community post tags (spec.md §3.6/§3.7,
# db.models.CommunityPost.flair) — "Rate My Build" reuses the "good"
# (Matrix Green) pulse status, "Looking for Help" reuses "danger" (Alert
# Red), matching this app's own existing semantic-color meanings (green =
# positive/showcase, red = needs-attention) rather than introducing a THIRD,
# one-off color pair that would duplicate what these two already mean.
_FLAIR_STATUS = {
    "Rate My Build": "good",
    "Looking for Help": "danger",
}


def flair_badge(flair: str) -> str:
    """`pulse_badge` for a real `CommunityPost.flair` value — green for "Rate
    My Build", red for "Looking for Help". Falls back to the old generic
    "flair" (magenta) status for any OTHER value (e.g. a historical post
    seeded before these two became the only real options), so a legacy/
    unrecognized flair string still renders instead of raising a KeyError."""
    return pulse_badge(flair, _FLAIR_STATUS.get(flair, "flair"))


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


def section_header(text: str, suffix_html: str | None = None) -> str:
    """Left-accent-bordered section heading (e.g. grouping build cards by
    workload profile in my_builds.py, or the community hardware blueprint's
    "Core Components"/"Storage & Cooling" groups) — a step up from a bare
    `####` without hardcoding a color outside this module. Data Observatory
    "tech label" formatting (spec.md §7.7): `text` is uppercased, non-
    alphanumeric runs collapsed to a single underscore, and prefixed
    `"// "` — e.g. "Core Components" -> "// CORE_COMPONENTS", matching a
    mission-control panel title. `suffix_html` (optional, e.g.
    `auth_required_notice()` below) is inlined right after the label INSIDE
    the same heading element, so it renders beside the text rather than on
    its own line — used by `landing.py`'s Preset Launchpad/Trending Builds
    headers to show a red "must be logged in" notice for a guest visitor.
    Render via `st.markdown(section_header(...), unsafe_allow_html=True)`."""
    tech_label = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return f'<div class="uncapped-section-header">// {tech_label}{suffix_html or ""}</div>'


def auth_required_notice(text: str = "[ must be logged in to view these builds ]") -> str:
    """Small red monospace inline notice (spec.md §7.11) for a guest-locked
    section — pass as `section_header(..., suffix_html=auth_required_notice())`.
    Render via `st.markdown(..., unsafe_allow_html=True)`."""
    return (
        f'<span style="color:{DANGER_ACCENT}; font-weight:600; font-family:{FONT_MONO}; '
        f'margin-left:12px; font-size:0.85rem;">{text}</span>'
    )


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

        html, body, [class*="css"] {{ font-family: {FONT_SANS}; }}
        /* Streamlit's own chrome (sidebar, main container) painted to match
        the Blueprint base — .streamlit/config.toml sets the same base colors
        for native widgets, this covers the surrounding containers
        config.toml's [theme] table doesn't reach. A faint radial cyan "aura"
        (spec.md §7.11) layers a subtle circuit-mesh glow behind the content
        without competing with it — a low-opacity radial-gradient overlay on
        top of the solid background color, not a separate image asset. */
        [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: {BACKGROUND};
            background-image: radial-gradient({PRIMARY_ACCENT}14 0%, transparent 60%);
            background-repeat: no-repeat;
            background-position: top center;
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

        /* Sharp, tactile button styling (spec.md §7.4.1/§7.11) — dark steel
        background, clean cyan border, subtle glow on hover, replacing
        Streamlit's flat default. Applies app-wide since every button already
        goes through Streamlit's own type="primary"/"secondary" mechanism. */
        .stButton button, .stFormSubmitButton button {{
            background-color: {BUTTON_SURFACE};
            border-radius: 6px;
            border: 1px solid {PRIMARY_ACCENT};
            transition: box-shadow 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
        }}
        .stButton button:hover, .stFormSubmitButton button:hover {{
            box-shadow: 0 0 10px {PRIMARY_ACCENT}55;
        }}
        .stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {{
            box-shadow: 0 0 10px {PRIMARY_ACCENT}55;
            border-color: {PRIMARY_ACCENT};
        }}
        .stButton button[kind="primary"]:hover, .stFormSubmitButton button[kind="primary"]:hover {{
            box-shadow: 0 0 16px {PRIMARY_ACCENT}88;
        }}
        /* Visibly locked/unclickable disabled state (spec.md §7.11 — guest
        lockdown on the Preset Launchpad/Trending Builds buttons): muted
        steel background/border and no hover glow, so a disabled button never
        looks identical to a real, clickable one. */
        .stButton button:disabled, .stFormSubmitButton button:disabled {{
            background-color: {STEEL_MUTED} !important;
            border-color: rgba(255,255,255,0.12) !important;
            box-shadow: none !important;
            opacity: 0.6;
        }}
        /* Uniform card heights across the Home page's 3 grids — Platform
        Capability Matrix, Tiered Preset Launchpad, Trending Community
        Builds (spec.md §7.11, ui/views/landing.py) — every card in all 3 is
        a plain `st.container(border=True)` nested inside `st.columns(...)`,
        no keys/classes distinguishing one grid's cards from another's, so
        one generic, testid-based rule covers all 3 at once. Confirmed live
        (Streamlit 1.63.0) the real DOM shape for a nested bordered
        container is `stColumn > stVerticalBlock > stLayoutWrapper >
        stVerticalBlock` (the INNER stVerticalBlock is the actual card,
        holding the icon/title/caption/price/button element-containers as
        direct children) — NOT `div[data-testid="column"] > div` as a first
        guess might assume: "column" alone is not a real testid in this
        Streamlit version, the real one is `stColumn`, and there is no
        wrapper div directly beneath it without the intermediate
        stLayoutWrapper layer. `align-items: stretch` on the row makes every
        column match the tallest; `height: 100%` threaded down through the
        column/wrapper/card makes the card itself fill that height;
        `:has(div[data-testid="stButton"])` (not `:last-child`, which would
        also wrongly catch the Capability Matrix's plain caption-only cards
        that have no button at all) pins ONLY an element-container that
        actually contains a button to the bottom via margin-top:auto,
        leaving the title/caption content grouped at the top above it. */
        div[data-testid="stHorizontalBlock"] {{
            align-items: stretch;
        }}
        div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"] {{
            height: 100%;
        }}
        div[data-testid="stColumn"] div[data-testid="stLayoutWrapper"] {{
            height: 100%;
        }}
        div[data-testid="stColumn"] div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"] {{
            height: 100%;
            display: flex;
            flex-direction: column;
            min-height: 220px;
        }}
        div[data-testid="stColumn"] div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"]
            > div[data-testid="stElementContainer"]:has(div[data-testid="stButton"]) {{
            margin-top: auto;
        }}

        /* Currency segmented control (spec.md §7.7/§7.12, app.py's sidebar
        `st.segmented_control(key="selected_currency", ...)`) — Deep Steel/
        Cyan glow aesthetic, active segment highlighted in PRIMARY_ACCENT.
        Confirmed live (Streamlit 1.63.0) this widget's real DOM is a
        `div[data-testid="stButtonGroup"]` wrapper containing plain `<button>`
        elements with NO distinguishing class or `data-testid` between the
        active and inactive segments — the only live-verified difference is
        the `aria-checked` attribute ("true" on the currently-selected
        segment, "false" on the others), so styling MUST key off
        `[aria-checked="true"]`, not a class selector (a class-based rule
        silently matches nothing, since every segment shares the exact same
        class list regardless of selection state). */
        div[data-testid="stButtonGroup"] {{
            gap: 4px;
        }}
        div[data-testid="stButtonGroup"] button {{
            background-color: {BUTTON_SURFACE} !important;
            border: 1px solid {SURFACE_BORDER} !important;
            color: {TEXT_MUTED} !important;
            transition: box-shadow 0.15s ease, border-color 0.15s ease,
                background-color 0.15s ease, color 0.15s ease;
        }}
        div[data-testid="stButtonGroup"] button:hover {{
            border-color: {PRIMARY_ACCENT}aa !important;
            color: {TEXT_PRIMARY} !important;
        }}
        div[data-testid="stButtonGroup"] button[aria-checked="true"] {{
            background-color: {STEEL_MUTED} !important;
            border-color: {PRIMARY_ACCENT} !important;
            color: {PRIMARY_ACCENT} !important;
            box-shadow: 0 0 10px {PRIMARY_ACCENT}55;
        }}

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

        /* Hide Streamlit's own default chrome — the top header bar (which
        holds the Deploy button and the "..." options menu) and, best-effort,
        the options-menu button specifically. Confirmed live (Streamlit
        1.63.0) the real DOM is `header[data-testid="stHeader"]` containing
        `div[data-testid="stToolbar"]`, which in turn contains
        `div[data-testid="stAppDeployButton"]` (the Deploy button) and
        `span[data-testid="stMainMenu"]` (the "..." menu) — NOT a
        `.stDeployButton` class or a bare `#MainMenu` id as a first guess
        might assume (`#MainMenu` DOES still exist as a legacy compatibility
        id on that same `stMainMenu` span, verified live, but the real
        testid is the one this rule targets). Hiding the outer `stHeader`
        alone already hides everything nested inside it; the `stToolbar`/
        `stAppDeployButton` rules are extra, harmless belt-and-suspenders in
        case a future Streamlit version restructures the header but keeps
        those inner testids stable. No `<footer>` element exists anywhere in
        this Streamlit version's DOM (checked live) — there is nothing for a
        `footer { ... }` rule to target, so none is included here.
        `min-height` must be zeroed too, not just `height`: confirmed live
        Streamlit's own emotion CSS sets a `min-height: 60px` on this
        element, and `min-height` always wins as the effective rendered
        height over a smaller `height` regardless of `!important` on
        `height` alone (a real box-model floor, not a specificity fight) —
        `height: 0px !important` by itself left a real 60px blank (if
        invisible) gap at the top of the page. */
        header[data-testid="stHeader"] {{
            visibility: hidden !important;
            height: 0px !important;
            min-height: 0px !important;
        }}
        div[data-testid="stToolbar"], div[data-testid="stAppDeployButton"] {{
            visibility: hidden !important;
            display: none !important;
        }}

        /* User profile identity card (spec.md §7.7/§7.8, theme.profile_badge)
        — the sidebar's own bold-name + muted-@handle text, reskinned as a
        small "cyber identity" module. */
        .uncapped-profile-badge {{
            background: rgba(16, 34, 53, 0.65);
            border: 1px solid rgba(0, 229, 255, 0.25);
            border-radius: 6px;
            padding: 10px 14px;
            margin-bottom: 0.5rem;
        }}
        .uncapped-profile-badge-name {{
            font-size: 1rem;
            font-weight: 700;
            color: {PRIMARY_ACCENT};
        }}
        .uncapped-profile-badge-role {{
            font-family: {FONT_MONO};
            font-size: 0.7rem;
            font-weight: 600;
            color: {PRIMARY_ACCENT};
            border: 1px solid {PRIMARY_ACCENT}55;
            border-radius: 4px;
            padding: 1px 5px;
            margin-left: 6px;
            vertical-align: middle;
        }}
        .uncapped-profile-badge-handle {{
            font-family: {FONT_MONO};
            font-size: 0.8rem;
            color: {TEXT_MUTED};
            margin-top: 2px;
        }}
        /* Widened from an earlier round's 320px (spec.md §7.7/§7.8) — the
        currency segmented control, nav buttons, and Concierge chat/telemetry
        were cramped at that width. 420px keeps a real min/max range
        (400-440px) rather than a single hard-pinned number, in case a future
        Streamlit version's own layout math wants a few px of slack, while
        still always overriding `width` itself for the same reason as
        before: Streamlit's draggable-resize logic writes a plain
        (non-!important) inline `width: <Npx>` that only an `!important`
        `width` override here (not just `min-width`/`max-width`) actually
        beats. */
        section[data-testid="stSidebar"] {{
            width: 420px !important;
            min-width: 400px !important;
            max-width: 440px !important;
        }}
        /* Breathing room for the sidebar's real content (currency control,
        nav buttons, Concierge chat/telemetry) — confirmed live this is a
        real testid (Streamlit 1.63.0), the direct wrapper around everything
        app.py writes into the sidebar (nav/currency/chat), distinct from
        `stSidebarHeader` (the collapse button/logo row, hidden above) so
        this padding never affects that already-hidden element. */
        div[data-testid="stSidebarUserContent"] {{
            padding-left: 1.25rem !important;
            padding-right: 1.25rem !important;
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
