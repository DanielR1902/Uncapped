"""Community forum (spec.md §7.6). `render()` also resolves a one-shot
`pending_community_filters` staging key (`_apply_pending_community_filters`)
that `ui/components/chat_assistant.py` sets when a Concierge `navigate`
action targets this page with a `filters` payload — see that function's
docstring."""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.repositories import builds_repo, community_repo, users_repo
from ui import state
from ui.components.build_card import render_build_card
from ui.format import humanize_profile, sanitize_markdown


def _post_subtitle(build, created_at) -> str:
    if (
        build.creation_mode == "Workload"
        and build.workload_profile is not None
        and build.workload_tier is not None
    ):
        domain = humanize_profile(build.workload_profile)
        return f"Workload build · {domain} · Tier: {build.workload_tier} · {created_at:%Y-%m-%d}"
    return f"{build.creation_mode} build · {created_at:%Y-%m-%d}"


def _fork_into_studio(build) -> None:
    draft = state.new_build_draft(build.creation_mode or "Free")
    draft["name"] = f"{build.name} (fork)"
    draft["workload_profile"] = build.workload_profile
    draft["budget_ceiling"] = build.budget_ceiling
    draft["components"] = {bc.category: bc.component_id for bc in build.components}

    st.session_state["fork_source_build_id"] = build.id
    st.session_state["build_draft"] = draft
    st.session_state["create_mode"] = build.creation_mode or "Free"
    st.session_state["build_draft_analysis"] = None
    # Unlike ui/views/drafts.py's "Load into Builder" (which replays
    # components through state.set_component/set_quantity — each of which
    # already sets this flag), this function writes draft["components"] as a
    # direct dict literal, bypassing those setters entirely. Set explicitly
    # here so a freshly-forked build is correctly tracked as having unsaved
    # picks, consistent with every other way a build_draft gets populated.
    st.session_state["has_unsaved_build_changes"] = True
    st.session_state["page"] = "create_build"
    st.rerun()


def _save_to_my_builds(build) -> None:
    user = current_user()
    builds_repo.create_build(
        user_id=user["id"],
        name=f"{build.name} (from community)",
        creation_mode=build.creation_mode,
        components=[
            builds_repo.BuildComponentInput(component_id=bc.component_id, quantity=bc.quantity)
            for bc in build.components
        ],
        total_cost=build.total_cost,
        compatibility_score=build.compatibility_score,
        workload_profile=build.workload_profile,
        workload_tier=build.workload_tier,
        budget_ceiling=build.budget_ceiling,
        synergy_score=build.synergy_score,
        bottleneck_percentage=build.bottleneck_percentage,
    )
    st.success("Saved to your builds!")


def _submit_comment(post_id: int) -> None:
    """on_click callback (see _comments_section) — runs before the widget is
    re-instantiated on the next rerun, which is the only reliable point at
    which a text_area's own session_state value can be cleared. Without this,
    a posted comment's text stayed in the box, and clicking "Post comment"
    again with nothing changed silently created a duplicate comment."""
    content = st.session_state.get("new_comment_input", "").strip()
    if content:
        community_repo.add_comment(post_id, current_user()["id"], content)
    st.session_state["new_comment_input"] = ""


@st.fragment
def _comments_section(post) -> None:
    """Fragment-scoped so posting a comment only re-renders this box, not the
    whole thread page above it (header, cost breakdown, component list) —
    keeps the page from fully reflowing/jumping back to the top on submit."""
    st.markdown("#### Comments")
    comments = community_repo.get_comments(post.id)
    for comment in comments:
        author = users_repo.get_by_id(comment.user_id)
        author_name = author.full_name if author is not None else "Unknown user"
        st.markdown(f"**{author_name}** · {comment.created_at:%Y-%m-%d %H:%M}")
        st.write(comment.content)

    st.text_area("Add a comment", key="new_comment_input")
    st.button("Post comment", key="post_comment", on_click=_submit_comment, args=(post.id,))


