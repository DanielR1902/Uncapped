"""PC Build Studio — Mode A/B/C (spec.md §7.4, intent.txt §3)."""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.models import WORKLOAD_PROFILES
from db.repositories import builds_repo, community_repo, components_repo
from engine import solvers
from engine.compatibility import evaluate_build
from llm.client import analyze_build
from ui import state, theme
from ui.components.part_picker import SORT_OPTIONS, render_part_picker
from ui.format import humanize_profile

CORE_CATEGORIES = solvers.CATEGORY_ORDER
PERIPHERAL_CATEGORIES = ("NetworkCard", "SoundCard", "OpticalDrive")
TIERS = ("Entry", "Mid", "High", "Enthusiast")


def _mode_selector() -> None:
    st.subheader("Choose how you'd like to build")
    cols = st.columns(3)
    if cols[0].button("💰 Budget Constrained", key="mode_budget", use_container_width=True):
        st.session_state["create_mode"] = "Budget"
        st.session_state["build_draft"] = state.new_build_draft("Budget")
        st.session_state["build_draft_analysis"] = None
        st.rerun()
    if cols[1].button("🎯 Workload Profile", key="mode_workload", use_container_width=True):
        st.session_state["create_mode"] = "Workload"
        st.session_state["build_draft"] = state.new_build_draft("Workload")
        st.session_state["build_draft_analysis"] = None
        st.rerun()
    if cols[2].button("🧩 Free Custom", key="mode_free", use_container_width=True):
        st.session_state["create_mode"] = "Free"
        st.session_state["build_draft"] = state.new_build_draft("Free")
        st.session_state["build_draft_analysis"] = None
        st.rerun()


def _budget_controls(build_draft: dict) -> None:
    ceiling = st.number_input(
        "Budget ceiling ($)", min_value=100.0, value=build_draft.get("budget_ceiling") or 1500.0, step=50.0,
        key="budget_ceiling_input",
    )
    if st.button("Generate starting build", key="generate_budget_build"):
        build_draft["budget_ceiling"] = ceiling
        seed_selection = state.resolve_build_state(build_draft)  # keep whatever the user already pinned
        new_selection = solvers.initialize_budget_build(ceiling, seed_selection=seed_selection or None)
        build_draft["components"] = {category: component.id for category, component in new_selection.items()}
        st.session_state["build_draft_analysis"] = None
        st.rerun()


def _workload_controls(build_draft: dict) -> None:
    profile = st.selectbox(
        "Workload profile", WORKLOAD_PROFILES, format_func=humanize_profile, key="workload_profile_input",
    )
    tier = st.selectbox("Tier", TIERS, index=1, key="workload_tier_input")
    if st.button("Generate baseline build", key="generate_workload_build"):
        build_draft["workload_profile"] = profile
        build_draft["tier"] = tier
        new_selection = solvers.allocate_workload_baseline(profile, target_tier=tier)
        build_draft["components"] = {category: component.id for category, component in new_selection.items()}
        st.session_state["build_draft_analysis"] = None
        st.rerun()


def _sort_controls() -> None:
    current = st.session_state.get("sort_criteria", "Cost")
    st.session_state["sort_criteria"] = st.radio(
        "Sort candidates by", SORT_OPTIONS, index=SORT_OPTIONS.index(current), horizontal=True, key="sort_criteria_input",
    )


def _candidates_for(build_draft: dict, build_state: dict, category: str) -> list:
    others = {c: comp for c, comp in build_state.items() if c != category}
    candidates = solvers.get_compatible_candidates(category, others)

    ceiling = build_draft.get("budget_ceiling")
    if build_draft.get("creation_mode") == "Budget" and ceiling:
        spent_elsewhere = sum(comp.price_usd for comp in others.values())
        remaining = ceiling - spent_elsewhere
        affordable = [c for c in candidates if c.price_usd <= remaining]
        return affordable or candidates  # never hide every option, even if all exceed what's left
    return candidates


