"""PC Build Studio — Mode A/B/C (spec.md §7.4, intent.txt §3).

Layout: mode selector (or a "change mode" header) → mode-specific generator
controls → a summary header that stays visible above the part-picker grid →
the 8 core slots in a 2-column grid + optional peripherals → a detailed
synergy/bottleneck analysis panel → save/publish.
"""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.models import WORKLOAD_PROFILES
from db.repositories import builds_repo, community_repo, components_repo
from engine import scoring, solvers
from engine.compatibility import evaluate_build
from llm.client import analyze_build
from ui import state, theme
from ui.components.part_picker import SORT_OPTIONS, render_part_picker
from ui.format import humanize_profile

CORE_CATEGORIES = solvers.CATEGORY_ORDER
PERIPHERAL_CATEGORIES = ("NetworkCard", "SoundCard", "OpticalDrive")
TIERS = ("Entry", "Mid", "High", "Enthusiast")

_MODE_CARDS = (
    ("Budget", "💰 Budget Constrained", "Set a spending ceiling — the solver converges on the most capable build that still fits under it."),
    ("Workload", "🎯 Workload Profile", "Pick what it's for — Gaming, Video Editing, Programming/AI, Design, or General — and get a tiered baseline instantly."),
    ("Free", "🧩 Free Custom", "Full control from the first part. Every pick is checked live for compatibility against everything else."),
)


def _mode_selector() -> None:
    st.subheader("Choose how you'd like to build")
    cols = st.columns(3)
    for (mode, title, hint), col in zip(_MODE_CARDS, cols):
        with col:
            with st.container(border=True):
                st.markdown(f"##### {title}")
                st.caption(hint)
                if st.button("Select", key=f"mode_{mode.lower()}", use_container_width=True, type="primary"):
                    st.session_state["create_mode"] = mode
                    st.session_state["build_draft"] = state.new_build_draft(mode)
                    st.session_state["build_draft_analysis"] = None
                    st.rerun()


def _budget_controls(build_draft: dict) -> None:
    with st.container(border=True):
        st.caption("💰 **Budget mode** — the solver spends as much of your ceiling as it can on the highest-tier compatible parts.")

        floor_cost = solvers.minimum_possible_build_cost()

        unlimited = st.checkbox(
            "No limit — show every compatible part, skip budget filtering",
            key="budget_unlimited_input",
        )
        ceiling = st.number_input(
            "Budget ceiling ($)", min_value=floor_cost,
            value=max(build_draft.get("budget_ceiling") or 1500.0, floor_cost), step=50.0,
            key="budget_ceiling_input", disabled=unlimited,
        )
        if not unlimited and ceiling < floor_cost:
            st.toast(
                f"Budget adjusted to ${floor_cost:,.2f} (the minimum viable cost "
                "for a complete compatible build).",
                icon="⚠️",
            )
            ceiling = floor_cost
        # Live-sync immediately (not just inside the button handler below) so
        # every picker's filtering updates the instant the ceiling changes,
        # with no need to click "Generate starting build" first.
        build_draft["budget_ceiling"] = None if unlimited else ceiling

        if st.button("Generate starting build", key="generate_budget_build", type="primary", disabled=unlimited):
            seed_selection = state.resolve_build_state(build_draft)  # keep whatever the user already pinned
            new_selection = solvers.initialize_budget_build(ceiling, seed_selection=seed_selection or None)

            downgraded_categories = [
                category for category, original in seed_selection.items()
                if category in new_selection and new_selection[category].id != original.id
            ]

            build_draft["components"] = {category: component.id for category, component in new_selection.items()}
            st.session_state["build_draft_analysis"] = None

            if downgraded_categories:
                st.toast(
                    "Adjusted pre-selected components to the best possible tier that "
                    "completes a functional build within your budget.",
                    icon="⚠️",
                )
            st.rerun()

        if unlimited:
            st.caption("Uncheck to set a ceiling and generate a starting build, or keep picking parts freely below.")


def _workload_controls(build_draft: dict) -> None:
    with st.container(border=True):
        st.caption("🎯 **Workload mode** — pre-allocates a tier-weighted baseline for your chosen use case, then you can fine-tune any slot.")
        cols = st.columns(2)
        profile = cols[0].selectbox(
            "Workload profile", WORKLOAD_PROFILES, format_func=humanize_profile, key="workload_profile_input",
        )
        tier = cols[1].selectbox("Tier", TIERS, index=1, key="workload_tier_input")
        if st.button("Generate baseline build", key="generate_workload_build", type="primary"):
            build_draft["workload_profile"] = profile
            build_draft["tier"] = tier
            new_selection = solvers.allocate_workload_baseline(profile, target_tier=tier)
            build_draft["components"] = {category: component.id for category, component in new_selection.items()}
            st.session_state["build_draft_analysis"] = None
            st.rerun()