def _thread_view(post) -> None:
    if st.button("⬅ Back to feed", key="back_to_feed"):
        st.session_state["selected_post_id"] = None
        st.rerun()

    st.title(post.title)
    st.caption(f"by {post.user.full_name} · {post.created_at:%Y-%m-%d}")
    if post.author_notes:
        st.markdown(post.author_notes)

    render_build_card(post.build)

    st.markdown("#### Components")
    for bc in post.build.components:
        # A quantity > 1 (only ever meaningful for RAM/Storage — every other
        # category is implicitly 1x) gets an "(xN)" badge appended directly
        # to the category label, with the line's own price reflecting the
        # real total for that many units (unit price × quantity), not just
        # one unit's price — e.g. "Storage (x3): WD Black SN850X 2TB
        # ($477.00)" for 3 units at $159.00 each.
        label = f"{bc.category} (x{bc.quantity})" if bc.quantity > 1 else bc.category
        total_price = bc.component.price_usd * bc.quantity
        st.write(f"- **{label}**: {bc.component.name} (${total_price:,.2f})")

    cols = st.columns(2)
    if cols[0].button("🍴 Fork / Customize", key="fork_build", use_container_width=True):
        _fork_into_studio(post.build)
    if cols[1].button("💾 Save to My Builds", key="save_to_my_builds", use_container_width=True):
        _save_to_my_builds(post.build)

    _comments_section(post)


def _budget_price_steps(posts) -> list[int]:
    """Real `$1,000`-then-`$500`-step price-ceiling options, stopping at the
    smallest step that covers the current most expensive shared Budget
    build — empty when no Budget builds are currently shared. Factored out
    of `render()` so this and `_apply_pending_community_filters` (the
    Concierge-staged filter resolver below) always compute from the exact
    same real data, rather than risking two copies of this logic drifting
    apart over time."""
    budget_costs = [p.build.total_cost for p in posts if p.build.creation_mode == "Budget"]
    if not budget_costs:
        return []
    max_cost = max(budget_costs)
    steps = []
    step = 1000
    while True:
        steps.append(step)
        if step >= max_cost:
            break
        step += 500
    return steps


def _workload_domains(posts) -> list[str]:
    """Real, `humanize_profile`'d, currently-shared Workload domains, sorted
    alphabetically — same single-source-of-truth reasoning as
    `_budget_price_steps`."""
    return sorted(
        {
            humanize_profile(p.build.workload_profile)
            for p in posts
            if p.build.creation_mode == "Workload" and p.build.workload_profile is not None
        }
    )


def _workload_tiers(posts) -> list[str]:
    """Real, currently-shared Workload tiers, sorted in Entry->Mid->High->
    Enthusiast order (not alphabetically) — same single-source-of-truth
    reasoning as `_budget_price_steps`."""
    return sorted(
        {
            p.build.workload_tier
            for p in posts
            if p.build.creation_mode == "Workload" and p.build.workload_tier is not None
        },
        key=lambda t: ("Entry", "Mid", "High", "Enthusiast").index(t),
    )


def _match_option(requested: str, options: list[str]) -> str:
    """Case/whitespace-insensitive match of a Concierge-requested domain/tier
    string against the REAL, currently-shared `options` list ("gaming" ==
    "Gaming"; "VideoEditing" == "Video Editing" == "video editing") —
    returns "All" (a guaranteed-real selectbox option) on no match, never a
    raw string outside `options` that could crash a keyed `st.selectbox`."""
    target = requested.strip().lower().replace(" ", "")
    for option in options:
        if option.lower().replace(" ", "") == target:
            return option
    return "All"


