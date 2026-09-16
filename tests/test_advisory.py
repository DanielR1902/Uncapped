"""AI Build Advisory: mocked-HTTP success, heuristic fallback, and never-raises
coverage for llm/advisory.py.

Mocks the HTTP layer via pytest's built-in `monkeypatch` (no live OpenRouter
calls, per llm/CLAUDE.md). Unlike tests/test_llm.py's hand-built Component
fixtures, the heuristic fallback here calls engine.solvers.get_compatible_
candidates, which reads the real catalog via db.repositories.components_repo
— so this file uses tests/test_engine.py's seeded-DB approach (run_seed())
rather than test_llm.py's in-memory-only Components.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from db import database
from engine import compatibility, solvers
from llm import advisory
from llm.schemas import BuildAdvisoryResponse


@pytest.fixture()
def seeded_db(tmp_path):
    from db.seed import run_seed

    db_path = tmp_path / "test_advisory.db"
    database.configure(f"sqlite:///{db_path}")
    database.init_db()
    run_seed()
    yield
    database.get_engine().dispose()


def make_build_state():
    """A small, fully-compatible CPU+GPU (+ Motherboard) build state drawn
    from the real seeded catalog, so engine.solvers.get_compatible_candidates
    has real neighbors to find."""
    from db.repositories import components_repo

    cpus = sorted(components_repo.get_by_category("CPU"), key=lambda c: c.benchmark_score or 0)
    gpus = sorted(components_repo.get_by_category("GPU"), key=lambda c: c.benchmark_score or 0)
    # Deliberately mismatched tiers (weak CPU, strong GPU) so bottleneck
    # direction is unambiguous rather than possibly landing on "Balanced".
    cpu = cpus[0]
    gpu = gpus[-1]
    build_state = {"CPU": cpu, "GPU": gpu}

    motherboards = [
        m for m in components_repo.get_by_category("Motherboard") if m.socket == cpu.socket
    ]
    if motherboards:
        build_state["Motherboard"] = motherboards[0]

    return build_state


VALID_ADVISORY_PAYLOAD = {
    "pros": ["Good CPU/GPU synergy for the stated workload.", "No compatibility conflicts detected."],
    "cons": ["The GPU is somewhat over-provisioned relative to the CPU."],
    "within_budget": "Downgrade the GPU one tier and put the savings toward a stronger CPU.",
    "stretch_budget": "Upgrade the CPU to the next tier up for an additional 45 USD to close the gap.",
}


def _fake_openrouter_response(payload: dict, status_code: int = 200) -> httpx.Response:
    body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", advisory.OPENROUTER_URL))


def _set_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")


# ---------------------------------------------------------------------------
# pydantic parsing
# ---------------------------------------------------------------------------
def test_build_advisory_response_parses_valid_payload():
    response = BuildAdvisoryResponse.model_validate(VALID_ADVISORY_PAYLOAD)
    assert response.pros == VALID_ADVISORY_PAYLOAD["pros"]
    assert response.cons == VALID_ADVISORY_PAYLOAD["cons"]
    assert response.within_budget == VALID_ADVISORY_PAYLOAD["within_budget"]
    assert response.stretch_budget == VALID_ADVISORY_PAYLOAD["stretch_budget"]
    assert response.source == "llm"


def test_build_advisory_response_rejects_non_string_field():
    bad_payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    bad_payload["within_budget"] = ["not", "a", "string"]
    with pytest.raises(PydanticValidationError):
        BuildAdvisoryResponse.model_validate(bad_payload)


def test_build_advisory_response_rejects_non_list_field():
    bad_payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    bad_payload["pros"] = "not a list"
    with pytest.raises(PydanticValidationError):
        BuildAdvisoryResponse.model_validate(bad_payload)


@pytest.mark.parametrize("missing_key", ["pros", "cons", "within_budget", "stretch_budget"])
def test_build_advisory_response_requires_all_four_keys(missing_key):
    bad_payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    del bad_payload[missing_key]
    with pytest.raises(PydanticValidationError):
        BuildAdvisoryResponse.model_validate(bad_payload)


# ---------------------------------------------------------------------------
# get_build_advisory — success path
# ---------------------------------------------------------------------------
def test_get_build_advisory_success(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return _fake_openrouter_response(VALID_ADVISORY_PAYLOAD)

    monkeypatch.setattr(advisory.httpx, "post", fake_post)

    build_state = make_build_state()
    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)

    assert result == {
        "pros": VALID_ADVISORY_PAYLOAD["pros"],
        "cons": VALID_ADVISORY_PAYLOAD["cons"],
        "within_budget": VALID_ADVISORY_PAYLOAD["within_budget"],
        "stretch_budget": VALID_ADVISORY_PAYLOAD["stretch_budget"],
        "source": "llm",
    }


def test_get_build_advisory_success_sets_source_llm_even_if_absent(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    payload_without_source = {
        "pros": ["strength"],
        "cons": ["limitation"],
        "within_budget": "tip",
        "stretch_budget": "upgrade",
    }

    monkeypatch.setattr(
        advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload_without_source)
    )

    build_state = make_build_state()
    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=950.0)
    assert result["source"] == "llm"


# ---------------------------------------------------------------------------
# get_build_advisory — heuristic fallback, never raises
# ---------------------------------------------------------------------------
def test_get_build_advisory_falls_back_when_env_not_configured(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    build_state = make_build_state()
    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)

    assert result["source"] == "heuristic"
    assert isinstance(result["within_budget"], str) and len(result["within_budget"]) > 0
    assert isinstance(result["stretch_budget"], str) and len(result["stretch_budget"]) > 0
    assert isinstance(result["pros"], list) and len(result["pros"]) > 0
    assert isinstance(result["cons"], list) and len(result["cons"]) > 0
    # References real catalog data — the bottleneck-limiting/over-provisioned
    # component's real name should show up in at least one suggestion.
    all_text = " ".join([result["within_budget"], result["stretch_budget"]])
    assert build_state["CPU"].name in all_text or build_state["GPU"].name in all_text


def test_get_build_advisory_falls_back_on_malformed_response(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            200, json={"unexpected": "shape"}, request=httpx.Request("POST", advisory.OPENROUTER_URL)
        )

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = advisory.get_build_advisory(make_build_state(), mode="Workload", current_budget_or_cost=1200.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_non_json_content(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        body = {"choices": [{"message": {"content": "not valid json {{{"}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", advisory.OPENROUTER_URL))

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = advisory.get_build_advisory(make_build_state(), mode="Free", current_budget_or_cost=1200.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_server_error(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            500, json={"error": "server error"}, request=httpx.Request("POST", advisory.OPENROUTER_URL)
        )

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_rate_limit(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            429, json={"error": "rate limited"}, request=httpx.Request("POST", advisory.OPENROUTER_URL)
        )

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_timeout(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_connection_error(seeded_db, monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_schema_validation_failure(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    invalid_payload = {
        "pros": ["strength"],
        "cons": ["limitation"],
        "within_budget": ["not-a-string"],
        "stretch_budget": "ok",
    }

    monkeypatch.setattr(
        advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(invalid_payload)
    )
    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


def test_get_build_advisory_falls_back_on_missing_key(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    payload_missing_cons = {
        "pros": ["strength"],
        "within_budget": "tip",
        "stretch_budget": "upgrade",
    }

    monkeypatch.setattr(
        advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload_missing_cons)
    )
    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert result["pros"] and result["cons"]


# ---------------------------------------------------------------------------
# Both Budget (ceiling) and Workload/Free (current cost) modes work
# ---------------------------------------------------------------------------
def test_budget_mode_with_ceiling_works_without_error(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(VALID_ADVISORY_PAYLOAD))

    result = advisory.get_build_advisory(make_build_state(), mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "llm"


def test_workload_mode_with_current_cost_works_without_error(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    build_state = make_build_state()
    current_cost = sum(c.price_usd for c in build_state.values())

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # exercise heuristic path here too
    result = advisory.get_build_advisory(
        build_state, mode="Workload", current_budget_or_cost=current_cost, profile="Gaming"
    )
    assert result["source"] == "heuristic"
    assert result["within_budget"]
    assert result["stretch_budget"]
    assert result["pros"] and result["cons"]


def test_free_mode_with_current_cost_works_without_error(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    build_state = make_build_state()
    current_cost = sum(c.price_usd for c in build_state.values())
    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(VALID_ADVISORY_PAYLOAD))

    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=current_cost)
    assert result["source"] == "llm"


# ---------------------------------------------------------------------------
# bottleneck_info: internal computation vs. caller-supplied shapes
# ---------------------------------------------------------------------------
def test_bottleneck_info_computed_internally_when_omitted(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    build_state = make_build_state()

    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    # A mismatched-tier CPU/GPU pair should not fall back to the generic
    # "balanced" heuristic message.
    assert "well balanced" not in result["within_budget"]
    assert result["pros"] and result["cons"]


def test_bottleneck_info_accepts_analyze_build_shape(seeded_db, monkeypatch):
    """A caller may pass a prior llm.client.analyze_build() result's
    `.bottleneck` field (model_dump()'d) instead of the raw
    (percentage, direction) tuple shape."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    build_state = make_build_state()

    pct, direction = __import__("engine.scoring", fromlist=["scoring"]).bottleneck_percentage_baseline(build_state)
    limiting = "None" if direction == "Balanced" else direction.split("-")[0]
    bottleneck_info = {
        "bottleneck_percentage": pct,
        "limiting_component": limiting,
        "resolution_impact": {"1080p": "x", "1440p": "x", "4K": "x"},
    }

    result = advisory.get_build_advisory(
        build_state, mode="Budget", current_budget_or_cost=1500.0, bottleneck_info=bottleneck_info
    )
    assert result["source"] == "heuristic"
    assert result["within_budget"]
    assert result["stretch_budget"]
    assert result["pros"] and result["cons"]


# ---------------------------------------------------------------------------
# Mode-specific objectives: remaining_budget payload field + heuristic wording
# ---------------------------------------------------------------------------
def test_workload_mode_payload_includes_profile_and_zero_remaining_budget(seeded_db, monkeypatch):
    """Workload mode has no budget ceiling: remaining_budget must be 0.0, and
    the workload_profile the caller supplied must reach the LLM payload
    verbatim so the model can apply the Workload-mode objective."""
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response(VALID_ADVISORY_PAYLOAD)

    monkeypatch.setattr(advisory.httpx, "post", fake_post)

    build_state = make_build_state()
    current_cost = sum(c.price_usd for c in build_state.values())
    result = advisory.get_build_advisory(
        build_state, mode="Workload", current_budget_or_cost=current_cost, profile="Gaming"
    )

    assert result["source"] == "llm"
    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["workload_profile"] == "Gaming"
    assert sent_payload["mode"] == "Workload"
    assert sent_payload["remaining_budget"] == 0.0


def test_free_mode_payload_has_zero_remaining_budget_and_no_ceiling_language(seeded_db, monkeypatch):
    """Free mode has no budget ceiling either: remaining_budget must be 0.0,
    and the heuristic fallback text must not hallucinate ceiling/remaining-
    budget framing that doesn't exist in this mode."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    build_state = make_build_state()
    current_cost = sum(c.price_usd for c in build_state.values())

    # Payload correctness (built even though the call falls back to heuristic).
    resolved_info, _pct, direction = advisory._resolve_bottleneck(build_state, None)
    payload = advisory._build_request_payload(build_state, "Free", current_cost, None, resolved_info)
    assert payload["remaining_budget"] == 0.0
    assert payload["mode"] == "Free"

    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=current_cost)
    assert result["source"] == "heuristic"
    all_text = " ".join([result["within_budget"], result["stretch_budget"]])
    for banned in ("remaining budget", "your budget", "ceiling"):
        assert banned not in all_text.lower()
    # Free-mode framing should mention rebalancing the CPU/GPU platform
    # (unless there's genuinely no downgrade/upgrade available in the catalog).
    assert "platform" in all_text.lower() or "no further downgrade" in all_text.lower() or "already the" in all_text.lower()


def test_budget_mode_at_ceiling_has_zero_remaining_budget(seeded_db, monkeypatch):
    """A Budget-mode build that already consumes the entire ceiling must
    report remaining_budget == 0.0 in the payload, and mocked-LLM-response
    validation should still succeed normally (the mode-specific budget
    constraint is a prompt instruction to the LLM, not something Python
    enforces on a mocked response)."""
    _set_env(monkeypatch)
    build_state = make_build_state()
    total_cost = sum(c.price_usd for c in build_state.values())

    resolved_info, _pct, direction = advisory._resolve_bottleneck(build_state, None)
    payload = advisory._build_request_payload(build_state, "Budget", total_cost, None, resolved_info)
    assert payload["remaining_budget"] == 0.0

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(VALID_ADVISORY_PAYLOAD))
    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=total_cost)
    assert result["source"] == "llm"
    assert result["within_budget"] == VALID_ADVISORY_PAYLOAD["within_budget"]
    assert result["stretch_budget"] == VALID_ADVISORY_PAYLOAD["stretch_budget"]


def test_heuristic_workload_mode_mentions_profile_by_name(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    build_state = make_build_state()
    current_cost = sum(c.price_usd for c in build_state.values())

    result = advisory.get_build_advisory(
        build_state, mode="Workload", current_budget_or_cost=current_cost, profile="Video Editing"
    )
    assert result["source"] == "heuristic"
    assert "Video Editing" in result["within_budget"] or "Video Editing" in result["stretch_budget"]


def test_heuristic_budget_mode_uses_cost_neutral_framing(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    build_state = make_build_state()

    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert "cost-neutral" in result["within_budget"].lower()


def test_compatible_build_state_used_in_fixture(seeded_db):
    """Sanity check on the fixture itself: the mismatched-tier CPU+GPU (+
    matching Motherboard, if found) must actually be compatible, otherwise
    the advisory tests above would be exercising an invalid build."""
    build_state = make_build_state()
    if "Motherboard" in build_state:
        assert compatibility.evaluate_build(build_state).is_compatible is True
    assert solvers.get_compatible_candidates("GPU", {"CPU": build_state["CPU"]})