def _sort_controls() -> None:
    current = st.session_state.get("sort_criteria", "Cost")
    choice = st.segmented_control(
        "Sort candidates by", SORT_OPTIONS, default=current, key="sort_criteria_input", selection_mode="single",
    )
    st.session_state["sort_criteria"] = choice or current


def _candidates_for(build_draft: dict, build_state: dict, category: str) -> list:
    """Compatible candidates for `category`, given whatever else is already
    selected. Price-based disabling (the Minimum Reserve Threshold check —
    "will picking this leave enough to fill every other empty slot too?")
    now lives entirely in ui/components/part_picker.py, which receives the
    raw budget ceiling directly and computes it from build_state itself."""
    others = {c: comp for c, comp in build_state.items() if c != category}
    return solvers.get_compatible_candidates(category, others)


def _part_pickers(build_draft: dict, build_state: dict) -> None:
    st.markdown("#### Core Components")
    grid_cols = st.columns(2)
    for index, category in enumerate(CORE_CATEGORIES):
        candidates = _candidates_for(build_draft, build_state, category)
        with grid_cols[index % 2]:
            render_part_picker(
                category,
                build_state,
                candidates,
                on_select=lambda component, cat=category: state.set_component(build_draft, cat, component),
                on_remove=lambda cat=category: state.remove_component(build_draft, cat),
                budget_ceiling=build_draft.get("budget_ceiling"),
            )

    with st.expander("Optional peripherals (Network Card, Sound Card, Optical Drive)"):
        periph_cols = st.columns(3)
        for index, category in enumerate(PERIPHERAL_CATEGORIES):
            candidates = components_repo.get_by_category(category)
            with periph_cols[index % 3]:
                render_part_picker(
                    category,
                    build_state,
                    candidates,
                    on_select=lambda component, cat=category: state.set_component(build_draft, cat, component),
                    on_remove=lambda cat=category: state.remove_component(build_draft, cat),
                )


def _summary_header(build_state: dict) -> None:
    """Prominent, always-current metrics bar placed above the part-picker
    grid. True CSS position:sticky was attempted (targeting the class
    Streamlit generates for st.container(key=...)) but doesn't actually
    engage under this Streamlit version's DOM/flex layout — verified live,
    not just assumed — so this is deliberately a normal (non-sticky) card
    at the top of the page rather than shipping CSS that silently no-ops."""
    total_cost = state.build_total_cost(build_state)
    report = evaluate_build(build_state)
    analysis = st.session_state.get("build_draft_analysis")
    live = scoring.live_bottleneck_and_synergy(build_state) if analysis is None else None

    with st.container(border=True, key="build_summary_header"):
        cols = st.columns(4)
        cols[0].metric("💰 Total Cost", f"${total_cost:,.2f}", border=True)
        cols[1].metric("🔧 Compatibility", f"{report.compatibility_score:.0f}%", border=True)

        if analysis:
            cols[2].metric("⚡ Synergy", f"{analysis['synergy']['overall_score']:.0f}", border=True)
            cols[3].metric(
                "📉 Bottleneck",
                f"{analysis['bottleneck']['bottleneck_percentage']:.0f}%",
                help=f"Limiting component: {analysis['bottleneck']['limiting_component']}",
                border=True,
            )
        elif live is not None:
            synergy, bottleneck_pct, direction = live
            cols[2].metric(
                "⚡ Synergy", f"{synergy:.0f}",
                help="Local estimate — click Analyze below for the full AI/heuristic breakdown.",
                border=True,
            )
            cols[3].metric(
                "📉 Bottleneck", f"{bottleneck_pct:.0f}%",
                help=f"Limiting: {direction}",
                border=True,
            )
        else:
            cols[2].metric("⚡ Synergy", "—", border=True)
            cols[3].metric("📉 Bottleneck", "—", border=True)

        badge_cols = st.columns([1, 1, 2])
        with badge_cols[0]:
            if report.is_compatible:
                st.badge("Compatible", icon=":material/check_circle:", color="green")
            else:
                st.badge(f"{len(report.issues)} issue(s)", icon=":material/error:", color="red")
        if analysis:
            with badge_cols[1]:
                is_ai = analysis["source"] == "llm"
                st.badge(
                    "AI Engine" if is_ai else "Heuristic Baseline",
                    icon=":material/smart_toy:" if is_ai else ":material/calculate:",
                    color="green" if is_ai else "orange",
                )
        elif live is not None:
            with badge_cols[1]:
                st.badge("Live Estimate", icon=":material/bolt:", color="gray")

        if report.issues:
            for issue in report.issues:
                st.markdown(theme.tag(issue, "danger"), unsafe_allow_html=True)


