"""SQLAlchemy ORM models — canonical schema per spec.md §3.

No business logic here (see db/CLAUDE.md). Portable across SQLite and PostgreSQL:
no SQLite-only pragmas/functions in column defaults.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Boolean,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


COMPONENT_CATEGORIES = (
    "CPU",
    "Motherboard",
    "GPU",
    "RAM",
    "Storage",
    "PSU",
    "Case",
    "Cooler",
    "NetworkCard",
    "SoundCard",
    "OpticalDrive",
)

WORKLOAD_PROFILES = ("General", "Gaming", "VideoEditing", "Design", "Programming")

WORKLOAD_TIERS = ("Entry", "Mid", "High", "Enthusiast")

CREATION_MODES = ("Budget", "Workload", "Free")


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    builds: Mapped[list["Build"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    community_posts: Mapped[list["CommunityPost"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    community_comments: Mapped[list["CommunityComment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Component(Base):
    __tablename__ = "components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)

    # Compatibility-critical fields (nullable — only populated for the categories they apply to).
    socket: Mapped[str | None] = mapped_column(Text)
    ram_type: Mapped[str | None] = mapped_column(Text)
    tdp_watts: Mapped[int | None] = mapped_column(Integer)
    wattage_capacity: Mapped[int | None] = mapped_column(Integer)
    form_factor: Mapped[str | None] = mapped_column(Text)
    capacity_gb: Mapped[int | None] = mapped_column(Integer)
    interface: Mapped[str | None] = mapped_column(Text)
    max_gpu_length_mm: Mapped[int | None] = mapped_column(Integer)
    max_cooler_height_mm: Mapped[int | None] = mapped_column(Integer)
    psu_form_factor_support: Mapped[str | None] = mapped_column(Text)
    chipset: Mapped[str | None] = mapped_column(Text)
    benchmark_score: Mapped[int | None] = mapped_column(Integer)

    specs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    image_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint(_in_list_sql("category", COMPONENT_CATEGORIES), name="ck_components_category"),
        CheckConstraint("price_usd > 0", name="ck_components_price_positive"),
        Index("idx_components_category", "category"),
        Index("idx_components_category_price", "category", "price_usd"),
        Index("idx_components_socket", "socket"),
    )


class WorkloadMapping(Base):
    __tablename__ = "workload_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component_id: Mapped[int] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE"), nullable=False
    )
    workload_profile: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    component: Mapped["Component"] = relationship()

    __table_args__ = (
        UniqueConstraint("component_id", "workload_profile", name="uq_workload_component_profile"),
        CheckConstraint(_in_list_sql("workload_profile", WORKLOAD_PROFILES), name="ck_workload_profile"),
        CheckConstraint(_in_list_sql("tier", WORKLOAD_TIERS), name="ck_workload_tier"),
        Index("idx_workload_profile_tier", "workload_profile", "tier"),
    )


class Build(Base):
    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    creation_mode: Mapped[str] = mapped_column(Text, nullable=False)
    workload_profile: Mapped[str | None] = mapped_column(Text)
    workload_tier: Mapped[str | None] = mapped_column(Text)
    budget_ceiling: Mapped[float | None] = mapped_column(Float)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False)
    synergy_score: Mapped[float | None] = mapped_column(Float)
    bottleneck_percentage: Mapped[float | None] = mapped_column(Float)
    compatibility_score: Mapped[float] = mapped_column(Float, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Informational only, per spec.md §3.4/§7.6 — deliberately NOT a DB-level ForeignKey.
    # `builds` <-> `community_posts` would otherwise form a circular FK dependency that
    # SQLite cannot express via ALTER TABLE ADD CONSTRAINT; referential integrity for
    # this link is enforced in db/repositories, not the schema.
    forked_from_post_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    user: Mapped["User"] = relationship(back_populates="builds")
    components: Mapped[list["BuildComponent"]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )
    community_posts: Mapped[list["CommunityPost"]] = relationship(
        back_populates="build", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(_in_list_sql("creation_mode", CREATION_MODES), name="ck_builds_creation_mode"),
        CheckConstraint(
            "workload_profile IS NULL OR " + _in_list_sql("workload_profile", WORKLOAD_PROFILES),
            name="ck_builds_workload_profile",
        ),
        CheckConstraint(
            "workload_tier IS NULL OR " + _in_list_sql("workload_tier", WORKLOAD_TIERS),
            name="ck_builds_workload_tier",
        ),
        Index("idx_builds_user_created", "user_id", "created_at"),
        Index("idx_builds_public_created", "is_public", "created_at"),
    )


class BuildComponent(Base):
    __tablename__ = "build_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id"), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    build: Mapped["Build"] = relationship(back_populates="components")
    component: Mapped["Component"] = relationship()

    __table_args__ = (
        UniqueConstraint("build_id", "component_id", name="uq_build_component"),
        Index("idx_build_components_build", "build_id"),
    )


class CommunityPost(Base):
    __tablename__ = "community_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    build: Mapped["Build"] = relationship(back_populates="community_posts")
    user: Mapped["User"] = relationship(back_populates="community_posts")
    comments: Mapped[list["CommunityComment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_community_posts_build", "build_id"),
        Index("idx_community_posts_created", "created_at"),
    )


class CommunityComment(Base):
    __tablename__ = "community_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("community_posts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    post: Mapped["CommunityPost"] = relationship(back_populates="comments")
    user: Mapped["User"] = relationship(back_populates="community_comments")

    __table_args__ = (Index("idx_comments_post_created", "post_id", "created_at"),)


class LLMCache(Base):
    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    synergy_score: Mapped[float] = mapped_column(Float, nullable=False)
    bottleneck_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    # Full serialized BuildAnalysisResponse (synergy + bottleneck + insights), not
    # just the insights section — synergy_score/bottleneck_percentage above are a
    # denormalized subset kept for cheap SQL-level queries without deserializing.
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
