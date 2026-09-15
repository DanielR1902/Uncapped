"""Community forum (spec.md §7.6)."""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.repositories import builds_repo, community_repo, users_repo
from ui import state
from ui.components.build_card import render_build_card


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
        st.write(f"- **{bc.category}**: {bc.component.name} (${bc.component.price_usd:,.2f})")

    cols = st.columns(2)
    if cols[0].button("🍴 Fork / Customize", key="fork_build", use_container_width=True):
        _fork_into_studio(post.build)
    if cols[1].button("💾 Save to My Builds", key="save_to_my_builds", use_container_width=True):
        _save_to_my_builds(post.build)

    _comments_section(post)


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

    for post in posts:
        with st.container(border=True):
            st.markdown(f"### {post.title}")
            st.caption(
                f"{post.build.creation_mode} build · ${post.build.total_cost:,.2f} · {post.created_at:%Y-%m-%d}"
            )
            if st.button("View", key=f"view_post_{post.id}"):
                st.session_state["selected_post_id"] = post.id
                st.rerun()
