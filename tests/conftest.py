"""Shared pytest fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_malformed_retry_backoff(monkeypatch):
    """`llm/concierge.py` and `llm/client.py` retry a malformed/truncated
    OpenRouter body with a 1s/2s exponential backoff. Zero it out for every
    test so the suite stays fast (the retry COUNT is preserved)."""
    from llm import client as llm_client
    from llm import concierge as llm_concierge

    monkeypatch.setattr(llm_client, "MALFORMED_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(llm_concierge, "MALFORMED_RETRY_DELAYS", (0.0, 0.0))
