"""Community forum (spec.md §7.6). `render()` also resolves a one-shot
`pending_community_filters` staging key (`_apply_pending_community_filters`)
that `ui/components/chat_assistant.py` sets when a Concierge `navigate`
action targets this page with a `filters` payload — see that function's
docstring.

Posts are shown strictly chronologically, newest first (`community_repo.get_feed`)
— there is no voting/scoring mechanism (a prior Reddit-style upvote/downvote
implementation was removed entirely by a later product decision; see git
history). Comments support threaded replies (`CommunityComment.parent_comment_id`)
— `community_repo.get_comments` still returns a FLAT, chronological list
(unchanged, so existing callers/tests keep working); grouping that flat list
into a parent/child tree and rendering it with indentation is this view's own
job (`_group_comments_by_parent`/`_render_comment_node`), not the
repository's — building a presentation tree from real data is a UI concern,
not business logic (db/CLAUDE.md)."""
from __future__ import annotations

import html
from collections import defaultdict

import streamlit as st

from auth.session import current_user
from db.repositories import builds_repo, community_repo, users_repo
from ui import state, theme
from ui.components.build_card import render_build_card
from ui.format import format_currency, humanize_profile, sanitize_markdown, time_ago

# Reply nesting keeps growing visually indented up to this many levels, then
# flattens out (still a real reply relationship in the data either way) —
# the same practical "don't run comments off the right edge of the screen"
# cap Reddit's own UI applies.
_MAX_VISUAL_REPLY_DEPTH = 6


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
    draft = state.load_components_into_new_draft(
        mode=build.creation_mode or "Free",
        components={bc.category: bc.component_id for bc in build.components},
        # A real, confirmed gap fixed alongside this refactor: the previous
        # direct-dict-literal version never carried quantities over at all,
        # so a RAM/Storage build with 2x+ units silently reverted to 1x on
        # fork. `load_components_into_new_draft` accepts them directly.
        quantities={bc.category: bc.quantity for bc in build.components},
        name=f"{build.name} (fork)",
    )
    draft["workload_profile"] = build.workload_profile
    draft["budget_ceiling"] = build.budget_ceiling

    st.session_state["fork_source_build_id"] = build.id
    st.session_state["build_draft"] = draft
    st.session_state["create_mode"] = build.creation_mode or "Free"
    st.session_state["build_draft_analysis"] = None
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


def _set_reply_target(comment_id: int | None) -> None:
    """on_click callback backing both the "Reply" button (opens the reply box
    under a specific comment, `comment_id`) and "Cancel" (`None`, closes
    whichever one is open). `community_reply_target` holds a real
    `CommunityComment.id` or `None` — a comment id is globally unique across
    every post, so this needs no per-post scoping: navigating to a
    DIFFERENT post while a reply box was open just means that id matches
    nothing in the new post's own comment list, and no reply box renders
    anywhere — self-correcting, no explicit cleanup needed."""
    st.session_state["community_reply_target"] = comment_id


def _submit_reply(post_id: int, parent_comment_id: int) -> None:
    """on_click callback for a specific comment's own "Post reply" button —
    same reasoning as `_submit_comment` for why this must run as a callback
    (clearing the text_area's OWN keyed value) rather than inline in the
    render pass. Each reply box gets its own widget key
    (`reply_input_{parent_comment_id}`) since, unlike the single top-level
    "Add a comment" box, more than one of these can exist in the comment
    tree's data even though only one is ever OPEN (rendered) at a time."""
    key = f"reply_input_{parent_comment_id}"
    content = st.session_state.get(key, "").strip()
    if content:
        community_repo.add_comment(post_id, current_user()["id"], content, parent_comment_id=parent_comment_id)
    st.session_state[key] = ""
    st.session_state["community_reply_target"] = None


def _group_comments_by_parent(comments: list) -> dict[int | None, list]:
    """`community_repo.get_comments`'s flat, chronological (oldest-first)
    list, regrouped by `parent_comment_id` (`None` for a top-level comment) —
    the shape `_render_comment_node` recurses over. Each group is already in
    chronological order (inherited from the input list's own order), so
    replies-to-the-same-parent render oldest-first too, matching the
    directive's "sorted chronologically" requirement at every level, not just
    the top one."""
    grouped: dict[int | None, list] = defaultdict(list)
    for comment in comments:
        grouped[comment.parent_comment_id].append(comment)
    return grouped


