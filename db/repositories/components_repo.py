"""Data access for the `components` and `workload_mappings` tables."""
from __future__ import annotations

from sqlalchemy import select

from db.database import session_scope
from db.models import Component, WorkloadMapping


def get_by_category(category: str) -> list[Component]:
    with session_scope() as session:
        rows = (
            session.execute(
                select(Component).where(Component.category == category).order_by(Component.price_usd)
            )
            .scalars()
            .all()
        )
        session.expunge_all()
        return list(rows)


def get_by_id(component_id: int) -> Component | None:
    with session_scope() as session:
        row = session.get(Component, component_id)
        if row is not None:
            session.expunge(row)
        return row


def get_all() -> list[Component]:
    with session_scope() as session:
        rows = session.execute(select(Component)).scalars().all()
        session.expunge_all()
        return list(rows)


def count() -> int:
    with session_scope() as session:
        return session.query(Component).count()


def get_workload_matches(workload_profile: str, tier: str | None = None) -> list[tuple[Component, WorkloadMapping]]:
    """Components tagged for a workload profile (optionally scoped to a tier),
    joined with the mapping row so callers can read weight/tier."""
    with session_scope() as session:
        stmt = (
            select(Component, WorkloadMapping)
            .join(WorkloadMapping, WorkloadMapping.component_id == Component.id)
            .where(WorkloadMapping.workload_profile == workload_profile)
        )
        if tier is not None:
            stmt = stmt.where(WorkloadMapping.tier == tier)
        rows = session.execute(stmt).all()
        result = [(component, mapping) for component, mapping in rows]
        for component, mapping in result:
            session.expunge(component)
            session.expunge(mapping)
        return result