def _part_pickers(build_draft: dict, build_state: dict) -> None:
    for category in CORE_CATEGORIES:
        candidates = _candidates_for(build_draft, build_state, category)
        render_part_picker(
            category,
            build_state,
            candidates,
            on_select=lambda component, cat=category: state.set_component(build_draft, cat, component),
            on_remove=lambda cat=category: state.remove_component(build_draft, cat),
        )

    with st.expander("Optional peripherals (Network Card, Sound Card, Optical Drive)"):
        for category in PERIPHERAL_CATEGORIES:
            candidates = components_repo.get_by_category(category)
            render_part_picker(
                category,
                build_state,
                candidates,
                on_select=lambda component, cat=category: state.set_component(build_draft, cat, component),
                on_remove=lambda cat=category: state.remove_component(build_draft, cat),
            )


def _analysis_card(build_draft: dict, build_state: dict) -> None:
    st.markdown("##### Synergy & Bottleneck")

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

    is_ai = analysis["source"] == "llm"
    badge_html = theme.badge("AI Engine" if is_ai else "Heuristic Baseline", "ai" if is_ai else "heuristic")
    st.markdown(badge_html, unsafe_allow_html=True)

    cols = st.columns(2)
    cols[0].metric("Synergy score", f"{analysis['synergy']['overall_score']:.0f}")
    cols[1].metric(
        "Bottleneck",
        f"{analysis['bottleneck']['bottleneck_percentage']:.0f}%",
        help=f"Limiting component: {analysis['bottleneck']['limiting_component']}",
    )
    st.write(analysis["insights"]["summary"])

    if analysis["synergy"]["negative_conflicts"]:
        st.markdown("**Conflicts:**")
        for conflict in analysis["synergy"]["negative_conflicts"]:
            st.markdown(theme.tag(conflict, "warning"), unsafe_allow_html=True)

    if analysis["insights"]["upgrade_path"]:
        st.markdown("**Upgrade path:**")
        for suggestion in analysis["insights"]["upgrade_path"]:
            st.write(f"- {suggestion}")


def _summary_bar(build_draft: dict, build_state: dict) -> None:
    st.markdown("---")
    st.subheader("Build summary")

    total_cost = state.build_total_cost(build_state)
    report = evaluate_build(build_state)

    cols = st.columns(3)
    cols[0].metric("Total cost", f"${total_cost:,.2f}")
    cols[1].metric("Compatibility", f"{report.compatibility_score:.0f}%")
    cols[2].metric("Status", "✅ Compatible" if report.is_compatible else "⚠️ Issues found")

    if report.issues:
        for issue in report.issues:
            st.markdown(theme.tag(issue, "danger"), unsafe_allow_html=True)

    _analysis_card(build_draft, build_state)


def _save_actions(build_draft: dict, build_state: dict) -> None:
    st.markdown("---")
    st.subheader("Save this build")

    user = current_user()
    name = st.text_input("Build name", value=build_draft.get("name", ""), key="build_name_input")
    publish = st.checkbox("Also publish to Community", key="publish_checkbox")

    if st.button("💾 Save build", key="save_build", disabled=not build_state):
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

    if st.button("⬅ Change mode", key="change_mode"):
        st.session_state["create_mode"] = None
        st.session_state["build_draft"] = None
        st.session_state["build_draft_analysis"] = None
        st.rerun()

    if mode == "Budget":
        _budget_controls(build_draft)
    elif mode == "Workload":
        _workload_controls(build_draft)

    _sort_controls()

    build_state = state.resolve_build_state(build_draft)
    _part_pickers(build_draft, build_state)

    # re-resolve once after the picker section: a Select/Remove click above
    # already triggers st.rerun(), but this keeps the summary/save section
    # consistent within a single render pass too.
    build_state = state.resolve_build_state(build_draft)
    _summary_bar(build_draft, build_state)
    _save_actions(build_draft, build_state)