def _render_comment_node(post_id: int, comment, children_by_parent: dict, depth: int) -> None:
    """Recursively renders `comment` and every reply nested under it,
    visually indented one step deeper per level (capped at
    `_MAX_VISUAL_REPLY_DEPTH` — the underlying reply relationship itself is
    never capped, only how far right the indentation keeps shifting).

    Indentation uses `st.columns([spacer, content])`, NOT a raw injected HTML
    `<div style="margin-left:...">` — Streamlit widgets (the "Reply" button,
    the reply `st.text_area`) are each their own independent component in the
    page's DOM; they are never children of an `st.markdown(unsafe_allow_html=
    True)` call's injected HTML, so a CSS margin/indent on that markup alone
    would leave the interactive controls flush-left regardless of depth. A
    `st.columns` split is the real, Streamlit-native way to shift an entire
    block — text AND widgets — to the right. The visual "connector line" look
    (a left border) IS achieved via CSS (`.uncapped-comment-reply`,
    ui/theme.py) — that part only ever wraps the STATIC text content
    (avatar/name/timestamp/body), never a widget, so it's safe."""
    visual_depth = min(depth, _MAX_VISUAL_REPLY_DEPTH)
    if visual_depth > 0:
        _, content_col = st.columns([0.05 * visual_depth, 1 - 0.05 * visual_depth])
    else:
        content_col = st.container()

    with content_col:
        author = users_repo.get_by_id(comment.user_id)
        author_name = author.full_name if author is not None else "Unknown user"
        css_class = "uncapped-comment-reply" if depth > 0 else "uncapped-comment-top"
        st.markdown(
            f'<div class="{css_class}">'
            f'<strong>👤 {html.escape(author_name)}</strong> '
            f'<span class="uncapped-muted">· {time_ago(comment.created_at)}</span><br>'
            f'{html.escape(comment.content)}'
            f"</div>",
            unsafe_allow_html=True,
        )
        st.button("Reply", key=f"reply_btn_{comment.id}", on_click=_set_reply_target, args=(comment.id,))
        if st.session_state.get("community_reply_target") == comment.id:
            st.text_area("Your reply", key=f"reply_input_{comment.id}")
            reply_cols = st.columns(2)
            reply_cols[0].button(
                "Post reply",
                key=f"submit_reply_{comment.id}",
                type="primary",
                on_click=_submit_reply,
                args=(post_id, comment.id),
            )
            reply_cols[1].button(
                "Cancel", key=f"cancel_reply_{comment.id}", on_click=_set_reply_target, args=(None,)
            )

        for child in children_by_parent.get(comment.id, []):
            _render_comment_node(post_id, child, children_by_parent, depth + 1)


@st.fragment
def _comments_section(post) -> None:
    """Fragment-scoped so posting a comment/reply only re-renders this box,
    not the whole thread page above it (header, cost breakdown, component
    list) — keeps the page from fully reflowing/jumping back to the top on
    submit. Threaded (spec.md §7.6): `community_repo.get_comments` still
    returns a flat, chronological list — `_group_comments_by_parent` +
    `_render_comment_node` build and render the reply tree from it here."""
    st.markdown("#### Comments")
    comments = community_repo.get_comments(post.id)
    children_by_parent = _group_comments_by_parent(comments)
    for top_level_comment in children_by_parent.get(None, []):
        _render_comment_node(post.id, top_level_comment, children_by_parent, depth=0)

    st.text_area("Add a comment", key="new_comment_input")
    st.button("Post comment", key="post_comment", on_click=_submit_comment, args=(post.id,))


# Industrial hardware blueprint grouping (spec.md §7.6.1) — every
# BuildComponent's `.category` falls into exactly one of these two named
# groups, or (peripherals only) neither; grouping is purely a presentation
# concern for this view, never business logic (db/CLAUDE.md).
_BLUEPRINT_GROUPS = (
    ("Core Components", ("CPU", "GPU", "Motherboard", "RAM", "PSU", "Case")),
    ("Storage & Cooling", ("Storage", "Cooler")),
)


def _render_hardware_blueprint(components) -> None:
    """Groups a build's real, persisted `BuildComponent` rows into the
    industrial "hardware blueprint card" groups above (spec.md §7.6.1) —
    replaces the old flat bullet list with the same underlying data, just
    organized the way an actual spec sheet reads. A quantity > 1 (only ever
    meaningful for RAM/Storage) still gets an "(xN)" badge with the line's
    real per-line total (unit price × quantity), unchanged from before."""
    currency = st.session_state.get("selected_currency", "USD")
    by_category = {bc.category: bc for bc in components}
    grouped_categories: set[str] = set()

    for group_name, categories in _BLUEPRINT_GROUPS:
        rows = [by_category[cat] for cat in categories if cat in by_category]
        grouped_categories.update(categories)
        if not rows:
            continue
        st.markdown(theme.section_header(group_name), unsafe_allow_html=True)
        for bc in rows:
            label = f"{bc.category} (x{bc.quantity})" if bc.quantity > 1 else bc.category
            total_price = bc.component.price_usd * bc.quantity
            st.write(f"- **{label}**: {bc.component.name} ({format_currency(total_price, currency)})")

    leftover = [bc for cat, bc in by_category.items() if cat not in grouped_categories]
    if leftover:
        st.markdown(theme.section_header("Peripherals"), unsafe_allow_html=True)
        for bc in leftover:
            label = f"{bc.category} (x{bc.quantity})" if bc.quantity > 1 else bc.category
            total_price = bc.component.price_usd * bc.quantity
            st.write(f"- **{label}**: {bc.component.name} ({format_currency(total_price, currency)})")


