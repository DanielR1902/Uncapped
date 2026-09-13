"""OpenRouter wrapper. analyze_build() is the only entry point ui/ should call
— it never raises for an expected failure mode; it returns a heuristic
BuildAnalysisResponse (source="heuristic") instead. See llm/CLAUDE.md.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from pydantic import ValidationError as PydanticValidationError

from db.repositories import llm_cache_repo
from engine import scoring
from engine.compatibility import BuildState
from llm.cache import cache_key
from llm.prompts import build_messages, build_request
from llm.schemas import (
    BottleneckAnalysis,
    BuildAnalysisRequest,
    BuildAnalysisResponse,
    ResolutionImpact,
    SynergyEvaluation,
    ArchitecturalInsights,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT_SECONDS = 15.0
BOTTLENECK_CLAMP_RANGE = 10.0


class LLMUnavailableError(Exception):
    """Internal signal for any failure mode that should fall back to the
    heuristic response: missing API key/model config, timeout, connection
    error, non-2xx/429 HTTP response, or a response that fails schema
    validation. analyze_build() always catches this — it never escapes to
    callers of analyze_build() itself."""


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise LLMUnavailableError("OPENROUTER_API_KEY is not set")
    return key


def _model() -> str:
    model = os.getenv("OPENROUTER_MODEL")
    if not model:
        raise LLMUnavailableError("OPENROUTER_MODEL is not set")
    return model


def _call_openrouter(request: BuildAnalysisRequest) -> dict[str, Any]:
    try:
        response = httpx.post(
            OPENROUTER_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
            },
            json={
                "model": _model(),
                "messages": build_messages(request),
                "response_format": {"type": "json_object"},
            },
        )
    except httpx.TimeoutException as exc:
        raise LLMUnavailableError("OpenRouter request timed out") from exc
    except httpx.HTTPError as exc:
        raise LLMUnavailableError(f"OpenRouter request failed: {exc}") from exc

    if response.status_code == 429:
        raise LLMUnavailableError("OpenRouter rate-limited the request")
    if response.status_code >= 400:
        raise LLMUnavailableError(f"OpenRouter returned HTTP {response.status_code}")

    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMUnavailableError(f"Malformed OpenRouter response: {exc}") from exc


def _clamp_bottleneck(response: BuildAnalysisResponse, request: BuildAnalysisRequest) -> BuildAnalysisResponse:
    """Enforce spec.md §6.3's bound: the LLM's bottleneck_percentage must stay
    within +/-10 of the deterministic baseline. Clamped in place against the
    request that produced it (schemas.py has no access to the baseline)."""
    baseline = request.deterministic_precheck.baseline_bottleneck_percentage
    low, high = max(0.0, baseline - BOTTLENECK_CLAMP_RANGE), min(100.0, baseline + BOTTLENECK_CLAMP_RANGE)
    clamped_pct = min(max(response.bottleneck.bottleneck_percentage, low), high)
    response.bottleneck = response.bottleneck.model_copy(update={"bottleneck_percentage": clamped_pct})
    return response


def _heuristic_fallback(build_state: BuildState, request: BuildAnalysisRequest) -> BuildAnalysisResponse:
    """Computed entirely from engine/scoring.py + the request's deterministic
    pre-check — no network. Used whenever the live call fails for any reason,
    so the app never crashes and always has something concrete to show."""
    precheck = request.deterministic_precheck
    bottleneck_pct, direction = scoring.bottleneck_percentage_baseline(build_state)
    limiting_component = "None" if direction == "Balanced" else direction.split("-")[0]

    resolution_impact = ResolutionImpact(
        **{
            "1080p": "Bottleneck impact is minor at 1080p."
            if bottleneck_pct < 15
            else "CPU/GPU imbalance is more visible at 1080p.",
            "1440p": "Well balanced at 1440p." if bottleneck_pct < 25 else "Some headroom lost at 1440p.",
            "4K": "GPU-bound workloads dominate at 4K regardless of CPU choice."
            if limiting_component != "GPU"
            else "GPU is the limiting factor at 4K.",
        }
    )

    synergy_score = scoring.heuristic_synergy_score(precheck.compatibility_score, bottleneck_pct)

    return BuildAnalysisResponse(
        synergy=SynergyEvaluation(
            overall_score=synergy_score,
            breakdown={
                "compatibility": precheck.compatibility_score,
                "balance": max(0.0, 100.0 - bottleneck_pct),
            },
            positive_synergies=[],
            negative_conflicts=list(precheck.failed_rules),
        ),
        bottleneck=BottleneckAnalysis(
            bottleneck_percentage=bottleneck_pct,
            limiting_component=limiting_component,
            resolution_impact=resolution_impact,
        ),
        insights=ArchitecturalInsights(
            summary="AI insight unavailable right now — showing a deterministic estimate instead.",
            upgrade_path=[],
            quirks=[],
        ),
        source="heuristic",
    )


def analyze_build(
    build_state: BuildState,
    workload_profile: str | None = None,
    budget_ceiling: float | None = None,
) -> BuildAnalysisResponse:
    request = build_request(build_state, workload_profile=workload_profile, budget_ceiling=budget_ceiling)
    key = cache_key(build_state, workload_profile)

    cached = llm_cache_repo.get_cached(key)
    if cached is not None:
        return BuildAnalysisResponse.model_validate_json(cached.response_json)

    try:
        raw = _call_openrouter(request)
        response = BuildAnalysisResponse.model_validate(raw)
    except (LLMUnavailableError, PydanticValidationError):
        return _heuristic_fallback(build_state, request)

    response.source = "llm"
    response = _clamp_bottleneck(response, request)

    llm_cache_repo.store_cached(
        cache_key=key,
        synergy_score=response.synergy.overall_score,
        bottleneck_percentage=response.bottleneck.bottleneck_percentage,
        response_json=response.model_dump_json(),
    )
    return response
