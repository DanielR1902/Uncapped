"""PC Build Studio — Mode A/B/C (spec.md §7.4, intent.txt §3).

Layout: mode selector (or a "change mode" header) → mode-specific generator
controls → a summary header that stays visible above the part-picker grid
(four clean numeric metric cards — Total Cost, Compatibility, Synergy,
Bottleneck; the LLM-backed synergy/bottleneck read still auto-runs the
instant a build first becomes complete in ANY mode, see
`_maybe_auto_analyze`, it just no longer surfaces a source badge or a
manual re-trigger button — a deliberate decluttering, not an oversight) →
an explicit "✨ Get AI Analysis & Upgrade Path" advisory button (see
`_advisory_controls`; a separate, click-gated feature from the auto-run
synergy/bottleneck read above it, available in every mode) → a
"Reset All Fields" control → the 8 core slots in a 2-column grid +
optional peripherals → save/publish (with an optional community post
description when publishing).
"""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.models import WORKLOAD_PROFILES
from db.repositories import builds_repo, community_repo, components_repo
from engine import scoring, solvers
from engine.compatibility import evaluate_build
from llm.advisory import get_build_advisory
from llm.client import analyze_build
from ui import state, theme
from ui.components.part_picker import render_part_picker
from ui.format import humanize_profile

CORE_CATEGORIES = solvers.CATEGORY_ORDER
PERIPHERAL_CATEGORIES = solvers.PERIPHERAL_CATEGORIES
TIERS = ("Entry", "Mid", "High", "Enthusiast")

_MODE_CARDS = (
    ("Budget", "💰 Budget Constrained", "Set a spending ceiling — the solver converges on the most capable build that still fits under it."),
    ("Workload", "🎯 Workload Profile", "Pick what it's for — Gaming, Video Editing, Programming/AI, Design, or General — and get a tiered baseline instantly."),
    ("Free", "🧩 Free Custom", "Full control from the first part. Every pick is checked live for compatibility against everything else."),
)


def _sanitize_markdown(text: str) -> str:
    """Defensively strip any literal `$` from LLM-authored text before it
    reaches st.markdown/st.write. The advisory system prompt already
    instructs the model to never use standalone dollar signs (a matching
    "$...$" pair triggers Streamlit's KaTeX/math-mode rendering and garbles
    plain prices), but this is a last-resort belt-and-suspenders guard in
    case the model ignores that instruction anyway."""
    return text.replace("$", "USD ")


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


def _cached_floor_cost() -> float:
    """`solvers.minimum_possible_build_cost()` is an exhaustive search (~2s
    against a realistically-sized catalog — see its docstring) — the
    catalog never changes during a running session, so this only needs to
    run once per session rather than on every single "Apply budget &
    generate build" click (which would otherwise pay that cost every time,
    even when the entered ceiling is nowhere near the floor)."""
    if "_budget_floor_cost" not in st.session_state:
        st.session_state["_budget_floor_cost"] = solvers.minimum_possible_build_cost()
    return st.session_state["_budget_floor_cost"]


def _apply_budget_and_generate(build_draft: dict) -> None:
    """on_click callback for the unified "Apply budget & generate build"
    button. Runs BEFORE the next script rerun, which is the only point at
    which it's legal to overwrite the ceiling number_input's own
    st.session_state["budget_ceiling_input"] value — doing that inside a
    plain post-widget button block would raise StreamlitAPIException, since
    the widget has already been instantiated earlier in the same run.
    Floor-clamps whatever the user typed, commits it to build_draft, and
    immediately generates a build against it — all in this one callback, so
    the very next render already reflects the fully-applied result
    (ceiling, components, and every part-picker's headroom) in a single
    rerun.

    Deliberately a full from-scratch regenerate, not an "adjust my existing
    picks to the new ceiling" one: every core and peripheral selection is
    cleared first, then `initialize_budget_build` runs with no seed at all.
    This button is "give me a fresh optimal build for this budget," not
    "nudge what I already have." Manually picking one slot via its own
    picker (`_part_pickers`'s `on_select`) goes straight through
    `state.set_component` and does not re-run the solver at all — it's a
    plain, unconstrained pin with no re-partitioning of the rest of the
    build, no ceiling re-check, and no `on_user_pins_component` call (that
    entry point exists and is exercised at the engine level, but nothing in
    this UI currently calls it)."""
    floor_cost = _cached_floor_cost()
    entered = st.session_state.get("budget_ceiling_input", floor_cost)

    if entered < floor_cost:
        st.session_state["budget_ceiling_input"] = floor_cost
        ceiling = floor_cost
        st.toast(f"Budget set to minimum viable floor: ${floor_cost:,.2f}", icon="⚠️")
    else:
        ceiling = entered

    build_draft["budget_ceiling"] = ceiling
    build_draft["components"] = {}  # wipe every existing core/peripheral selection before regenerating

    new_selection = solvers.initialize_budget_build(ceiling, fill_peripherals_with_surplus=True)

    build_draft["components"] = {category: component.id for category, component in new_selection.items()}
    st.session_state["build_draft_analysis"] = None


