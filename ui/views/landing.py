"""Landing page — Sci-Fi "Command Deck & Observatory Portal" home view
(spec.md §7.2/§7.11): hero header, capability matrix, tiered preset
launchpad, a trending-builds reel, and inline auth modal (spec.md §7.3,
intent.txt §2). Every WRITE action (loading a preset/draft into the Studio,
the 3 primary nav buttons) still requires auth — this app has no real
anonymous/guest session anywhere else (router.py's own comment: "all
buttons locked" pre-login, intent.txt) — so "Continue as Guest Architect"
below is a labeled invitation into registration, not real guest access;
implementing actual anonymous browsing would be a foundational, app-wide
gating change well beyond a home-page redesign.
"""
from __future__ import annotations

import json

import streamlit as st

from auth.session import current_user, set_auth_mode
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
        theme.pulse_badge("SYSTEM ONLINE • SPEC ENGINE v2.6 • ZERO BOTTLENECK AUDIT", "good"),
        unsafe_allow_html=True,
    )
    st.caption(
        "AI-assisted, spec-driven PC build platform — compatibility, budget, "
        "and synergy, solved for you."
    )
    st.markdown(
        "Uncapped takes the guesswork out of building a PC. Pick a budget, a "
        "workload, or go fully custom — every part list is checked for "
        "compatibility in real time, scored for synergy, and analyzed for "
        "bottlenecks before you spend a cent."
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
    st.markdown(theme.section_header("Tiered Preset Launchpad"), unsafe_allow_html=True)
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


def _trending_builds_reel() -> None:
    posts = community_repo.get_feed()[:3]
    if not posts:
        return
    st.markdown(theme.section_header("Trending Community Builds"), unsafe_allow_html=True)
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
                    st.markdown(theme.pulse_badge(post.flair, "flair"), unsafe_allow_html=True)
                if st.button("Inspect Blueprint", key=f"trending_view_{post.id}", use_container_width=True):
                    st.session_state["selected_post_id"] = post.id
                    st.session_state["page"] = "community"
                    st.rerun()


def _user_state_panel(authenticated: bool) -> None:
    if authenticated:
        user = current_user()
        st.markdown(f"#### Welcome back, Architect {sanitize_markdown(user['full_name'])}")
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
    if st.button("Continue as Guest Architect", key="continue_as_guest"):
        set_auth_mode("register")
        st.rerun()


def render() -> None:
    authenticated = st.session_state.get("auth_user") is not None
    _hero(authenticated)
    _user_state_panel(authenticated)
    st.divider()
    _capability_matrix()
    st.divider()
    _preset_launchpad(authenticated)
    st.divider()
    _trending_builds_reel()
