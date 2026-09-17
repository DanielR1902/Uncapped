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
from engine import compatibility, scoring, solvers
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
    "within_budget": {
        "explanation": "Downgrade the GPU one tier and put the savings toward a stronger CPU.",
        "swaps": [],
        "can_optimize_further": False,
    },
    "stretch_budget": {
        "explanation": "Upgrade the CPU to the next tier up for an additional 45 USD to close the gap.",
        "actions": [],
        "added_cost_usd": 45.0,
    },
}


def _valid_payload_with_swaps(build_state: dict) -> dict:
    """A VALID_ADVISORY_PAYLOAD variant whose swaps reference real,
    currently-valid catalog ids drawn from `build_state` — for tests that
    need to exercise the post-parse hallucination guard's happy path."""
    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    cpu_candidates = solvers.get_compatible_candidates("CPU", build_state)
    real_cpu_id = next(c.id for c in cpu_candidates if c.id != build_state["CPU"].id)
    payload["within_budget"]["swaps"] = [{"category": "CPU", "replace_with_id": real_cpu_id}]
    return payload


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
    assert response.within_budget.model_dump() == VALID_ADVISORY_PAYLOAD["within_budget"]
    assert response.stretch_budget.model_dump() == VALID_ADVISORY_PAYLOAD["stretch_budget"]
    assert response.source == "llm"


def test_build_advisory_response_rejects_non_string_field():
    bad_payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    bad_payload["within_budget"] = ["not", "a", "string"]
    with pytest.raises(PydanticValidationError):
        BuildAdvisoryResponse.model_validate(bad_payload)


def test_build_advisory_response_rejects_hallucinated_swap_id(seeded_db):
    """The post-parse guard, not pydantic itself, is what rejects a
    hallucinated replace_with_id — pydantic only validates shape (int).
    This test documents that BuildAdvisoryResponse.model_validate() alone
    accepts a swap with a nonsense id; llm.advisory._validate_swap_ids is
    what actually catches it (covered separately below)."""
    build_state = make_build_state()
    payload = _valid_payload_with_swaps(build_state)
    payload["within_budget"]["swaps"] = [{"category": "CPU", "replace_with_id": 999999}]
    response = BuildAdvisoryResponse.model_validate(payload)
    assert response.within_budget.swaps[0].replace_with_id == 999999


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
        "within_budget": {"explanation": "tip", "swaps": [], "can_optimize_further": False},
        "stretch_budget": {"explanation": "upgrade", "actions": [], "added_cost_usd": 0.0},
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
    assert isinstance(result["within_budget"], dict)
    assert isinstance(result["within_budget"]["explanation"], str) and result["within_budget"]["explanation"]
    assert isinstance(result["within_budget"]["swaps"], list)
    assert isinstance(result["stretch_budget"], dict)
    assert isinstance(result["stretch_budget"]["explanation"], str) and result["stretch_budget"]["explanation"]
    assert isinstance(result["stretch_budget"]["actions"], list)
    assert isinstance(result["pros"], list) and len(result["pros"]) > 0
    assert isinstance(result["cons"], list) and len(result["cons"]) > 0
    # References real catalog data — the bottleneck-limiting/over-provisioned
    # component's real name should show up in at least one suggestion.
    all_text = " ".join([result["within_budget"]["explanation"], result["stretch_budget"]["explanation"]])
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
        "within_budget": {"explanation": ["not-a-string"], "swaps": [], "can_optimize_further": False},
        "stretch_budget": {"explanation": "ok", "actions": [], "added_cost_usd": 0.0},
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
        "within_budget": {"explanation": "tip", "swaps": [], "can_optimize_further": False},
        "stretch_budget": {"explanation": "upgrade", "actions": [], "added_cost_usd": 0.0},
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
    assert "well balanced" not in result["within_budget"]["explanation"]
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
    all_text = " ".join([result["within_budget"]["explanation"], result["stretch_budget"]["explanation"]])
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
    assert (
        "Video Editing" in result["within_budget"]["explanation"]
        or "Video Editing" in result["stretch_budget"]["explanation"]
    )