def _budget_controls(build_draft: dict) -> None:
    with st.container(border=True):
        st.caption("💰 **Budget mode** — the solver spends as much of your ceiling as it can on the highest-tier compatible parts.")

        unlimited = st.checkbox(
            "No limit — show every compatible part, skip budget filtering",
            key="budget_unlimited_input",
        )
        if unlimited:
            build_draft["budget_ceiling"] = None

        st.number_input(
            "Budget ceiling ($)",
            value=build_draft.get("budget_ceiling") or 1500.0, step=50.0,
            key="budget_ceiling_input", disabled=unlimited,
        )

        st.button(
            "Apply budget & generate build", key="apply_budget_generate", type="primary",
            disabled=unlimited, on_click=_apply_budget_and_generate, args=(build_draft,),
        )

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
            new_selection = solvers.allocate_workload_baseline(profile, target_tier=tier, include_peripherals=True)
            build_draft["components"] = {category: component.id for category, component in new_selection.items()}
            st.session_state["build_draft_analysis"] = None
            st.rerun()


def _reset_controls(build_draft: dict) -> None:
    if st.button("🔄 Reset All Fields", key="reset_all_fields"):
        mode = build_draft.get("creation_mode")
        st.session_state["build_draft"] = state.new_build_draft(mode)
        st.session_state["build_draft_analysis"] = None
        # Clear the mode-specific widgets' own remembered state too, or
        # they'd keep showing whatever the user last typed/picked instead of
        # falling back to new_build_draft's clean defaults on the next
        # render (a widget's `key`-bound session_state entry always wins
        # over its `value=`/`index=` default once it exists).
        st.session_state.pop("budget_ceiling_input", None)
        st.session_state.pop("budget_unlimited_input", None)
        st.session_state.pop("workload_profile_input", None)
        st.session_state.pop("workload_tier_input", None)
        st.rerun()


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
                    budget_ceiling=build_draft.get("budget_ceiling"),
                )


def _maybe_auto_analyze(build_draft: dict, build_state: dict) -> None:
    """Auto-run the LLM analysis the instant a build first becomes complete
    (all 8 core categories filled) — across every creation flow (Budget's
    "Apply budget & generate build", Workload's "Generate baseline build",
    or simply finishing the last manual pick in Free mode) — so the summary
    banner shows AI Engine metrics on the very next render, with no manual
    "Analyze" click required.

    A single hook point in `render()` covers all three flows uniformly
    rather than duplicating a trigger call in each button handler, because
    they already share one signal for "this needs (re-)analysis":
    `ui/state.py`'s `set_component`/`remove_component` (used by every
    picker's on_select/on_remove) and every generate handler already clear
    `build_draft_analysis` to `None` on any component change. So the only
    two conditions to check here are "no analysis on file yet" and "the
    build is actually complete" — never on every single rerun, only once
    per distinct complete build state.

    No separate cache/hash bookkeeping is needed beyond that: `analyze_build`
    itself already checks `llm_cache` (keyed by the exact sorted component
    id set) before ever making a network call, so even the FIRST time this
    fires for a given session, if that exact build was already analyzed
    previously (this session or another), it's a cache hit, not a new
    OpenRouter call."""
    if st.session_state.get("build_draft_analysis") is not None:
        return
    if not set(solvers.CATEGORY_ORDER).issubset(build_state.keys()):
        return
    with st.spinner("Analyzing build..."):
        response = analyze_build(
            build_state,
            workload_profile=build_draft.get("workload_profile"),
            budget_ceiling=build_draft.get("budget_ceiling"),
        )
    st.session_state["build_draft_analysis"] = response.model_dump()


def _summary_header(build_draft: dict, build_state: dict) -> None:
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
                help="Local estimate — the full AI/heuristic breakdown appears automatically once the build is complete.",
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

        if report.issues:
            for issue in report.issues:
                st.markdown(theme.tag(issue, "danger"), unsafe_allow_html=True)


