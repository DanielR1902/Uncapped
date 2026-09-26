"""Landing page — Sci-Fi "Command Deck & Observatory Portal" home view
(spec.md §7.2/§7.11): hero header, capability matrix, tiered preset
launchpad, a trending-builds reel, and inline auth modal (spec.md §7.3,
intent.txt §2). Every WRITE action (loading a preset/draft into the Studio,
the 3 primary nav buttons, inspecting a trending build) requires auth — this
app has no real anonymous/guest session anywhere else (router.py's own
comment: "all buttons locked" pre-login, intent.txt); a logged-out visitor
sees only the Login/Register forms, with an inline red notice next to the
Preset Launchpad/Trending Builds section headers explaining why those
buttons are locked.
"""
from __future__ import annotations

import html
import json

import streamlit as st

from auth.session import current_user
from db.repositories import community_repo, drafts_repo
from engine import solvers
from ui import state, theme
from ui.components.auth_modal import render_auth_modal
from ui.format import format_currency, humanize_profile, sanitize_markdown, time_ago

# Tier Alpha/Beta/Ultra (spec.md §7.11) are all the real "Gaming" workload
# profile at increasing tiers — chosen because the directive's own labels
# ("1080p Competitive", "1440p Sweetspot", "4K Enthusiast") are gaming-
# resolution language end to end; `engine.solvers.allocate_workload_baseline`
# already guarantees Cost(Entry) < Cost(High) < Cost(Enthusiast) strictly,
# so the 3 cards are never mispriced relative to each other.
_PRESET_TIERS = (
    ("alpha", "Tier Alpha", "1080p Competitive / Budget Master", "Gaming", "Entry"),
    ("beta", "Tier Beta", "1440p Sweetspot / High-FPS Powerhouse", "Gaming", "High"),
    ("ultra", "Tier Ultra", "4K Enthusiast / AI & Content Creation", "Gaming", "Enthusiast"),
)


def _load_preset_into_studio(profile: str, tier: str) -> None:
    selection = solvers.allocate_workload_baseline(profile, target_tier=tier, include_peripherals=True)
    draft = state.load_components_into_new_draft(
        mode="Workload",
        components={category: component.id for category, component in selection.items()},
        name=f"{humanize_profile(profile)} {tier} Preset",
    )
    draft["workload_profile"] = profile
    draft["tier"] = tier
    st.session_state["build_draft"] = draft
    st.session_state["create_mode"] = "Workload"
    st.session_state["build_draft_analysis"] = None
    st.session_state["has_unsaved_build_changes"] = True
    st.session_state["page"] = "create_build"
    st.rerun()


def _load_draft_into_studio(draft_row) -> None:
    new_draft = state.load_components_into_new_draft(
        mode=draft_row.mode,
        components=json.loads(draft_row.components_json),
        quantities=json.loads(draft_row.quantities_json),
        name=draft_row.name,
    )
    st.session_state["build_draft"] = new_draft
    st.session_state["create_mode"] = draft_row.mode
    st.session_state["build_draft_analysis"] = None
    st.session_state["page"] = "create_build"
    st.rerun()


def _hero(authenticated: bool) -> None:
    st.markdown(
        '<div style="font-family:\'JetBrains Mono\',monospace; font-size:0.85rem; '
        f'color:{theme.PRIMARY_ACCENT}; letter-spacing:0.08em; margin-bottom:0.25rem;">'
        "UNCAPPED // PC ARCHITECTURE PLATFORM</div>",
        unsafe_allow_html=True,
    )
    st.title("Uncapped")
    st.markdown(
        '<div style="display:flex; justify-content:center;">'
        '<span class="uncapped-pulse-badge" style="border-color:'
        f'{theme.PRIMARY_ACCENT}55; font-size:0.95rem; font-weight:700; letter-spacing:0.08em; '
        f'color:{theme.PRIMARY_ACCENT};">'
        f'<span class="uncapped-pulse-dot" style="background:{theme.PRIMARY_ACCENT}; '
        f'box-shadow:0 0 6px {theme.PRIMARY_ACCENT};"></span>'
        "SYSTEM OPERATIONAL</span></div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "AI-assisted, spec-driven PC build platform — compatibility, budget, "
        "and synergy, solved for you."
    )
    with st.expander("ℹ️ SYSTEM CAPABILITIES & ARCHITECTURE OVERVIEW", expanded=True):
        st.markdown(
            "- **Zero-Bottleneck Configuration:** Real-time hardware compatibility matrix, power "
            "load auditing, and dynamic bottleneck detection.\n"
            "- **Deterministic Budget Engine:** Holistic allocation across core flagships and all "
            "7 unified peripherals, matching 88%-98% of your ceiling.\n"
            "- **Intelligent AI Concierge:** Autonomous component rebalancing, multi-intent batch "
            "changes, and conversational tuning.\n"
            "- **Community Rig Exchange:** Seamless one-click publishing, telemetry sharing, and "
            "interactive public build feedback."
        )

    cols = st.columns(3)
    if cols[0].button(
        "⚡ Launch Build Studio", key="nav_create_build", use_container_width=True,
        disabled=not authenticated, type="primary",
    ):
        st.session_state["page"] = "create_build"
        st.rerun()
    if cols[1].button(
        "🌐 Community Showcase", key="nav_community", use_container_width=True, disabled=not authenticated,
    ):
        st.session_state["page"] = "community"
        st.rerun()
    if cols[2].button(
        "📁 Saved Builds / Telemetry Logs", key="nav_my_builds", use_container_width=True,
        disabled=not authenticated,
    ):
        st.session_state["page"] = "my_builds"
        st.rerun()


