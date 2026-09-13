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


def create_post(build_id: int, user_id: int, title: str, author_notes: str | None = None) -> CommunityPost:
    with session_scope() as session:
        post = CommunityPost(build_id=build_id, user_id=user_id, title=title, author_notes=author_notes)
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
    with session_scope() as session:
        rows = (
            session.execute(
                select(CommunityPost).options(*_POST_EAGER).order_by(CommunityPost.created_at.desc())
            )
            .scalars()
            .all()
        )
        session.expunge_all()
        return list(rows)


def add_comment(post_id: int, user_id: int, content: str) -> CommunityComment:
    with session_scope() as session:
        comment = CommunityComment(post_id=post_id, user_id=user_id, content=content)
        session.add(comment)
        session.flush()
        session.refresh(comment)
        session.expunge(comment)
        return comment


def get_comments(post_id: int) -> list[CommunityComment]:
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
