"""Data access for the `users` table. See db/CLAUDE.md for the interface contract."""
from __future__ import annotations

from sqlalchemy import select

from db.database import session_scope
from db.models import User


def get_by_username(username: str) -> User | None:
    with session_scope() as session:
        row = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def get_by_email(email: str) -> User | None:
    with session_scope() as session:
        row = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def get_by_id(user_id: int) -> User | None:
    with session_scope() as session:
        row = session.get(User, user_id)
        if row is not None:
            session.expunge(row)
        return row


def create_user(username: str, password_hash: str, email: str, full_name: str) -> User:
    with session_scope() as session:
        user = User(username=username, password_hash=password_hash, email=email, full_name=full_name)
        session.add(user)
        session.flush()  # raises sqlalchemy.exc.IntegrityError on duplicate username/email
        session.refresh(user)
        session.expunge(user)
        return user