_CAPABILITY_MATRIX = (
    ("🔧", "Real-Time Synergy & Clearance", "Automated checks across cooler height, case clearance, sockets, and RAM."),
    ("📉", "Dynamic Bottleneck Mitigation", "Real-time compute balance monitoring."),
    ("💱", "Multi-Currency Telemetry", "Instant sync across USD ($), EUR (€), and NIS (₪)."),
    ("🤖", "AI Architecture Concierge", "Spec-driven hardware orchestration."),
)


def _capability_matrix() -> None:
    st.markdown(theme.section_header("Platform Capability Matrix"), unsafe_allow_html=True)
    cols = st.columns(4)
    for col, (icon, title, desc) in zip(cols, _CAPABILITY_MATRIX):
        with col:
            with st.container(border=True):
                st.markdown(f"### {icon}")
                st.markdown(f"**{title}**")
                st.caption(desc)


def _preset_launchpad(authenticated: bool) -> None:
    suffix = None if authenticated else theme.auth_required_notice()
    st.markdown(theme.section_header("Tiered Preset Launchpad", suffix_html=suffix), unsafe_allow_html=True)
    currency = st.session_state.get("selected_currency", "USD")
    cols = st.columns(3)
    for col, (key, name, subtitle, profile, tier) in zip(cols, _PRESET_TIERS):
        with col:
            with st.container(border=True):
                st.markdown(f"**{name}**")
                st.caption(subtitle)
                selection = solvers.allocate_workload_baseline(profile, target_tier=tier, include_peripherals=True)
                cost = state.build_total_cost(selection)
                st.markdown(
                    theme.hud_chip("Est. Price", format_currency(cost, currency)), unsafe_allow_html=True,
                )
                if st.button(
                    "Load Preset to Studio", key=f"load_preset_{key}", use_container_width=True,
                    disabled=not authenticated,
                ):
                    _load_preset_into_studio(profile, tier)


def _trending_builds_reel(authenticated: bool) -> None:
    posts = community_repo.get_feed()[:3]
    if not posts:
        return
    suffix = None if authenticated else theme.auth_required_notice()
    st.markdown(theme.section_header("Trending Community Builds", suffix_html=suffix), unsafe_allow_html=True)
    currency = st.session_state.get("selected_currency", "USD")
    cols = st.columns(3)
    for col, post in zip(cols, posts):
        with col:
            with st.container(border=True):
                st.markdown(
                    f'<span class="uncapped-muted">u/{sanitize_markdown(post.user.full_name)} • '
                    f"{time_ago(post.created_at)}</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(f"**{sanitize_markdown(post.title)}**")
                st.caption(format_currency(post.build.total_cost, currency))
                if post.flair:
                    st.markdown(theme.flair_badge(post.flair), unsafe_allow_html=True)
                if st.button(
                    "Inspect Blueprint", key=f"trending_view_{post.id}", use_container_width=True,
                    disabled=not authenticated,
                ):
                    st.session_state["selected_post_id"] = post.id
                    st.session_state["page"] = "community"
                    st.rerun()


def _user_state_panel(authenticated: bool) -> None:
    if authenticated:
        user = current_user()
        st.markdown(
            '<div style="text-align:center; padding:12px 0;">'
            '<span style="font-size:1.35rem; font-weight:700; letter-spacing:0.05em; color:#FFFFFF;">'
            f"Welcome back, "
            f'<span style="color:{theme.PRIMARY_ACCENT};">Architect {html.escape(user["full_name"])}</span>'
            "</span></div>",
            unsafe_allow_html=True,
        )
        recent_drafts = drafts_repo.get_user_drafts(user["id"])
        if recent_drafts:
            latest = recent_drafts[0]
            cols = st.columns([3, 1])
            cols[0].caption(f"Resume Last Draft: **{sanitize_markdown(latest.name)}**")
            if cols[1].button("Resume", key="resume_last_draft", use_container_width=True):
                _load_draft_into_studio(latest)
        return

    st.info("Log in or create an account to unlock the Build Studio, Community, and saved builds.")
    render_auth_modal()


def render() -> None:
    authenticated = st.session_state.get("auth_user") is not None
    _hero(authenticated)
    _user_state_panel(authenticated)
    st.divider()
    _capability_matrix()
    st.divider()
    _preset_launchpad(authenticated)
    st.divider()
    _trending_builds_reel(authenticated)