def _thread_view(post) -> None:
    if st.button("⬅ Back to feed", key="back_to_feed"):
        st.session_state["selected_post_id"] = None
        st.rerun()

    # Clean header (spec.md §7.6): avatar + "u/{author}" + "• {time_ago}"
    # above a large, bold post title — author_name/content are real
    # user-supplied text, HTML-escaped before this unsafe_allow_html=True
    # markup (the same discipline theme.avatar_html itself already applies
    # internally).
    st.markdown(
        f'<div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">'
        f"{theme.avatar_html(post.user.full_name)}"
        f'<span style="font-weight:600;">u/{html.escape(post.user.full_name)}</span>'
        f'<span class="uncapped-muted">• {time_ago(post.created_at)}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )
    st.title(post.title)
    if post.flair:
        st.markdown(theme.pulse_badge(post.flair, "flair"), unsafe_allow_html=True)
    if post.author_notes:
        st.markdown(post.author_notes)

    render_build_card(post.build)

    st.markdown("#### Hardware Blueprint")
    _render_hardware_blueprint(post.build.components)

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
    mode-filter options; `domain`/`tier` are resolved against
    `_workload_domains`/`_workload_tiers` via `_match_option`, falling back
    to "All" (itself an always-real option) rather than ever writing a
    string absent from a keyed selectbox's own `options` list — the exact
    class of `StreamlitAPIException` footgun `ui/CLAUDE.md`'s existing
    `number_input` pre-clamp precedent warns about for a different widget
    type. `community_price_filter` is the one exception to "always a
    string": it's a raw `float | None` (`None` == "All Prices"), matching
    `render()`'s own `st.selectbox(..., format_func=...)` widget below —
    keeping the STORED/COMPARED value a real USD number and letting
    `format_func` be the only currency-DISPLAY-aware layer (ui/format.py)
    avoids the fragile "parse the number back out of a currency-symbol
    string" approach a naive multi-currency label would otherwise need.
    Confirmed live via `streamlit.testing.v1.AppTest` (see
    `tests/test_ui_smoke.py`) that a mismatched/unmatched request degrades
    gracefully instead of raising."""
    pending = st.session_state.pop("pending_community_filters", None)
    if not pending:
        return

    build_type = pending.get("build_type")
    if build_type not in ("All", "Budget", "Workload", "Free"):
        build_type = "All"
    st.session_state["community_mode_filter"] = build_type

    if build_type == "Budget":
        price_steps = _budget_price_steps(posts)
        price_choice: float | None = None  # None == "All Prices" — see render()'s format_func
        max_price = pending.get("max_price")
        if price_steps and max_price is not None:
            covering = [step for step in price_steps if step >= max_price]
            price_choice = min(covering) if covering else price_steps[-1]
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

    currency = st.session_state.get("selected_currency", "USD")
    if mode_filter == "Budget":
        price_steps = _budget_price_steps(posts)
        if price_steps:
            price_choice = st.selectbox(
                "Max Price Limit",
                [None] + price_steps,  # None == "All Prices" — a real USD number otherwise, never a
                # currency-symbol string to parse back (see _apply_pending_community_filters's docstring)
                format_func=lambda p: "All Prices" if p is None else format_currency(p, currency),
                key="community_price_filter",
            )
            if price_choice is not None:
                price_ceiling = float(price_choice)
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
            st.markdown(
                f'<div style="display:flex; align-items:center; gap:6px; margin-bottom:2px;">'
                f"{theme.avatar_html(post.user.full_name, size_px=20)}"
                f'<span class="uncapped-muted">u/{html.escape(post.user.full_name)} '
                f"• {time_ago(post.created_at)}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                sanitize_markdown(f"### {post.title} | {format_currency(post.build.total_cost, currency)}")
            )
            if post.flair:
                st.markdown(theme.pulse_badge(post.flair, "flair"), unsafe_allow_html=True)
            st.caption(_post_subtitle(post.build, post.created_at))
            if st.button("View", key=f"view_post_{post.id}"):
                st.session_state["selected_post_id"] = post.id
                st.rerun()