def test_heuristic_budget_mode_uses_cost_neutral_framing(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    build_state = make_build_state()

    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"
    assert "cost-neutral" in result["within_budget"]["explanation"].lower()


def test_compatible_build_state_used_in_fixture(seeded_db):
    """Sanity check on the fixture itself: the mismatched-tier CPU+GPU (+
    matching Motherboard, if found) must actually be compatible, otherwise
    the advisory tests above would be exercising an invalid build."""
    build_state = make_build_state()
    if "Motherboard" in build_state:
        assert compatibility.evaluate_build(build_state).is_compatible is True
    assert solvers.get_compatible_candidates("GPU", {"CPU": build_state["CPU"]})


# ---------------------------------------------------------------------------
# Post-parse hallucination guard: swaps must reference real, currently-valid
# catalog ids for their stated category, or the response falls to heuristic.
# ---------------------------------------------------------------------------
def test_get_build_advisory_falls_back_on_hallucinated_swap_id(seeded_db, monkeypatch):
    """A replace_with_id that is not a real compatible catalog candidate for
    its stated category must fall through to the heuristic path — proving
    Python (llm.advisory._validate_swap_ids), not just the prompt wording,
    enforces the zero-hallucination rule."""
    _set_env(monkeypatch)
    build_state = make_build_state()
    hallucinated_payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    hallucinated_payload["within_budget"]["swaps"] = [{"category": "CPU", "replace_with_id": 999999}]

    monkeypatch.setattr(
        advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(hallucinated_payload)
    )
    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"


def test_get_build_advisory_falls_back_on_swap_with_wrong_category(seeded_db, monkeypatch):
    """A real catalog id, but tagged under the WRONG category (a real RAM id
    labeled "GPU"), must also fall through to heuristic — the guard
    cross-checks the id against that specific category's compatible-
    candidate set, not just "is this id real for something in the catalog."
    """
    _set_env(monkeypatch)
    build_state = make_build_state()

    ram_candidates = solvers.get_compatible_candidates("RAM", build_state)
    assert ram_candidates
    real_ram_id = ram_candidates[0].id

    bad_payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    bad_payload["within_budget"]["swaps"] = [{"category": "GPU", "replace_with_id": real_ram_id}]

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(bad_payload))
    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "heuristic"


def test_get_build_advisory_accepts_real_swap_ids(seeded_db, monkeypatch):
    """Sanity counterpart to the two hallucination tests above: a swap with a
    genuinely real, currently-valid id for its stated category must NOT be
    rejected by the post-parse guard, and the response stays source="llm"."""
    _set_env(monkeypatch)
    build_state = make_build_state()
    good_payload = _valid_payload_with_swaps(build_state)

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(good_payload))
    result = advisory.get_build_advisory(build_state, mode="Budget", current_budget_or_cost=1500.0)
    assert result["source"] == "llm"
    # SwapAction now carries an `action` field (default "swap") that wasn't
    # in the raw payload — model_dump() fills it in, so compare against the
    # raw swaps with that default applied rather than the raw dicts verbatim.
    expected_swaps = [dict(swap, action="swap") for swap in good_payload["within_budget"]["swaps"]]
    assert result["within_budget"]["swaps"] == expected_swaps


# ---------------------------------------------------------------------------
# Heuristic fallback: swaps reference real catalog ids, can_optimize_further
# ---------------------------------------------------------------------------
def test_heuristic_swaps_reference_real_catalog_ids(seeded_db, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    build_state = make_build_state()

    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=1000.0)
    assert result["source"] == "heuristic"

    for swap in result["within_budget"]["swaps"]:
        candidates = solvers.get_compatible_candidates(swap["category"], build_state)
        valid_ids = {c.id for c in candidates}
        assert swap["replace_with_id"] in valid_ids

    for action in result["stretch_budget"]["actions"]:
        if action["action"] == "swap":
            candidates = solvers.get_compatible_candidates(action["category"], build_state)
            valid_ids = {c.id for c in candidates}
            assert action["replace_with_id"] in valid_ids
        else:  # set_quantity
            slot_count = advisory._real_slot_count(build_state, action["category"])
            assert slot_count is not None
            assert 1 <= action["quantity"] <= slot_count


