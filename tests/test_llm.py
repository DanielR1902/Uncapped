"""Phase 3 verification: LLM cache hit/miss, pydantic parsing, and heuristic fallback.

Mocks the HTTP layer via pytest's built-in `monkeypatch` (no live OpenRouter
calls, per llm/CLAUDE.md) — no extra mocking dependency needed beyond what's
already in requirements.txt.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from db import database
from db.models import Component
from engine import scoring
from engine.compatibility import BuildState
from llm import cache as cache_module
from llm import client as llm_client
from llm.schemas import BuildAnalysisResponse


@pytest.fixture()
def temp_db(tmp_path):
    db_path = tmp_path / "test_llm.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    yield
    database.get_engine().dispose()


def make_build_state() -> BuildState:
    cpu = Component(
        id=1, category="CPU", name="Test CPU", brand="TestBrand", price_usd=200.0,
        socket="AM5", tdp_watts=105, benchmark_score=80, specs_json="{}",
    )
    gpu = Component(
        id=2, category="GPU", name="Test GPU", brand="TestBrand", price_usd=600.0,
        tdp_watts=220, benchmark_score=74, specs_json=json.dumps({"length_mm": 267}),
    )
    return {"CPU": cpu, "GPU": gpu}


VALID_RESPONSE_PAYLOAD = {
    "synergy": {
        "overall_score": 88.0,
        "breakdown": {"compatibility": 100.0, "balance": 92.0},
        "positive_synergies": ["Balanced CPU/GPU pairing."],
        "negative_conflicts": [],
    },
    "bottleneck": {
        "bottleneck_percentage": 12.0,
        "limiting_component": "GPU",
        "resolution_impact": {"1080p": "Negligible impact.", "1440p": "Balanced.", "4K": "GPU-bound."},
    },
    "insights": {
        "summary": "Solid balanced build for 1440p gaming.",
        "upgrade_path": ["Consider a higher-tier GPU for 4K."],
        "quirks": [],
    },
}


def _fake_openrouter_response(payload: dict, status_code: int = 200) -> httpx.Response:
    body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", llm_client.OPENROUTER_URL))


def _set_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")


# ---------------------------------------------------------------------------
# pydantic parsing — valid and malformed payloads
# ---------------------------------------------------------------------------
def test_build_analysis_response_parses_valid_payload():
    response = BuildAnalysisResponse.model_validate(VALID_RESPONSE_PAYLOAD)
    assert response.synergy.overall_score == 88.0
    assert response.bottleneck.limiting_component == "GPU"
    assert response.bottleneck.resolution_impact.p1080 == "Negligible impact."
    assert response.source == "llm"


def test_build_analysis_response_rejects_out_of_range_score():
    bad_payload = json.loads(json.dumps(VALID_RESPONSE_PAYLOAD))
    bad_payload["synergy"]["overall_score"] = 150.0
    with pytest.raises(PydanticValidationError):
        BuildAnalysisResponse.model_validate(bad_payload)


def test_build_analysis_response_rejects_invalid_limiting_component():
    bad_payload = json.loads(json.dumps(VALID_RESPONSE_PAYLOAD))
    bad_payload["bottleneck"]["limiting_component"] = "APU"
    with pytest.raises(PydanticValidationError):
        BuildAnalysisResponse.model_validate(bad_payload)


def test_build_analysis_response_rejects_missing_section():
    bad_payload = json.loads(json.dumps(VALID_RESPONSE_PAYLOAD))
    del bad_payload["insights"]
    with pytest.raises(PydanticValidationError):
        BuildAnalysisResponse.model_validate(bad_payload)


# ---------------------------------------------------------------------------
# cache key
# ---------------------------------------------------------------------------
def test_cache_key_order_independent():
    build_state = make_build_state()
    reordered = {"GPU": build_state["GPU"], "CPU": build_state["CPU"]}
    assert cache_module.cache_key(build_state, None) == cache_module.cache_key(reordered, None)


def test_cache_key_differs_by_workload_profile():
    build_state = make_build_state()
    assert cache_module.cache_key(build_state, "Gaming") != cache_module.cache_key(build_state, "Programming")


# ---------------------------------------------------------------------------
# analyze_build — success + cache hit
# ---------------------------------------------------------------------------
def test_analyze_build_success_and_cache_hit(temp_db, monkeypatch):
    _set_env(monkeypatch)
    calls = {"count": 0}

    def fake_post(url, timeout=None, headers=None, json=None):
        calls["count"] += 1
        return _fake_openrouter_response(VALID_RESPONSE_PAYLOAD)

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)

    build_state = make_build_state()
    first = llm_client.analyze_build(build_state)
    assert first.source == "llm"
    assert first.synergy.overall_score == 88.0
    assert calls["count"] == 1

    second = llm_client.analyze_build(build_state)  # should hit cache, not the network
    assert second.source == "llm"
    assert second.synergy.overall_score == 88.0
    assert calls["count"] == 1


def test_analyze_build_bottleneck_clamped_to_baseline(temp_db, monkeypatch):
    _set_env(monkeypatch)
    wild_payload = json.loads(json.dumps(VALID_RESPONSE_PAYLOAD))
    wild_payload["bottleneck"]["bottleneck_percentage"] = 99.0  # far outside baseline +/-10

    monkeypatch.setattr(llm_client.httpx, "post", lambda *a, **k: _fake_openrouter_response(wild_payload))

    build_state = make_build_state()
    response = llm_client.analyze_build(build_state)

    baseline_pct, _ = scoring.bottleneck_percentage_baseline(build_state)
    assert abs(response.bottleneck.bottleneck_percentage - baseline_pct) <= 10.0 + 1e-6
    assert response.source == "llm"  # still a real LLM response, just clamped


# ---------------------------------------------------------------------------
# analyze_build — heuristic fallback, never raises
# ---------------------------------------------------------------------------
def test_analyze_build_falls_back_when_env_not_configured(temp_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"
    assert 0.0 <= response.bottleneck.bottleneck_percentage <= 100.0
    assert 0.0 <= response.synergy.overall_score <= 100.0


def test_analyze_build_falls_back_on_timeout(temp_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"


def test_analyze_build_falls_back_on_connection_error(temp_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"


def test_analyze_build_falls_back_on_rate_limit(temp_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            429, json={"error": "rate limited"}, request=httpx.Request("POST", llm_client.OPENROUTER_URL)
        )

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"


def test_analyze_build_falls_back_on_server_error(temp_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            500, json={"error": "server error"}, request=httpx.Request("POST", llm_client.OPENROUTER_URL)
        )

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"


def test_analyze_build_falls_back_on_malformed_response_shape(temp_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            200, json={"unexpected": "shape"}, request=httpx.Request("POST", llm_client.OPENROUTER_URL)
        )

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"


def test_analyze_build_falls_back_on_schema_validation_failure(temp_db, monkeypatch):
    _set_env(monkeypatch)
    invalid_payload = json.loads(json.dumps(VALID_RESPONSE_PAYLOAD))
    invalid_payload["bottleneck"]["limiting_component"] = "TPU"  # not in Literal["CPU","GPU","None"]

    monkeypatch.setattr(llm_client.httpx, "post", lambda *a, **k: _fake_openrouter_response(invalid_payload))
    response = llm_client.analyze_build(make_build_state())
    assert response.source == "heuristic"


def test_heuristic_fallback_never_cached(temp_db, monkeypatch):
    """A fallback must not shadow a later real call — only successful LLM
    responses get written to llm_cache (spec.md §6.1)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    build_state = make_build_state()
    fallback_response = llm_client.analyze_build(build_state)
    assert fallback_response.source == "heuristic"

    key = cache_module.cache_key(build_state, None)
    from db.repositories import llm_cache_repo

    assert llm_cache_repo.get_cached(key) is None
