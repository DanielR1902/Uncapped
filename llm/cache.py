"""Cache-key computation for the llm/ memoization layer (spec.md §6.5)."""
from __future__ import annotations

import hashlib

from engine.compatibility import BuildState

DEFAULT_PROMPT_TYPE = "build_analysis"


def cache_key(
    build_state: BuildState,
    workload_profile: str | None,
    prompt_type: str = DEFAULT_PROMPT_TYPE,
) -> str:
    """sha256 of prompt_type + workload_profile + sorted component IDs.
    `prompt_type` defaults to the single combined-call type used today; it's a
    parameter so a future distinct prompt type can share this cache table
    without key collisions (spec.md §6.5) — not because today's three sections
    are cached separately (they aren't)."""
    component_ids = sorted(component.id for component in build_state.values())
    raw = f"{prompt_type}|{workload_profile or ''}|{','.join(str(i) for i in component_ids)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
