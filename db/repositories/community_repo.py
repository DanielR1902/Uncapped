"""Data access for `community_posts` / `community_comments`."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.database import session_scope
from db.models import Build, BuildComponent, CommunityComment, CommunityPost

_POST_EAGER = (
    selectinload(CommunityPost.build).selectinload(Build.components).selectinload(BuildComponent.component),
    selectinload(CommunityPost.user),
)


def create_post(
    build_id: int, user_id: int, title: str, author_notes: str | None = None, flair: str | None = None
) -> CommunityPost:
    with session_scope() as session:
        post = CommunityPost(
            build_id=build_id, user_id=user_id, title=title, author_notes=author_notes, flair=flair
        )
        session.add(post)
        session.flush()
        post = session.execute(
            select(CommunityPost).where(CommunityPost.id == post.id).options(*_POST_EAGER)
        ).scalar_one()
        session.expunge_all()
        return post


def get_post(post_id: int) -> CommunityPost | None:
    with session_scope() as session:
        row = session.execute(
            select(CommunityPost).where(CommunityPost.id == post_id).options(*_POST_EAGER)
        ).scalar_one_or_none()
        if row is not None:
            session.expunge_all()
        return row


def get_feed() -> list[CommunityPost]:
    """Strictly chronological, newest first — no vote/score ranking (the
    upvote/downvote mechanism, `community_votes`, and `.net_score` were
    removed entirely per a later product decision; see git history for the
    prior Reddit-style implementation this replaced)."""
    with session_scope() as session:
        posts = list(
            session.execute(
                select(CommunityPost).options(*_POST_EAGER).order_by(CommunityPost.created_at.desc())
            )
            .scalars()
            .all()
        )
        session.expunge_all()
        return posts


def add_comment(post_id: int, user_id: int, content: str, parent_comment_id: int | None = None) -> CommunityComment:
    """`parent_comment_id` (optional, defaults to `None` — a top-level comment)
    is the id of the comment this one directly replies to (spec.md §3.7/§7.6,
    threaded discussions). Never validated against `post_id` here (trusted
    caller data — `ui/views/community.py` only ever offers a Reply control on
    a comment it already fetched for THIS post) the same "caller already has
    real, trusted ids" precedent as every other repository function here."""
    with session_scope() as session:
        comment = CommunityComment(
            post_id=post_id, user_id=user_id, content=content, parent_comment_id=parent_comment_id
        )
        session.add(comment)
        session.flush()
        session.refresh(comment)
        session.expunge(comment)
        return comment


def get_comments(post_id: int) -> list[CommunityComment]:
    """A FLAT list ordered by `created_at` ascending (oldest first) — unchanged
    shape regardless of `parent_comment_id`, so existing callers/tests keep
    working. Building the reply tree (grouping by `parent_comment_id`,
    indentation depth) from this flat, already-chronological list is a
    presentation concern handled entirely in `ui/views/community.py`, not
    here (db/ owns no business/presentation logic, db/CLAUDE.md)."""
    with session_scope() as session:
        rows = (
            session.execute(
                select(CommunityComment)
                .where(CommunityComment.post_id == post_id)
                .order_by(CommunityComment.created_at.asc())
            )
            .scalars()
            .all()
        )
        session.expunge_all()
        return list(rows)
