"""Your Posts — a management dashboard for the current user's own real,
currently-shared community posts (spec.md §7.6.2). Distinct from `my_builds.py`
("Previous Builds" — the user's saved `Build` rows, published or not) and from
`community.py` (the full public feed, every author's posts): this page is
scoped to `community_repo.get_posts_for_user(user_id)`.

Deleting a post here (`community_repo.delete_post`) removes ONLY the
`community_posts` row (and, via the DB's own `ON DELETE CASCADE`, its
comments) — it NEVER touches the underlying `Build` row, which stays exactly
as saved in Previous Builds. This mirrors `builds_repo.delete_build`'s own
"delete cascades downward, never upward" shape, just the other direction:
deleting the post never cascades UP to the build it wraps.
"""
from __future__ import annotations

import streamlit as st

from auth.session import current_user
from db.repositories import community_repo
from ui.format import format_currency, humanize_profile, sanitize_markdown, time_ago
from ui import theme

_DELETE_CONFIRM_KEY_PREFIX = "confirm_delete_post_"


def _view_in_community(post_id: int) -> None:
    # Sets `page` directly (not `ui.state.navigate_to_page`, which always
    # clears `selected_post_id` as part of navigating TO "community" — the
    # exact same reason `ui/components/chat_assistant.py`'s own
    # `open_community_build` handler bypasses it too) so both land on the
    # SAME transition instead of the id being wiped a moment after it's set.
    st.session_state["page"] = "community"
    st.session_state["selected_post_id"] = post_id
    st.rerun()


def _delete_post(post_id: int) -> None:
    community_repo.delete_post(post_id)
    st.success("Post removed from Community.")
    st.rerun()


def render() -> None:
    st.title("Your Posts")
    st.caption("Community posts you've published — deleting one here never touches the saved build itself.")

    user = current_user()
    posts = community_repo.get_posts_for_user(user["id"])

    if not posts:
        st.info("You haven't published any builds to Community yet — share one from Previous Builds or the Build Studio.")
        return

    currency = st.session_state.get("selected_currency", "USD")

    for post in posts:
        with st.container(border=True):
            st.markdown(sanitize_markdown(f"#### {post.title}"))
            if post.flair:
                st.markdown(theme.flair_badge(post.flair), unsafe_allow_html=True)
            caption = f"{format_currency(post.build.total_cost, currency)} · {post.build.creation_mode} build"
            if post.build.workload_profile:
                caption += f" · {humanize_profile(post.build.workload_profile)}"
            caption += f" · Posted {time_ago(post.created_at)}"
            st.caption(caption)

            confirm_key = f"{_DELETE_CONFIRM_KEY_PREFIX}{post.id}"
            if st.session_state.get(confirm_key):
                st.badge(
                    f'Delete "{post.title}"? The saved build itself is never affected.',
                    icon=":material/warning:", color="red",
                )
                yes_col, cancel_col = st.columns(2)
                if yes_col.button(
                    "Yes, delete", key=f"{confirm_key}_yes", use_container_width=True, type="primary",
                ):
                    st.session_state[confirm_key] = False
                    _delete_post(post.id)
                if cancel_col.button("Cancel", key=f"{confirm_key}_cancel", use_container_width=True):
                    st.session_state[confirm_key] = False
                    st.rerun()
            else:
                view_col, delete_col = st.columns(2)
                if view_col.button("View in Community", key=f"view_in_community_{post.id}", use_container_width=True):
                    _view_in_community(post.id)
                if delete_col.button("Delete Post", key=f"delete_post_{post.id}", use_container_width=True):
                    st.session_state[confirm_key] = True
                    st.rerun()