def test_heuristic_can_optimize_further_true_for_unresolved_imbalance(seeded_db):
    """The fixture's CPU/GPU pair is deliberately mismatched (weak CPU,
    strong GPU) — a single heuristic downgrade (or paired downgrade+upgrade)
    essentially never perfectly equalizes the two benchmark scores, so
    can_optimize_further should stay True."""
    build_state = make_build_state()
    _, direction = scoring.bottleneck_percentage_baseline(build_state)
    assert direction != "Balanced"  # fixture is deliberately mismatched

    result = advisory._heuristic_within_budget(build_state, direction, mode="Free", profile=None)
    assert result["can_optimize_further"] is True


def test_heuristic_can_optimize_further_false_when_already_balanced(seeded_db):
    build_state = make_build_state()
    result = advisory._heuristic_within_budget(build_state, "Balanced", mode="Free", profile=None)
    assert result["swaps"] == []
    assert result["can_optimize_further"] is False


# ---------------------------------------------------------------------------
# stretch_budget multi-category fallthrough: a peaked bottleneck category no
# longer means "give up" — GPU/RAM/Storage/Cooler are tried in turn.
# ---------------------------------------------------------------------------
def test_heuristic_stretch_budget_falls_through_when_bottleneck_category_maxed(seeded_db, monkeypatch):
    """Force the CPU category to look already-maxed (no pricier compatible
    option) and assert the heuristic fallthrough finds SOME other real
    action (GPU swap, RAM/Storage set_quantity, or Cooler swap) rather than
    giving up with an empty actions list — the actual bug this task fixes.
    Deliberately does not assert one specific outcome: which category wins
    depends on the exact seeded catalog data (e.g. whether the fixture's GPU
    also happens to be maxed already)."""
    build_state = make_build_state()
    assert "Motherboard" in build_state, "fixture must include a Motherboard for RAM/Storage headroom"
    # make_build_state() only sets CPU/GPU/Motherboard — fill in RAM, an NVMe
    # Storage drive, and a Cooler too, so the fallthrough chain has every
    # high-impact category available to try, not just GPU.
    ram_candidates = solvers.get_compatible_candidates("RAM", build_state)
    assert ram_candidates
    build_state["RAM"] = ram_candidates[0]
    storage_candidates = solvers.get_compatible_candidates("Storage", build_state)
    assert storage_candidates
    nvme_storage = next((s for s in storage_candidates if s.interface and "NVMe" in s.interface), None)
    build_state["Storage"] = nvme_storage or storage_candidates[0]
    cooler_candidates = solvers.get_compatible_candidates("Cooler", build_state)
    assert cooler_candidates
    build_state["Cooler"] = cooler_candidates[0]

    real_get_compatible_candidates = solvers.get_compatible_candidates

    def fake_get_compatible_candidates(category, state):
        candidates = real_get_compatible_candidates(category, state)
        if category == "CPU":
            current = build_state["CPU"]
            # Simulate "already the priciest compatible CPU": drop every
            # candidate pricier than the current pick.
            return [c for c in candidates if c.price_usd <= current.price_usd]
        return candidates

    monkeypatch.setattr(advisory.solvers, "get_compatible_candidates", fake_get_compatible_candidates)

    result = advisory._heuristic_stretch_budget(build_state, "CPU-bound", mode="Free", profile=None)

    assert result["actions"], (
        "expected the fallthrough chain to find a real GPU/RAM/Storage/Cooler action, "
        f"got explanation: {result['explanation']!r}"
    )
    action = result["actions"][0]
    assert action["action"] in ("swap", "set_quantity")
    if action["action"] == "swap":
        assert action["category"] in ("GPU", "Cooler")
    else:
        assert action["category"] in ("RAM", "Storage")
    assert result["added_cost_usd"] > 0.0


# ---------------------------------------------------------------------------
# set_quantity actions: schema parsing + post-parse validation guard
# ---------------------------------------------------------------------------
def test_quantity_action_parses_from_raw_set_quantity_payload():
    from llm.schemas import QuantityAction

    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["stretch_budget"]["actions"] = [{"action": "set_quantity", "category": "RAM", "quantity": 2}]
    response = BuildAdvisoryResponse.model_validate(payload)

    action = response.stretch_budget.actions[0]
    assert isinstance(action, QuantityAction)
    assert action.category == "RAM"
    assert action.quantity == 2