def _analysis_panel(build_draft: dict, build_state: dict) -> None:
    st.markdown("#### Synergy & Bottleneck Analysis")

    if len(build_state) < 2:
        st.caption("Select at least two components to run an analysis.")
        return

    if st.button("🔮 Analyze synergy & bottleneck", key="run_analysis"):
        with st.spinner("Analyzing build..."):
            response = analyze_build(
                build_state,
                workload_profile=build_draft.get("workload_profile"),
                budget_ceiling=build_draft.get("budget_ceiling"),
            )
        st.session_state["build_draft_analysis"] = response.model_dump()
        st.rerun()

    analysis = st.session_state.get("build_draft_analysis")
    if not analysis:
        return

    st.write(analysis["insights"]["summary"])

    if analysis["synergy"]["positive_synergies"]:
        st.markdown("**Working well:**")
        for note in analysis["synergy"]["positive_synergies"]:
            st.markdown(theme.tag(note, "success"), unsafe_allow_html=True)

    if analysis["synergy"]["negative_conflicts"]:
        st.markdown("**Conflicts:**")
        for conflict in analysis["synergy"]["negative_conflicts"]:
            st.markdown(theme.tag(conflict, "warning"), unsafe_allow_html=True)

    if analysis["insights"]["upgrade_path"]:
        st.markdown("**Upgrade path:**")
        for suggestion in analysis["insights"]["upgrade_path"]:
            st.write(f"- {suggestion}")


def _save_actions(build_draft: dict, build_state: dict) -> None:
    st.markdown("---")
    st.subheader("Save this build")

    user = current_user()
    name = st.text_input("Build name", value=build_draft.get("name", ""), key="build_name_input")
    publish = st.checkbox("Also publish to Community", key="publish_checkbox")

    if st.button("💾 Save build", key="save_build", disabled=not build_state, type="primary"):
        report = evaluate_build(build_state)
        analysis = st.session_state.get("build_draft_analysis") or {}
        synergy = analysis.get("synergy", {}).get("overall_score")
        bottleneck = analysis.get("bottleneck", {}).get("bottleneck_percentage")

        build = builds_repo.create_build(
            user_id=user["id"],
            name=name or "Untitled build",
            creation_mode=build_draft.get("creation_mode") or "Free",
            components=[builds_repo.BuildComponentInput(component_id=c.id) for c in build_state.values()],
            total_cost=state.build_total_cost(build_state),
            compatibility_score=report.compatibility_score,
            workload_profile=build_draft.get("workload_profile"),
            budget_ceiling=build_draft.get("budget_ceiling"),
            synergy_score=synergy,
            bottleneck_percentage=bottleneck,
            is_public=publish,
        )
        if publish:
            community_repo.create_post(build.id, user["id"], name or "Untitled build", None)

        st.success("Build saved!")
        st.session_state["create_mode"] = None
        st.session_state["build_draft"] = None
        st.session_state["build_draft_analysis"] = None
        st.session_state["page"] = "my_builds"
        st.rerun()


def render() -> None:
    st.title("Build Studio")

    if st.session_state.get("create_mode") is None or st.session_state.get("build_draft") is None:
        _mode_selector()
        return

    build_draft = st.session_state["build_draft"]
    mode = build_draft.get("creation_mode")

    header_cols = st.columns([1, 5])
    with header_cols[0]:
        if st.button("⬅ Change mode", key="change_mode"):
            st.session_state["create_mode"] = None
            st.session_state["build_draft"] = None
            st.session_state["build_draft_analysis"] = None
            st.rerun()
    with header_cols[1]:
        st.caption(f"Mode: **{humanize_profile(mode)}**")

    if mode == "Budget":
        _budget_controls(build_draft)
    elif mode == "Workload":
        _workload_controls(build_draft)

    build_state = state.resolve_build_state(build_draft)
    _summary_header(build_state)

    _sort_controls()
    _part_pickers(build_draft, build_state)

    # re-resolve after the picker section: a Select/Remove click above
    # already triggers st.rerun(), but this keeps the analysis/save section
    # consistent within a single render pass too.
    build_state = state.resolve_build_state(build_draft)
    _analysis_panel(build_draft, build_state)
    _save_actions(build_draft, build_state)
