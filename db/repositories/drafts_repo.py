"""Data access for the `draft_builds` table — save/load/list/delete a user's
in-progress build snapshots (see db/CLAUDE.md's one-file-per-aggregate
convention). JSON serialization of the two storage columns happens here,
matching this package's own precedent (e.g. Component.specs_json is stored
as a JSON string and parsed by its caller)."""
from __future__ import annotations

import json

from sqlalchemy import select

from db.database import session_scope
from db.models import DraftBuild


def save_draft(
    user_id: int,
    name: str,
    mode: str,
    components: dict[str, int],
    quantities: dict[str, int],
) -> DraftBuild:
    with session_scope() as session:
        draft = DraftBuild(
            user_id=user_id,
            name=name,
            mode=mode,
            components_json=json.dumps(components),
            quantities_json=json.dumps(quantities),
        )
        session.add(draft)
        session.flush()  # populate draft.id
        session.refresh(draft)
        session.expunge(draft)
        return draft


def get_user_drafts(user_id: int) -> list[DraftBuild]:
    with session_scope() as session:
        rows = session.execute(
            select(DraftBuild)
            .where(DraftBuild.user_id == user_id)
            .order_by(DraftBuild.updated_at.desc())
        ).scalars().all()
        session.expunge_all()
        return list(rows)


def get_draft(draft_id: int) -> DraftBuild | None:
    with session_scope() as session:
        row = session.get(DraftBuild, draft_id)
        if row is not None:
            session.expunge(row)
        return row


def delete_draft(draft_id: int) -> None:
    """No-op if the draft is already gone, matching builds_repo.delete_build's
    own precedent."""
    with session_scope() as session:
        draft = session.get(DraftBuild, draft_id)
        if draft is None:
            return
        session.delete(draft)