def test_swap_action_parses_from_raw_swap_payload_inside_stretch_actions():
    from llm.schemas import SwapAction as SwapActionModel

    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["stretch_budget"]["actions"] = [{"action": "swap", "category": "GPU", "replace_with_id": 42}]
    response = BuildAdvisoryResponse.model_validate(payload)

    action = response.stretch_budget.actions[0]
    assert isinstance(action, SwapActionModel)
    assert action.category == "GPU"
    assert action.replace_with_id == 42


def test_set_quantity_action_within_ram_slots_survives_validation(seeded_db, monkeypatch):
    """A set_quantity action whose quantity is within the real motherboard
    ram_slots count must parse into a QuantityAction and survive
    _validate_advisory_actions, keeping source == "llm"."""
    _set_env(monkeypatch)
    build_state = make_build_state()
    assert "Motherboard" in build_state, "fixture must include a Motherboard for slot-count validation"
    # resolve_quantity_limit (and therefore _real_slot_count) needs a real
    # RAM pick to compute the real modules-per-kit math against — add one
    # from the catalog, same as make_build_state()'s other categories.
    ram_candidates = solvers.get_compatible_candidates("RAM", build_state)
    assert ram_candidates
    build_state["RAM"] = ram_candidates[0]
    ram_slots = advisory._real_slot_count(build_state, "RAM")
    assert ram_slots is not None and ram_slots >= 1

    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["stretch_budget"]["actions"] = [
        {"action": "set_quantity", "category": "RAM", "quantity": ram_slots}
    ]

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))
    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=1000.0)

    assert result["source"] == "llm"
    assert result["stretch_budget"]["actions"] == [
        {"action": "set_quantity", "category": "RAM", "quantity": ram_slots}
    ]


def test_set_quantity_action_exceeding_ram_slots_falls_back_to_heuristic(seeded_db, monkeypatch):
    _set_env(monkeypatch)
    build_state = make_build_state()
    assert "Motherboard" in build_state
    ram_candidates = solvers.get_compatible_candidates("RAM", build_state)
    assert ram_candidates
    build_state["RAM"] = ram_candidates[0]
    ram_slots = advisory._real_slot_count(build_state, "RAM")
    assert ram_slots is not None

    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["stretch_budget"]["actions"] = [
        {"action": "set_quantity", "category": "RAM", "quantity": ram_slots + 1}
    ]

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))
    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=1000.0)
    assert result["source"] == "heuristic"


def test_set_quantity_action_for_non_quantity_eligible_category_rejected(seeded_db, monkeypatch):
    """"RAM"/"Storage" are the only quantity-eligible categories — a
    set_quantity action for any other category (e.g. "CPU") must be rejected
    outright, falling to the heuristic path."""
    _set_env(monkeypatch)
    build_state = make_build_state()

    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["stretch_budget"]["actions"] = [{"action": "set_quantity", "category": "CPU", "quantity": 1}]

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))
    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=1000.0)
    assert result["source"] == "heuristic"


def test_set_quantity_action_for_non_nvme_storage_rejected(seeded_db, monkeypatch):
    """Storage's slot check (m2_slots) only applies when the current pick's
    interface contains "NVMe" — for any other interface (e.g. "SATA") there
    is no real slot-count constraint in this catalog, so no set_quantity
    action should ever validate for it."""
    _set_env(monkeypatch)
    from db.repositories import components_repo

    build_state = dict(make_build_state())
    non_nvme_storage = next(
        (s for s in components_repo.get_by_category("Storage") if not s.interface or "NVMe" not in s.interface),
        None,
    )
    assert non_nvme_storage is not None, "seeded catalog must include a non-NVMe storage option"
    build_state["Storage"] = non_nvme_storage

    payload = json.loads(json.dumps(VALID_ADVISORY_PAYLOAD))
    payload["stretch_budget"]["actions"] = [{"action": "set_quantity", "category": "Storage", "quantity": 1}]

    monkeypatch.setattr(advisory.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))
    result = advisory.get_build_advisory(build_state, mode="Free", current_budget_or_cost=1000.0)
    assert result["source"] == "heuristic"