def _advisory_controls(build_draft: dict, build_state: dict) -> None:
    """Explicit-click AI Build Advisory (in-budget optimization tips +
    a stretch-budget upgrade path), rendered below the summary metric cards
    for all three creation modes — unlike `_maybe_auto_analyze`'s synergy/
    bottleneck read, this one is never auto-triggered; it costs a real (or
    heuristic-fallback) `get_build_advisory` call per click, so it stays
    behind an explicit button, same gating value (`len(build_state) < 2`)
    the old manual "🔮 Analyze" button used before auto-analysis replaced it.

    Cached in `st.session_state["advisory_cache"]` keyed by a deterministic
    tuple of (sorted (category, component.id) pairs, mode, current budget-
    or-cost figure) — component id rather than the Component row itself,
    since a SQLAlchemy model instance isn't reliably hashable/sortable
    across reruns. This mirrors `_cached_floor_cost` and `llm/cache.py`'s
    `llm_cache` table: don't repeat expensive (here, potentially networked)
    work for an unchanged build just because an unrelated widget elsewhere
    on the page triggered a Streamlit rerun.

    Which cached result is "currently displayed" is decided by recomputing
    the CURRENT build's cache key on every render and looking IT up in the
    cache, rather than remembering "whatever key was last clicked" in a
    separate session-state slot. That means: (a) the expander keeps showing
    the same result across reruns that don't touch the build (no need to
    re-click), and (b) the moment the build/mode/cost actually changes, the
    new cache key simply has no entry yet, so the stale advisory silently
    stops being displayed instead of lingering for a build it no longer
    describes — the same "a change invalidates the old read" philosophy
    `ui/state.py`'s `_invalidate_analysis` already applies to the synergy/
    bottleneck card.
    """
    mode = build_draft.get("creation_mode")
    current_budget_or_cost = build_draft.get("budget_ceiling") if mode == "Budget" else None
    if not current_budget_or_cost:
        current_budget_or_cost = state.build_total_cost(build_state)

    cache_key = (
        mode,
        build_draft.get("workload_profile"),
        tuple(sorted((category, component.id) for category, component in build_state.items())),
        round(current_budget_or_cost, 2),
    )
    cache = st.session_state.setdefault("advisory_cache", {})

    if st.button(
        "✨ Get AI Analysis & Upgrade Path",
        key="get_advisory",
        use_container_width=True,
        disabled=len(build_state) < 2,
    ):
        if cache_key not in cache:
            with st.spinner("Getting AI advisory..."):
                cache[cache_key] = get_build_advisory(
                    build_state,
                    mode,
                    current_budget_or_cost,
                    profile=build_draft.get("workload_profile"),
                    bottleneck_info=(st.session_state.get("build_draft_analysis") or {}).get("bottleneck"),
                )

    advisory = cache.get(cache_key)
    if advisory is None:
        return

    stretch_amount = round(current_budget_or_cost * 0.10 / 10) * 10
    with st.expander("💡 AI Build Advisory & Recommendations", expanded=True):
        st.caption(
            "Source: AI-generated" if advisory["source"] == "llm" else "Source: local heuristic estimate"
        )

        col_pros, col_cons = st.columns(2)
        with col_pros:
            st.markdown("##### :green[✔ Pros & Strengths]")
            for item in advisory.get("pros", []):
                st.markdown(f"- :green[{_sanitize_markdown(item)}]")
        with col_cons:
            st.markdown("##### :red[✖ Cons & Limitations]")
            for item in advisory.get("cons", []):
                st.markdown(f"- :red[{_sanitize_markdown(item)}]")
        st.divider()

        tab1, tab2 = st.tabs([
            "⚖️ In-Budget Optimization & Balance",
            f"🚀 Stretch Budget Upgrades (+{stretch_amount:,.0f} USD)",
        ])
        with tab1:
            st.markdown(_sanitize_markdown(advisory["within_budget"]))
        with tab2:
            st.markdown(_sanitize_markdown(advisory["stretch_budget"]))


def _save_actions(build_draft: dict, build_state: dict) -> None:
    st.markdown("---")
    st.subheader("Save this build")

    user = current_user()
    name = st.text_input("Build name", value=build_draft.get("name", ""), key="build_name_input")
    publish = st.checkbox("Also publish to Community", key="publish_checkbox")

    community_description = ""
    if publish:
        community_description = st.text_area(
            "Community post description",
            placeholder="Share your thoughts, use-case, or notes about this build...",
            key="community_description_input",
        )

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
            community_repo.create_post(build.id, user["id"], name or "Untitled build", community_description or None)

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
    _maybe_auto_analyze(build_draft, build_state)
    _summary_header(build_draft, build_state)
    _advisory_controls(build_draft, build_state)

    _reset_controls(build_draft)
    _part_pickers(build_draft, build_state)

    # re-resolve after the picker section: a Select/Remove click above
    # already triggers st.rerun(), but this keeps the save section
    # consistent within a single render pass too.
    build_state = state.resolve_build_state(build_draft)
    _save_actions(build_draft, build_state)
