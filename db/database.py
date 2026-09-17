"""Connection/session management — the only place that owns the SQLAlchemy engine.

Every repository goes through get_session()/session_scope() from here. No business
logic lives in this module (see db/CLAUDE.md).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

load_dotenv()

DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent / "uncapped.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_SQLITE_PATH}"

_engine: Engine | None = None
SessionLocal: sessionmaker | None = None


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    # SQLite ignores FK constraints unless explicitly enabled per connection.
    if _engine is not None and _engine.dialect.name == "sqlite":
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def configure(database_url: str | None = None) -> Engine:
    """(Re)bind the module-level engine/session factory. Tests call this with an
    isolated URL (e.g. a temp-file SQLite DB) before running against a clean schema."""
    global _engine, SessionLocal

    url = database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    _engine = create_engine(url, future=True)
    event.listen(_engine, "connect", _enable_sqlite_foreign_keys)
    SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def get_engine() -> Engine:
    assert _engine is not None, "database.configure() has not run yet"
    return _engine


def get_session() -> Session:
    assert SessionLocal is not None, "database.configure() has not run yet"
    return SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_builds_workload_tier_column() -> None:
    """One-time, idempotent, additive migration: db/models.py added
    Build.workload_tier after this project's `builds` table already existed
    (populated by real usage) in some environments — Base.metadata.create_all
    only creates missing TABLES, never ALTERs existing ones to add a missing
    column, so this must be done explicitly. Purely additive (ADD COLUMN,
    nullable, no data touched or removed) — never drops/recreates anything.

    Must run AFTER Base.metadata.create_all() so `builds` is guaranteed to
    exist (a genuinely fresh DB gets the column correctly from create_all
    already, so this just no-ops on it)."""
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return  # this project's only other target is Postgres in cloud deploy; a
                 # real migration tool would be needed there, out of scope here
    with engine.connect() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='builds'"
        ).fetchone()
        if table_exists is None:
            return
        existing_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(builds)")}
        if "workload_tier" not in existing_columns:
            conn.exec_driver_sql("ALTER TABLE builds ADD COLUMN workload_tier TEXT")
            conn.commit()


def init_db() -> None:
    """Create all tables that don't already exist."""
    Base.metadata.create_all(get_engine())
    _ensure_builds_workload_tier_column()


def reset_db() -> None:
    """Drop and recreate all tables. Destructive — tests and local dev only."""
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())


# Bind a default engine at import time so `db.database.get_session()` works out of
# the box for the app; tests override this via configure(<temp url>).
configure()