def _apply_pending_community_filters(posts) -> None:
    """One-shot translation of a Concierge-staged `pending_community_filters`
    dict (set by `ui/components/chat_assistant.py::_apply_concierge_action`
    when a `navigate` action targeting "community" carries a `filters`
    payload — see that module's docstring) into this view's REAL widget
    session-state keys (`community_mode_filter`/`community_price_filter`/
    `community_domain_filter`/`community_tier_filter`), computed from the
    exact same real, currently-shared `posts` data the widgets below use
    (`_budget_price_steps`/`_workload_domains`/`_workload_tiers`) — never a
    second, independently-maintained list that could drift from what's
    actually displayed this render.

    Deliberately a DISTINCT key from the real widget keys above:
    `chat_assistant.py` cannot resolve a requested filter value into one of
    THIS view's real, live option strings itself (it has no access to the
    current community feed's price/domain/tier data at the point it applies
    the action), so it only stages the raw request here for this function —
    the one place that already has `posts` — to translate.

    `st.session_state.pop` (not `.get`) makes this strictly one-shot: once
    consumed, it cannot re-apply itself on a later rerun, so a manual filter
    change the user makes afterward is never silently overwritten again.

    Every value this function writes is guaranteed to already be one of the
    real options the corresponding selectbox will render this same pass —
    `build_type` falls back to "All" if it's somehow not one of the 4 real
    mode-filter options; `max_price`/`domain`/`tier` are resolved against
    `_budget_price_steps`/`_workload_domains`/`_workload_tiers` via
    `_match_option`, falling back to "All Prices"/"All" (themselves always-
    real options) rather than ever writing a string absent from a keyed
    selectbox's own `options` list — the exact class of `StreamlitAPIException`
    footgun `ui/CLAUDE.md`'s existing `number_input` pre-clamp precedent
    warns about for a different widget type. Confirmed live via
    `streamlit.testing.v1.AppTest` (see `tests/test_ui_smoke.py`) that a
    mismatched/unmatched request degrades gracefully instead of raising."""
    pending = st.session_state.pop("pending_community_filters", None)
    if not pending:
        return

    build_type = pending.get("build_type")
    if build_type not in ("All", "Budget", "Workload", "Free"):
        build_type = "All"
    st.session_state["community_mode_filter"] = build_type

    if build_type == "Budget":
        price_steps = _budget_price_steps(posts)
        price_choice = "All Prices"
        max_price = pending.get("max_price")
        if price_steps and max_price is not None:
            covering = [step for step in price_steps if step >= max_price]
            chosen = min(covering) if covering else price_steps[-1]
            price_choice = f"${chosen:,.0f}"
        st.session_state["community_price_filter"] = price_choice
    elif build_type == "Workload":
        domains = _workload_domains(posts)
        tiers = _workload_tiers(posts)

        requested_domain = pending.get("domain")
        st.session_state["community_domain_filter"] = (
            _match_option(requested_domain, domains) if requested_domain else "All"
        )

        requested_tier = pending.get("tier")
        st.session_state["community_tier_filter"] = (
            _match_option(requested_tier, tiers) if requested_tier else "All"
        )


def render() -> None:
    selected_id = st.session_state.get("selected_post_id")
    if selected_id is not None:
        post = community_repo.get_post(selected_id)
        if post is not None:
            _thread_view(post)
            return
        st.session_state["selected_post_id"] = None  # stale reference, fall through to feed

    st.title("Community")
    posts = community_repo.get_feed()
    if not posts:
        st.info("No builds shared yet — be the first from your Previous Builds page!")
        return

    # One-shot Concierge-staged filter resolution — must run before the
    # mode/price/domain/tier widgets below are instantiated, since it writes
    # directly to their own session-state keys for them to pick up as their
    # current value on this same render.
    _apply_pending_community_filters(posts)

    mode_filter = st.selectbox(
        "Filter by Build Type", ["All", "Budget", "Workload", "Free"], key="community_mode_filter"
    )

    price_ceiling: float | None = None
    domain_filter = "All"
    tier_filter = "All"

    if mode_filter == "Budget":
        price_steps = _budget_price_steps(posts)
        if price_steps:
            price_choice = st.selectbox(
                "Max Price Limit",
                ["All Prices"] + [f"${p:,.0f}" for p in price_steps],
                key="community_price_filter",
            )
            if price_choice != "All Prices":
                price_ceiling = float(price_choice.replace("$", "").replace(",", ""))
    elif mode_filter == "Workload":
        col_domain, col_tier = st.columns(2)
        domains = _workload_domains(posts)
        tiers = _workload_tiers(posts)
        domain_filter = col_domain.selectbox("Domain", ["All"] + domains, key="community_domain_filter")
        tier_filter = col_tier.selectbox("Tier", ["All"] + tiers, key="community_tier_filter")

    filtered_posts = posts
    if mode_filter != "All":
        filtered_posts = [p for p in filtered_posts if p.build.creation_mode == mode_filter]
    if mode_filter == "Budget" and price_ceiling is not None:
        filtered_posts = [p for p in filtered_posts if p.build.total_cost <= price_ceiling]
    if mode_filter == "Workload":
        if domain_filter != "All":
            filtered_posts = [
                p
                for p in filtered_posts
                if p.build.workload_profile is not None
                and humanize_profile(p.build.workload_profile) == domain_filter
            ]
        if tier_filter != "All":
            filtered_posts = [p for p in filtered_posts if p.build.workload_tier == tier_filter]

    if not filtered_posts:
        st.info("No community builds match the selected filters.")
        return

    for post in filtered_posts:
        with st.container(border=True):
            st.markdown(sanitize_markdown(f"### {post.title} | ${post.build.total_cost:,.2f}"))
            st.caption(_post_subtitle(post.build, post.created_at))
            if st.button("View", key=f"view_post_{post.id}"):
                st.session_state["selected_post_id"] = post.id
                st.rerun()
