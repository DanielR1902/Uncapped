"""Data access for `llm_cache` — memoization table for the llm/ layer (spec.md §3.8, §6.5)."""
from __future__ import annotations

from db.database import session_scope
from db.models import LLMCache


def get_cached(cache_key: str) -> LLMCache | None:
    with session_scope() as session:
        row = session.get(LLMCache, cache_key)
        if row is not None:
            session.expunge(row)
        return row


def store_cached(cache_key: str, synergy_score: float, bottleneck_percentage: float, response_json: str) -> LLMCache:
    with session_scope() as session:
        row = session.get(LLMCache, cache_key)
        if row is None:
            row = LLMCache(
                cache_key=cache_key,
                synergy_score=synergy_score,
                bottleneck_percentage=bottleneck_percentage,
                response_json=response_json,
            )
            session.add(row)
        else:
            row.synergy_score = synergy_score
            row.bottleneck_percentage = bottleneck_percentage
            row.response_json = response_json
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row
