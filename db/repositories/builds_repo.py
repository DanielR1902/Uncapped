"""Data access for the `builds` / `build_components` tables."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.database import session_scope
from db.models import Build, BuildComponent, Component


@dataclass
class BuildComponentInput:
    component_id: int
    quantity: int = 1


@dataclass
class BuildsFilter:
    workload_profile: str | None = None
    sort: str = "date"  # "date" | "cost_desc" | "cost_asc"


_EAGER = selectinload(Build.components).selectinload(BuildComponent.component)


def create_build(
    user_id: int,
    name: str,
    creation_mode: str,
    components: list[BuildComponentInput],
    total_cost: float,
    compatibility_score: float,
    workload_profile: str | None = None,
    workload_tier: str | None = None,
    budget_ceiling: float | None = None,
    synergy_score: float | None = None,
    bottleneck_percentage: float | None = None,
    is_public: bool = False,
) -> Build:
    with session_scope() as session:
        build = Build(
            user_id=user_id,
            name=name,
            creation_mode=creation_mode,
            workload_profile=workload_profile,
            workload_tier=workload_tier,
            budget_ceiling=budget_ceiling,
            total_cost=total_cost,
            synergy_score=synergy_score,
            bottleneck_percentage=bottleneck_percentage,
            compatibility_score=compatibility_score,
            is_public=is_public,
        )
        session.add(build)
        session.flush()  # populate build.id

        for item in components:
            component = session.get(Component, item.component_id)
            if component is None:
                raise ValueError(f"No component with id={item.component_id}")
            session.add(
                BuildComponent(
                    build_id=build.id,
                    component_id=item.component_id,
                    category=component.category,
                    quantity=item.quantity,
                )
            )

        session.flush()
        session.refresh(build)
        # re-fetch with components + nested component eagerly loaded before detaching
        build = session.execute(
            select(Build).where(Build.id == build.id).options(_EAGER)
        ).scalar_one()
        session.expunge_all()
        return build


def get_build(build_id: int) -> Build | None:
    with session_scope() as session:
        row = session.execute(
            select(Build).where(Build.id == build_id).options(_EAGER)
        ).scalar_one_or_none()
        if row is not None:
            session.expunge_all()
        return row


def get_builds_for_user(user_id: int, build_filter: BuildsFilter | None = None) -> list[Build]:
    build_filter = build_filter or BuildsFilter()
    with session_scope() as session:
        stmt = select(Build).where(Build.user_id == user_id).options(_EAGER)
        if build_filter.workload_profile is not None:
            stmt = stmt.where(Build.workload_profile == build_filter.workload_profile)

        if build_filter.sort == "cost_desc":
            stmt = stmt.order_by(Build.total_cost.desc())
        elif build_filter.sort == "cost_asc":
            stmt = stmt.order_by(Build.total_cost.asc())
        else:
            stmt = stmt.order_by(Build.created_at.desc())

        rows = session.execute(stmt).scalars().all()
        session.expunge_all()
        return list(rows)


def get_public_builds() -> list[Build]:
    with session_scope() as session:
        rows = (
            session.execute(
                select(Build)
                .where(Build.is_public.is_(True))
                .options(_EAGER)
                .order_by(Build.created_at.desc())
            )
            .scalars()
            .all()
        )
        session.expunge_all()
        return list(rows)


def set_public(build_id: int, is_public: bool) -> None:
    with session_scope() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise ValueError(f"No build with id={build_id}")
        build.is_public = is_public


def set_workload_tier(build_id: int, workload_tier: str) -> None:
    with session_scope() as session:
        build = session.get(Build, build_id)
        if build is None:
            raise ValueError(f"No build with id={build_id}")
        build.workload_tier = workload_tier


def delete_build(build_id: int) -> None:
    """Deletes the build and, via ORM cascade + the DB-level ON DELETE CASCADE
    on build_id/build_id FKs, its build_components and any community_posts it
    was shared as (which in turn cascades their community_comments). No-op if
    the build is already gone."""
    with session_scope() as session:
        build = session.get(Build, build_id)
        if build is None:
            return
        session.delete(build)
