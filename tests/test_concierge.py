"""Concierge chat: mocked-HTTP success, heuristic fallback, and never-raises
coverage for llm/concierge.py.

Mocks the HTTP layer via pytest's built-in `monkeypatch` (no live OpenRouter
calls, per llm/CLAUDE.md). Unlike tests/test_advisory.py, this module never
touches the database or engine/ — catalog_summary/community_summary are
plain caller-supplied dicts, so a small hand-built fixture is enough (no
seeded DB needed).
"""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from llm import concierge
from llm.schemas import ConciergeResponse

CATALOG_SUMMARY = [
    {"id": 1, "category": "CPU", "name": "Ryzen 7 5800X3D", "price_usd": 329.0, "socket": "AM4"},
    {"id": 2, "category": "CPU", "name": "Intel Core i5-13600K", "price_usd": 289.0, "socket": "LGA1700"},
    {"id": 3, "category": "Motherboard", "name": "B550 Tomahawk", "price_usd": 179.0, "socket": "AM4"},
    {"id": 4, "category": "GPU", "name": "RTX 4070 Founders Edition", "price_usd": 599.0},
    {"id": 5, "category": "GPU", "name": "RTX 4060 Ti", "price_usd": 399.0},
    {"id": 6, "category": "RAM", "name": "Corsair Vengeance 32GB (2x16GB)", "price_usd": 89.0},
    {"id": 7, "category": "Storage", "name": "Samsung 980 Pro 1TB NVMe", "price_usd": 99.0},
    {"id": 8, "category": "PSU", "name": "Corsair RM750", "price_usd": 109.0},
    {"id": 9, "category": "Case", "name": "NZXT H510", "price_usd": 79.0},
    {"id": 10, "category": "Cooler", "name": "Noctua NH-D15", "price_usd": 99.0},
    {"id": 11, "category": "NetworkCard", "name": "TP-Link Archer TX3000E", "price_usd": 39.0},
    {"id": 12, "category": "OpticalDrive", "name": "LG WH16NS60 Blu-ray Writer", "price_usd": 42.0},
]

CURRENT_BUILD_CONTEXT = {
    "mode": "Free",
    "components": {
        "CPU": {"id": 1, "name": "Ryzen 7 5800X3D", "price_usd": 329.0},
        "Motherboard": {"id": 3, "name": "B550 Tomahawk", "price_usd": 179.0},
        "GPU": {"id": 4, "name": "RTX 4070 Founders Edition", "price_usd": 599.0},
        "RAM": {"id": 6, "name": "Corsair Vengeance 32GB (2x16GB)", "price_usd": 89.0},
        "Storage": {"id": 7, "name": "Samsung 980 Pro 1TB NVMe", "price_usd": 99.0},
        "PSU": {"id": 8, "name": "Corsair RM750", "price_usd": 109.0},
        "Case": {"id": 9, "name": "NZXT H510", "price_usd": 79.0},
        "Cooler": {"id": 10, "name": "Noctua NH-D15", "price_usd": 99.0},
    },
    "quantities": {},
}

ADVISORY_CONTEXT = {
    "pros": ["Strong CPU/GPU balance for the stated workload."],
    "cons": ["RAM capacity is a bit light for heavy multitasking."],
    "within_budget": {
        "explanation": "Downgrade the Case to fund a better Cooler.",
        "swaps": [],
        "can_optimize_further": False,
    },
    "stretch_budget": {
        "explanation": "Add a second RAM kit for 64GB total (+89.00 USD) to remove the multitasking limit.",
        "actions": [],
        "added_cost_usd": 89.0,
    },
    "source": "llm",
}

COMMUNITY_SUMMARY = [
    {
        "post_id": 1,
        "build_id": 1,
        "title": "Budget 1440p Gaming Rig",
        "creation_mode": "Workload",
        "workload_profile": "Gaming",
        "workload_tier": "Mid",
        "total_cost": 1450.0,
        "author_notes": "Great value for 1440p gaming.",
    },
    {
        "post_id": 2,
        "build_id": 2,
        "title": "Video Editing Powerhouse",
        "creation_mode": "Workload",
        "workload_profile": "Video Editing",
        "workload_tier": "High",
        "total_cost": 2800.0,
        "author_notes": "Fast exports, lots of RAM.",
    },
]

DRAFTS_SUMMARY = [
    {"draft_id": 101, "name": "pc-master-race", "mode": "Free"},
    {"draft_id": 102, "name": "Weekend WIP", "mode": "Budget"},
]

PREVIOUS_BUILDS_SUMMARY = [
    {"build_id": 201, "name": "Ultra Rig", "creation_mode": "Free"},
    {"build_id": 202, "name": "Office PC", "creation_mode": "Budget"},
]


def _fake_openrouter_response(payload: dict, status_code: int = 200) -> httpx.Response:
    body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", concierge.OPENROUTER_URL))


def _set_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")


# ---------------------------------------------------------------------------
# pydantic parsing
# ---------------------------------------------------------------------------
def test_concierge_response_parses_plain_reply_no_action():
    payload = {"reply": "We have 2 CPUs: Ryzen 7 5800X3D and Intel Core i5-13600K.", "action": None}
    response = ConciergeResponse.model_validate(payload)
    assert response.reply == payload["reply"]
    assert response.action is None
    assert response.source == "llm"


def test_concierge_response_parses_load_build_action():
    payload = {
        "reply": "Here's a build centered on the RTX 4070.",
        "action": {
            "type": "load_build",
            "components": {"CPU": 1, "GPU": 4},
            "explanation": "Paired a strong CPU with the requested GPU.",
        },
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action is not None
    assert response.action.components == {"CPU": 1, "GPU": 4}


def test_concierge_response_requires_reply_field():
    with pytest.raises(PydanticValidationError):
        ConciergeResponse.model_validate({"action": None})


# ---------------------------------------------------------------------------
# Intent 1: catalog questions
# ---------------------------------------------------------------------------
def test_catalog_question_intent_passes_through(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Our cheapest GPU is the RTX 4060 Ti at 399.00 USD; we also carry the RTX 4070 "
        "Founders Edition at 599.00 USD.",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "What GPUs do you have?", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert "RTX 4060 Ti" in result["reply"]


# ---------------------------------------------------------------------------
# Intent 2: community recommendations
# ---------------------------------------------------------------------------
def test_community_recommendation_intent_passes_through(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Check out 'Budget 1440p Gaming Rig' — a Gaming build at 1450.00 USD, well under "
        "your 2000 USD budget.",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Recommend a gaming build under 2000 USD", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert "Budget 1440p Gaming Rig" in result["reply"]
    assert result["reply"] != "We have 2 CPUs: Ryzen 7 5800X3D and Intel Core i5-13600K."


# ---------------------------------------------------------------------------
# Intent 3: build-me requests
# ---------------------------------------------------------------------------
def test_build_me_intent_with_valid_action_passes_through(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Built a rig around the RTX 4070 and Ryzen 7 5800X3D.",
        "action": {
            "type": "load_build",
            "components": {
                "CPU": 1,
                "Motherboard": 3,
                "GPU": 4,
                "RAM": 6,
                "Storage": 7,
                "PSU": 8,
                "Case": 9,
                "Cooler": 10,
            },
            "explanation": "Matched the requested CPU/GPU and filled the rest with compatible picks.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Build me a PC with an RTX 4070 and Ryzen 5800X3D", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"] == payload["action"]


# ---------------------------------------------------------------------------
# Hallucination guards
# ---------------------------------------------------------------------------
def test_load_build_action_with_unknown_id_falls_back_to_heuristic(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Here's a build.",
        "action": {
            "type": "load_build",
            "components": {"CPU": 999999, "GPU": 4},
            "explanation": "made up id",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response("Build me a PC", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_load_build_action_with_wrong_category_falls_back_to_heuristic(monkeypatch):
    """A real id (6 is a real RAM id), but tagged under the wrong category
    (GPU) — must fall through to heuristic. The guard cross-checks the id
    against that specific category's real ids, not just "is this id real for
    something in the catalog."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Here's a build.",
        "action": {
            "type": "load_build",
            "components": {"CPU": 1, "GPU": 6},
            "explanation": "wrong category for id 6",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response("Build me a PC", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"
    assert result["action"] is None


# ---------------------------------------------------------------------------
# Standard failure modes — always fall back, never raise
# ---------------------------------------------------------------------------
def test_falls_back_when_env_not_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"
    assert result["action"] is None
    assert isinstance(result["reply"], str) and result["reply"]


def test_falls_back_on_timeout(monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"


def test_falls_back_on_connection_error(monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"


def test_falls_back_on_malformed_json_content(monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        body = {"choices": [{"message": {"content": "not valid json {{{"}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", concierge.OPENROUTER_URL))

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"


def test_falls_back_on_unexpected_response_shape(monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            200, json={"unexpected": "shape"}, request=httpx.Request("POST", concierge.OPENROUTER_URL)
        )

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"


def test_falls_back_on_server_error(monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            500, json={"error": "server error"}, request=httpx.Request("POST", concierge.OPENROUTER_URL)
        )

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"


def test_falls_back_on_rate_limit(monkeypatch):
    _set_env(monkeypatch)

    def fake_post(url, timeout=None, headers=None, json=None):
        return httpx.Response(
            429, json={"error": "rate limited"}, request=httpx.Request("POST", concierge.OPENROUTER_URL)
        )

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"


def test_falls_back_on_schema_validation_failure_missing_required_field(monkeypatch):
    """`reply` is required on ConciergeResponse — a payload missing it must
    fail validation and fall through to the heuristic path."""
    _set_env(monkeypatch)
    invalid_payload = {"action": None}

    monkeypatch.setattr(
        concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(invalid_payload)
    )
    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_heuristic_response_shape_is_always_well_formed(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = concierge.get_concierge_response("anything", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert set(result.keys()) == {"reply", "action", "source"}
    assert result["source"] == "heuristic"
    assert result["action"] is None
    assert isinstance(result["reply"], str) and result["reply"]


# ---------------------------------------------------------------------------
# Conversation history is forwarded to the payload verbatim
# ---------------------------------------------------------------------------
def test_conversation_history_reaches_the_request_payload(monkeypatch):
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    history = [
        {"role": "user", "content": "What CPUs do you have?"},
        {"role": "assistant", "content": "We have a couple of options..."},
    ]
    result = concierge.get_concierge_response(
        "And what about GPUs?", history, CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["conversation_history"] == history
    assert sent_payload["user_message"] == "And what about GPUs?"
    assert sent_payload["catalog_summary"] == CATALOG_SUMMARY
    assert sent_payload["community_summary"] == COMMUNITY_SUMMARY


# ---------------------------------------------------------------------------
# Intent 4: incremental modify_build requests
# ---------------------------------------------------------------------------
def test_modify_build_action_with_new_categories_passes_through(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Added a network card and an optical drive to your build.",
        "action": {
            "type": "modify_build",
            "components": {"NetworkCard": 11, "OpticalDrive": 12},
            "quantities": {},
            "explanation": "Added the requested peripherals.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Add a network card and optical drive",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] == payload["action"]


def test_modify_build_action_with_object_shaped_components_is_coerced_to_plain_ids(monkeypatch):
    """Reproduces a real, confirmed bug: a live (non-mocked) LLM call, on a
    second turn asking to bump RAM/Storage quantities (with advisory_context
    also present), returned a `modify_build.components` value shaped like
    `current_build_context["components"]`'s OWN `{"id", "name", "price_usd"}`
    object shape instead of the plain `{category: int}` shape the schema
    requires — Pydantic correctly rejected this, falling back to the
    unhelpful heuristic reply for what was actually a simple, valid request.
    `_coerce_component_id_shapes` fixes this by extracting the id from a
    recognizable `{"id": <int>, ...}` value before validation, so the action
    still applies instead of silently degrading."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Increased Storage to 2 units and upgraded RAM to 64GB.",
        "action": {
            "type": "modify_build",
            "components": {
                "Storage": {"id": 7, "name": "Samsung 980 Pro 1TB NVMe", "price_usd": 99.0},
                "RAM": {"id": 6, "name": "Corsair Vengeance 32GB (2x16GB)", "price_usd": 89.0},
            },
            "quantities": {"Storage": 2, "RAM": 2},
            "explanation": "Doubled storage and RAM.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Can you upgrade the storage a bit? maybe another slot? same with ram",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
        advisory_context=ADVISORY_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["type"] == "modify_build"
    # Coerced down to plain ids — never the raw object the mocked LLM returned.
    assert result["action"]["components"] == {"Storage": 7, "RAM": 6}
    assert result["action"]["quantities"] == {"Storage": 2, "RAM": 2}


def test_coerce_component_id_shapes_leaves_already_correct_ids_untouched():
    """The coercion must be a no-op for the normal/correct case — a plain
    int id must never be altered."""
    raw = {"reply": "ok", "action": {"type": "modify_build", "components": {"RAM": 6}, "quantities": {}}}
    coerced = concierge._coerce_component_id_shapes(raw)
    assert coerced["action"]["components"] == {"RAM": 6}


def test_coerce_component_id_shapes_handles_missing_or_non_dict_action():
    """Must not raise for action: null or a non-dict components value (an
    already-invalid shape Pydantic will reject on its own either way)."""
    assert concierge._coerce_component_id_shapes({"reply": "hi", "action": None}) == {
        "reply": "hi",
        "action": None,
    }
    raw = {"reply": "hi", "action": {"type": "navigate", "navigate_to": "community"}}
    assert concierge._coerce_component_id_shapes(raw) == raw


def test_modify_build_action_with_missing_explanation_still_parses(monkeypatch):
    """Reproduces a real, confirmed regression: the STRICT BREVITY RULE
    sometimes led the model to omit the `explanation` field entirely on a
    modify_build action (plausibly over-applying "be terse" to internal JSON
    fields, not just the user-facing `reply`) — since `explanation` used to
    be REQUIRED with no default, this failed Pydantic validation outright
    and fell back to the heuristic for an otherwise perfectly valid quantity
    change. `explanation` is now optional (defaults to "") on every
    Concierge action type, since it has no functional consumer anywhere in
    ui/ — only `reply` is ever shown to the user."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Increased Storage and RAM to 2 units each.",
        "action": {
            "type": "modify_build",
            "components": {},
            "quantities": {"Storage": 2, "RAM": 2},
            # Deliberately no "explanation" key at all.
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "upgrade storage to 2 and ram to 2",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
        advisory_context=ADVISORY_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["type"] == "modify_build"
    assert result["action"]["quantities"] == {"Storage": 2, "RAM": 2}
    assert result["action"]["explanation"] == ""


def test_complete_build_request_returns_modify_build_filling_only_empty_categories(monkeypatch):
    """Reproduces a real, confirmed bug: a live (non-mocked) LLM call, given a
    `current_build_context` with ONLY a manually-picked CPU and GPU (nothing
    else), on a "Complete this build for me" request returned a `load_build`
    action with a COMPLETELY DIFFERENT CPU and GPU than the ones already
    picked -- `load_build` always starts a fresh build from scratch, so it
    silently discarded the user's own manual picks instead of preserving them
    and filling only the empty slots. The fix is prompt-only (SYSTEM_PROMPT's
    intent 4 COMPLETING/FINISHING AN EXISTING BUILD rule): a "complete"/
    "finish this build" request against a build that already has real
    components must resolve to `modify_build`, patching in ONLY the
    currently-empty categories, never `load_build`. Since this is fundamentally
    a prompt-following behavior (not something Python can enforce structurally
    -- `_validate_action` has no way to know intent from wording), this is a
    plumbing-level test: it confirms the CORRECT shape of response (a
    `modify_build` action whose `components` does NOT touch the already-filled
    CPU/GPU and DOES fill in the remaining empty categories) passes through
    cleanly, matching this project's existing "prompt-only rules get plumbing
    tests, not behavior-proof tests" convention (e.g. the budget-guardrail/
    advisory-synthesis tests above)."""
    _set_env(monkeypatch)
    partial_build_context = {
        "mode": "Free",
        "components": {
            "CPU": {"id": 1, "name": "Ryzen 7 5800X3D", "price_usd": 329.0},
            "GPU": {"id": 4, "name": "RTX 4070 Founders Edition", "price_usd": 599.0},
        },
        "quantities": {},
    }
    payload = {
        "reply": "Filled in the remaining parts around your CPU and GPU.",
        "action": {
            "type": "modify_build",
            "components": {
                "Motherboard": 3,
                "RAM": 6,
                "Storage": 7,
                "PSU": 8,
                "Case": 9,
                "Cooler": 10,
            },
            "quantities": {},
            "explanation": "Filled in every remaining core category, leaving the existing CPU/GPU untouched.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Complete this build for me",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=partial_build_context,
    )

    assert result["source"] == "llm"
    assert result["action"]["type"] == "modify_build"
    # The already-filled categories must NOT appear in the patch...
    assert "CPU" not in result["action"]["components"]
    assert "GPU" not in result["action"]["components"]
    # ...and every previously-empty core category must be filled in.
    assert set(result["action"]["components"]) == {
        "Motherboard",
        "RAM",
        "Storage",
        "PSU",
        "Case",
        "Cooler",
    }


def test_modify_build_action_with_bad_quantity_category_falls_back_to_heuristic(monkeypatch):
    """quantities may only ever reference RAM/Storage — a CPU quantity makes
    no sense and must be rejected, falling back to heuristic."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Bumping your CPU count.",
        "action": {
            "type": "modify_build",
            "components": {},
            "quantities": {"CPU": 2},
            "explanation": "nonsensical quantity category",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Give me two CPUs",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_modify_build_with_no_active_build_context_says_nothing_to_modify(monkeypatch):
    """current_build_context=None (no active draft) — the LLM is expected to
    say there's nothing to modify and return action: null; this must pass
    through cleanly, not be treated as a failure."""
    _set_env(monkeypatch)
    payload = {"reply": "You don't have an active build to modify yet.", "action": None}
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Add a network card to my build",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=None,
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert "active build" in result["reply"] or "build" in result["reply"]


# ---------------------------------------------------------------------------
# Intent 5: navigate requests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page_key", ["landing", "create_build", "my_builds", "community", "drafts"])
def test_navigate_action_parses_for_each_valid_page_key(monkeypatch, page_key):
    _set_env(monkeypatch)
    payload = {
        "reply": f"Taking you to {page_key}.",
        "action": {"type": "navigate", "navigate_to": page_key},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Take me there", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"] == {
        "type": "navigate",
        "navigate_to": page_key,
        "filters": None,
        "reset_mode": False,
    }


def test_navigate_action_with_invalid_page_key_falls_back_to_heuristic(monkeypatch):
    """navigate_to is constrained by a Literal — an invalid key like
    "previous_builds" (not a real page key in this codebase) or "settings"
    must fail Pydantic validation and fall back to heuristic."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Taking you to your previous builds.",
        "action": {"type": "navigate", "navigate_to": "previous_builds"},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Take me to my previous builds", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


# ---------------------------------------------------------------------------
# Intent 5 extension: navigate with `filters`
# ---------------------------------------------------------------------------
def test_navigate_action_with_budget_filters_parses_and_passes_through(monkeypatch):
    """A navigate action carrying a Budget-shaped filters payload (max_price
    set, domain/tier null) must parse and reach the caller unmodified —
    ui/views/community.py, not this module, resolves max_price into a real
    selectbox option string."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Here are the budget builds under 2000 USD.",
        "action": {
            "type": "navigate",
            "navigate_to": "community",
            "filters": {"build_type": "Budget", "max_price": 2000.0, "domain": None, "tier": None},
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Take me to budget builds under 2000", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"]["type"] == "navigate"
    assert result["action"]["filters"] == {
        "build_type": "Budget",
        "max_price": 2000.0,
        "domain": None,
        "tier": None,
    }


def test_navigate_action_with_workload_filters_parses_and_passes_through(monkeypatch):
    """A navigate action carrying a Workload-shaped filters payload
    (domain/tier set, max_price null)."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Here are the gaming builds.",
        "action": {
            "type": "navigate",
            "navigate_to": "community",
            "filters": {"build_type": "Workload", "max_price": None, "domain": "Gaming", "tier": None},
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Show me gaming builds", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"]["filters"]["build_type"] == "Workload"
    assert result["action"]["filters"]["domain"] == "Gaming"


def test_navigate_action_with_invalid_filters_build_type_falls_back_to_heuristic(monkeypatch):
    """filters.build_type is a closed Literal — a value outside
    All/Budget/Workload/Free must fail pydantic validation, same precedent
    as an invalid navigate_to."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Here you go.",
        "action": {
            "type": "navigate",
            "navigate_to": "community",
            "filters": {"build_type": "Enthusiast", "max_price": None, "domain": None, "tier": None},
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Take me to community", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_navigate_action_ignores_legacy_save_as_draft_field_from_llm(monkeypatch):
    """Regression guard for the explicit, deliberate removal of the
    `save_as_draft` field/auto-stash-to-Drafts mechanism: even if a
    non-compliant (e.g. stale-prompt-cached, or hallucinating) LLM response
    still includes a `"save_as_draft": true` key on a navigate action,
    `ConciergeNavigateAction` no longer declares that field at all, so
    Pydantic silently drops the unrecognized key on parse — the resulting
    action must NOT carry it, and there must be nothing in this module's
    plumbing that could act on it."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Taking you to Community.",
        "action": {
            "type": "navigate",
            "navigate_to": "community",
            "save_as_draft": True,
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Take me to Community",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] == {
        "type": "navigate",
        "navigate_to": "community",
        "filters": None,
        "reset_mode": False,
    }
    assert "save_as_draft" not in result["action"]


def test_navigate_action_without_filters_defaults(monkeypatch):
    """A plain navigate action (no filters mentioned by the mocked response)
    must still parse, defaulting filters to None, matching the pre-existing
    navigate tests' minimal payload shape. There is no `save_as_draft` field
    to default anymore — it was removed from the schema entirely."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Taking you to My Builds.",
        "action": {"type": "navigate", "navigate_to": "my_builds"},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Show my previous builds", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"]["filters"] is None
    assert "save_as_draft" not in result["action"]


# ---------------------------------------------------------------------------
# advisory_context: new optional parameter (intent 6 — optimization/analysis)
# ---------------------------------------------------------------------------
def test_advisory_context_synthesized_reply_passes_through(monkeypatch):
    """A mocked concise, synthesized reply grounded in advisory_context — the
    plumbing must pass it through unmodified with source == "llm". The actual
    synthesis judgment is the model's job (untestable here); this only checks
    that a well-formed response using advisory_context isn't rejected or
    altered."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Your RAM is a bit light for multitasking — adding a second kit for 64GB would help most.",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "How can I optimize this build?",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
        advisory_context=ADVISORY_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert result["reply"] == payload["reply"]


def test_advisory_context_reaches_the_request_payload_when_provided(monkeypatch):
    """Inspect the mocked httpx.post call's JSON body to confirm
    advisory_context is actually threaded into the request payload sent to
    OpenRouter, not just accepted and dropped."""
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    result = concierge.get_concierge_response(
        "Analyze my build",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
        advisory_context=ADVISORY_CONTEXT,
    )

    assert result["source"] == "llm"
    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["advisory_context"] == ADVISORY_CONTEXT


def test_active_currency_reaches_the_request_payload_when_provided(monkeypatch):
    """Same inspection as advisory_context above, for active_currency
    (ui/format.py's multi-currency support, spec.md §7.7) — confirms it's
    actually threaded into the request payload, not just accepted/dropped."""
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    result = concierge.get_concierge_response(
        "What CPUs do you have?",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        active_currency="NIS",
    )

    assert result["source"] == "llm"
    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["active_currency"] == "NIS"


def test_active_currency_defaults_to_usd_when_omitted(monkeypatch):
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    concierge.get_concierge_response("What CPUs do you have?", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)

    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["active_currency"] == "USD"


def test_currency_rates_reaches_the_request_payload_when_provided(monkeypatch):
    """currency_rates (ui.format.CURRENCY_RATES) must actually be threaded
    into the request payload -- it's what lets the model convert a
    foreign-currency-stated budget ("build me a PC for 7000 NIS") to USD
    before selecting parts (SYSTEM_PROMPT intent 3's BUDGET CURRENCY
    CONVERSION rule)."""
    _set_env(monkeypatch)
    captured_payload = {}
    rates = {"USD": 1.0, "EUR": 0.92, "NIS": 3.70}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    concierge.get_concierge_response(
        "build me a PC for 7000 NIS",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        active_currency="NIS",
        currency_rates=rates,
    )

    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["currency_rates"] == rates


def test_currency_rates_defaults_to_usd_only_when_omitted(monkeypatch):
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    concierge.get_concierge_response("What CPUs do you have?", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)

    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["currency_rates"] == {"USD": 1.0}


# ---------------------------------------------------------------------------
# currency_switch (spec.md §6.7/§7.10) — a top-level ConciergeResponse field,
# separate from `action`, set whenever the user explicitly names a currency.
# ---------------------------------------------------------------------------
def test_currency_switch_parses_alongside_a_load_build_action(monkeypatch):
    """"build me a gaming PC for 10000 NIS" -> both a load_build action AND
    currency_switch: "NIS" in the SAME response -- currency_switch is a
    sibling field to action, not nested inside it."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Built you a gaming PC within budget.",
        "action": {
            "type": "load_build",
            "components": {"CPU": 1, "GPU": 4},
            "explanation": "Balanced pick.",
        },
        "currency_switch": "NIS",
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "build me a gaming PC for 10000 NIS",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        active_currency="USD",
        currency_rates={"USD": 1.0, "EUR": 0.92, "NIS": 3.70},
    )

    assert result["source"] == "llm"
    assert result["currency_switch"] == "NIS"
    assert result["action"]["type"] == "load_build"


def test_currency_switch_parses_standalone_with_no_action(monkeypatch):
    """"switch to NIS" -- currency_switch fires with action: null."""
    _set_env(monkeypatch)
    payload = {"reply": "Switched to NIS.", "action": None, "currency_switch": "NIS"}
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "switch to NIS", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY, active_currency="USD"
    )

    assert result["source"] == "llm"
    assert result["currency_switch"] == "NIS"
    assert result["action"] is None


def test_currency_switch_defaults_to_none_when_not_mentioned():
    """A plain reply with no currency_switch key at all must default to
    None, matching every other optional ConciergeResponse field."""
    response = ConciergeResponse.model_validate({"reply": "Here are the CPUs.", "action": None})
    assert response.currency_switch is None


def test_currency_switch_rejects_an_invalid_currency_code(monkeypatch):
    """A hallucinated/invalid currency code must fail Pydantic validation
    outright (the Literal type is the structural guarantee, not just a
    prompt instruction) and fall back to the heuristic response."""
    _set_env(monkeypatch)
    payload = {"reply": "Switched.", "action": None, "currency_switch": "GBP"}
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response("switch to pounds", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)

    assert result["source"] == "heuristic"


def test_advisory_context_defaults_to_empty_dict_when_none(monkeypatch):
    """When the caller doesn't pass advisory_context at all (None default),
    the payload sent to OpenRouter must contain an empty dict, not None,
    matching current_build_context's existing None -> {} behavior."""
    _set_env(monkeypatch)
    captured_payload = {}

    def fake_post(url, timeout=None, headers=None, json=None):
        captured_payload.update(json)
        return _fake_openrouter_response({"reply": "ok", "action": None})

    monkeypatch.setattr(concierge.httpx, "post", fake_post)

    result = concierge.get_concierge_response("Analyze my build", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)

    assert result["source"] == "llm"
    sent_payload = json.loads(captured_payload["messages"][1]["content"])
    assert sent_payload["advisory_context"] == {}
    assert sent_payload["current_build_context"] == {}


def test_build_payload_includes_advisory_context_directly():
    """Directly exercises _build_payload (not just the full request path) to
    confirm the new "advisory_context" key is present with the given value,
    and defaults to {} when omitted."""
    payload_with_advisory = concierge._build_payload(
        "hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY, None, ADVISORY_CONTEXT
    )
    assert payload_with_advisory["advisory_context"] == ADVISORY_CONTEXT

    payload_without_advisory = concierge._build_payload("hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert payload_without_advisory["advisory_context"] == {}


# ---------------------------------------------------------------------------
# Budget guardrail wording: pure action:null plumbing (no new server-side
# validation — the reasoning behind this reply is the model's job, not
# unit-testable; this only confirms the wording doesn't confuse the existing
# action:null pass-through path).
# ---------------------------------------------------------------------------
def test_budget_guard_reply_template_passes_through_as_ordinary_null_action(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "This upgrade will exceed your budget by USD 45.00. Would you like to proceed anyway?",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Swap my GPU for the RTX 4070",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert result["reply"] == payload["reply"]


# ---------------------------------------------------------------------------
# Intent 7: save & publish requests
# ---------------------------------------------------------------------------
def test_concierge_response_parses_save_build_action():
    payload = {
        "reply": "Saved as 'My Rig'! Would you like to publish it to the Community as well?",
        "action": {
            "type": "save_build",
            "name": "My Rig",
            "destination": "build",
            "explanation": "Saving your current build.",
        },
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action is not None
    assert response.action.type == "save_build"
    assert response.action.name == "My Rig"
    assert response.action.destination == "build"
    assert response.action.explanation == "Saving your current build."


def test_concierge_save_build_action_requires_name_field():
    """`name` is a REQUIRED field on ConciergeSaveBuildAction — a genuine
    Pydantic-level guarantee (not just a prompt instruction) that the model
    must have actually gathered a name from the user before this action can
    ever be constructed. Omitting it must fail validation."""
    payload = {
        "reply": "Saved!",
        "action": {"type": "save_build", "destination": "build", "explanation": "Saving."},
    }
    with pytest.raises(PydanticValidationError):
        ConciergeResponse.model_validate(payload)


def test_concierge_save_build_action_requires_destination_field():
    """Same guarantee as above, for `destination` — omitting it must fail
    validation rather than silently defaulting to either table."""
    payload = {
        "reply": "Saved!",
        "action": {"type": "save_build", "name": "My Rig", "explanation": "Saving."},
    }
    with pytest.raises(PydanticValidationError):
        ConciergeResponse.model_validate(payload)


def test_concierge_save_build_action_parses_fine_with_both_fields_present():
    """Sanity check: a well-formed action with both required fields present
    parses cleanly for either real destination value."""
    for destination in ("draft", "build"):
        payload = {
            "reply": "Saved!",
            "action": {
                "type": "save_build",
                "name": "My Rig",
                "destination": destination,
                "explanation": "Saving.",
            },
        }
        response = ConciergeResponse.model_validate(payload)
        assert response.action.destination == destination


def test_concierge_response_parses_publish_build_action_with_null_author_notes():
    payload = {
        "reply": "Published without a description.",
        "action": {"type": "publish_build", "author_notes": None},
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action is not None
    assert response.action.type == "publish_build"
    assert response.action.author_notes is None


def test_concierge_response_parses_publish_build_action_with_author_notes():
    payload = {
        "reply": "Published with your notes.",
        "action": {"type": "publish_build", "author_notes": "Great 1440p gaming build on a budget."},
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action.author_notes == "Great 1440p gaming build on a budget."


def test_save_build_first_message_asks_for_name_and_destination_with_no_action(monkeypatch):
    """The FIRST "save this build" message with an active build_context must
    NOT return an action yet — the model asks for both a name and a
    destination and waits. This only confirms the plumbing passes such a
    reply through unmodified (the model's own reasoning produces the actual
    question text)."""
    _set_env(monkeypatch)
    payload = {
        "reply": "What name would you like to give this build? Also, should I save it as a Draft or a finished Build?",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Save this PC to my list",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert "name" in result["reply"].lower()
    assert "draft" in result["reply"].lower() or "build" in result["reply"].lower()


def test_save_build_followup_with_both_name_and_destination_returns_action(monkeypatch):
    """A follow-up message answering the model's own immediately-prior
    name+destination question with BOTH pieces of information returns the
    real save_build action carrying those exact values."""
    _set_env(monkeypatch)
    history = [
        {"role": "user", "content": "Save this PC to my list"},
        {
            "role": "assistant",
            "content": "What name would you like to give this build? Also, should I save it as a Draft or a finished Build?",
        },
    ]
    payload = {
        "reply": "Saved as 'Weekend Gaming Rig'! Would you like to publish it to the Community as well?",
        "action": {
            "type": "save_build",
            "name": "Weekend Gaming Rig",
            "destination": "build",
            "explanation": "Saving the active build as a finished build.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Call it Weekend Gaming Rig, and save it as a finished build",
        history,
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] == {
        "type": "save_build",
        "name": "Weekend Gaming Rig",
        "destination": "build",
        "publish_immediately": False,
        "author_notes": None,
        "source": "studio",
        "source_post_id": None,
        "explanation": "Saving the active build as a finished build.",
    }
    assert "weekend gaming rig" in result["reply"].lower()
    assert "publish" in result["reply"].lower()


def test_save_build_followup_with_draft_destination_does_not_ask_about_publishing(monkeypatch):
    """A "draft"-destination save has nothing to publish — the reply for
    that turn must confirm the draft save only, never ask about publishing."""
    _set_env(monkeypatch)
    history = [
        {"role": "user", "content": "Save this PC to my list"},
        {
            "role": "assistant",
            "content": "What name would you like to give this build? Also, should I save it as a Draft or a finished Build?",
        },
    ]
    payload = {
        "reply": "Saved 'WIP Rig' as a draft.",
        "action": {
            "type": "save_build",
            "name": "WIP Rig",
            "destination": "draft",
            "explanation": "Saving the active build as a draft.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Call it WIP Rig, save it as a draft for now",
        history,
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["destination"] == "draft"
    assert "publish" not in result["reply"].lower()


def test_save_build_followup_with_only_name_asks_for_missing_destination(monkeypatch):
    """A follow-up giving only a name (no destination wording) must NOT guess
    the missing piece — action stays null, and the model asks specifically
    for what's still missing."""
    _set_env(monkeypatch)
    history = [
        {"role": "user", "content": "Save this PC to my list"},
        {
            "role": "assistant",
            "content": "What name would you like to give this build? Also, should I save it as a Draft or a finished Build?",
        },
    ]
    payload = {
        "reply": "Got it — should I save it as a Draft or a finished Build?",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Call it Weekend Gaming Rig",
        history,
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] is None
    assert "draft" in result["reply"].lower() or "build" in result["reply"].lower()


def test_save_build_intent_with_no_active_build_says_nothing_to_save(monkeypatch):
    _set_env(monkeypatch)
    payload = {"reply": "You don't have an active build to save yet.", "action": None}
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Save this PC to my list",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=None,
    )

    assert result["source"] == "llm"
    assert result["action"] is None


# ---------------------------------------------------------------------------
# Fast-track save/publish: the user's FIRST message already unambiguously
# supplies name + destination (+ optional publish intent) up front, so the
# model skips straight to firing save_build instead of asking questions it
# already has the answer to.
# ---------------------------------------------------------------------------
def test_save_build_action_parses_with_publish_immediately_and_author_notes():
    payload = {
        "reply": "Saved 'Workstation' and published it to the Community!",
        "action": {
            "type": "save_build",
            "name": "Workstation",
            "destination": "build",
            "publish_immediately": True,
            "author_notes": "Built for heavy multitasking.",
            "explanation": "Saving and publishing in one turn.",
        },
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action.publish_immediately is True
    assert response.action.author_notes == "Built for heavy multitasking."


def test_save_build_action_defaults_publish_immediately_false_and_author_notes_none():
    """Both new fields are optional with safe defaults — an ordinary
    save_build action (the common case) that never mentions either must
    still parse cleanly without the model having to set them explicitly."""
    payload = {
        "reply": "Saved!",
        "action": {"type": "save_build", "name": "My Rig", "destination": "draft"},
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action.publish_immediately is False
    assert response.action.author_notes is None


def test_save_build_fast_track_first_message_with_draft_name_fires_immediately(monkeypatch):
    """"save this as draft named Silent Beast" already answers BOTH
    questions in one message -- the model must fire save_build on this VERY
    FIRST turn, never ask "what name" / "draft or build" first."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved 'Silent Beast' as a draft.",
        "action": {
            "type": "save_build",
            "name": "Silent Beast",
            "destination": "draft",
            "explanation": "Fast-track: name and destination given up front.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save this as draft named Silent Beast",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["type"] == "save_build"
    assert result["action"]["name"] == "Silent Beast"
    assert result["action"]["destination"] == "draft"
    assert "publish" not in result["reply"].lower()


def test_save_build_fast_track_first_message_with_build_name_asks_only_about_publish(monkeypatch):
    """"save this as a final build named Ultra Rig" already answers both
    questions (a finished build, named Ultra Rig, via the unambiguous
    "final build" qualifier) -- fires immediately, and since publishing
    wasn't mentioned, the reply asks ONLY the one remaining question
    (whether to publish), not the original name+destination pair again.
    (A bare "save build named X" is deliberately NOT treated as unambiguous
    fast-track wording -- see the destination-extraction rule in
    SYSTEM_PROMPT intent 7 and spec.md §6.7 -- so this test uses a
    qualified phrase that the prompt's own rule does commit to.)"""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved as 'Ultra Rig'! Would you like to publish it to the Community as well?",
        "action": {
            "type": "save_build",
            "name": "Ultra Rig",
            "destination": "build",
            "explanation": "Fast-track: name and destination given up front.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save this as a final build named Ultra Rig",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["destination"] == "build"
    assert result["action"].get("publish_immediately") in (False, None)
    assert "publish" in result["reply"].lower()


def test_save_build_fast_track_first_message_with_publish_intent_fires_both_in_one_turn(monkeypatch):
    """"save build as X and publish to community" gives ALL the information
    needed for both actions in one message -- the model returns save_build
    with publish_immediately: true, and the caller applies both writes in
    the SAME turn with no second round-trip."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved 'Workstation' and published it to the Community!",
        "action": {
            "type": "save_build",
            "name": "Workstation",
            "destination": "build",
            "publish_immediately": True,
            "author_notes": None,
            "explanation": "Fast-track: name, destination, and publish intent all given up front.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save build as workstation and publish to community",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["destination"] == "build"
    assert result["action"]["publish_immediately"] is True
    assert "published" in result["reply"].lower()


def test_publish_confirmation_no_answer_stays_action_null(monkeypatch):
    """A "no" answering the model's own immediately-prior publish question
    must resolve to a plain reply confirming the build stays private, with
    no action — this is prompt-reasoning behavior, so this test only checks
    the plumbing accepts that shape cleanly."""
    _set_env(monkeypatch)
    history = [
        {"role": "assistant", "content": "Saved your build! Would you like to publish it to the Community as well?"},
    ]
    payload = {"reply": "No problem — your build stays private.", "action": None}
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "No", history, CATALOG_SUMMARY, COMMUNITY_SUMMARY, current_build_context=CURRENT_BUILD_CONTEXT
    )
    assert result["source"] == "llm"
    assert result["action"] is None


def test_publish_confirmation_yes_then_description_no_returns_publish_action(monkeypatch):
    """A "no" answering the model's own "want to add a description?" question
    returns the publish_build action with author_notes=None."""
    _set_env(monkeypatch)
    history = [
        {"role": "user", "content": "Save this PC to my list"},
        {"role": "assistant", "content": "Saved! Would you like to publish it to the Community as well?"},
        {"role": "user", "content": "Yes"},
        {
            "role": "assistant",
            "content": "Would you like to include an introductory description or notes for the community?",
        },
    ]
    payload = {
        "reply": "Published to the Community without a description.",
        "action": {"type": "publish_build", "author_notes": None},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "No", history, CATALOG_SUMMARY, COMMUNITY_SUMMARY, current_build_context=CURRENT_BUILD_CONTEXT
    )

    assert result["source"] == "llm"
    assert result["action"] == {"type": "publish_build", "author_notes": None}


def test_publish_confirmation_with_description_text_returns_publish_action_with_notes(monkeypatch):
    """The description text itself (sent after the model asked for it) comes
    back verbatim as author_notes on the publish_build action."""
    _set_env(monkeypatch)
    history = [
        {"role": "assistant", "content": "Saved! Would you like to publish it to the Community as well?"},
        {"role": "user", "content": "Yes"},
        {
            "role": "assistant",
            "content": "Would you like to include an introductory description or notes for the community?",
        },
        {"role": "user", "content": "Yes"},
        {"role": "assistant", "content": "Go ahead and send the description text."},
    ]
    payload = {
        "reply": "Published with your description!",
        "action": {"type": "publish_build", "author_notes": "Great value 1440p gaming rig."},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Great value 1440p gaming rig.",
        history,
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] == {
        "type": "publish_build",
        "author_notes": "Great value 1440p gaming rig.",
    }


def test_publish_confirmation_with_ai_generated_description_request_returns_composed_notes(monkeypatch):
    """New branch (intent 7): on the turn where the model asked the user to
    send the actual description text, the user can instead ask the model to
    COMPOSE it itself (e.g. "generate one for me"). This is a plumbing-level
    test -- it doesn't judge the LLM's composition quality, only that a
    well-formed publish_build action carrying a plausible, model-composed
    author_notes string passes through cleanly, exactly like the pre-existing
    verbatim-text path below."""
    _set_env(monkeypatch)
    history = [
        {"role": "assistant", "content": "Saved! Would you like to publish it to the Community as well?"},
        {"role": "user", "content": "Yes"},
        {
            "role": "assistant",
            "content": "Would you like to include an introductory description or notes for the community?",
        },
        {"role": "user", "content": "Yes"},
        {"role": "assistant", "content": "Go ahead and send the description text."},
    ]
    payload = {
        "reply": "Published! I wrote a description for you based on your build.",
        "action": {
            "type": "publish_build",
            "author_notes": "Built around a Ryzen 7 5800X3D and RTX 4070 for smooth 1440p gaming.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Generate one for me",
        history,
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
        advisory_context=ADVISORY_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] == {
        "type": "publish_build",
        "author_notes": "Built around a Ryzen 7 5800X3D and RTX 4070 for smooth 1440p gaming.",
    }


def test_publish_confirmation_with_description_text_still_returns_verbatim_notes(monkeypatch):
    """Regression guard for the pre-existing verbatim-user-text path (see
    test_publish_confirmation_with_description_text_returns_publish_action_with_notes
    above, which already covers this): confirms adding the new AI-generated-
    description branch above did not disturb the ordinary case where the user
    supplies their own literal description text."""
    _set_env(monkeypatch)
    history = [
        {"role": "assistant", "content": "Saved! Would you like to publish it to the Community as well?"},
        {"role": "user", "content": "Yes"},
        {
            "role": "assistant",
            "content": "Would you like to include an introductory description or notes for the community?",
        },
        {"role": "user", "content": "Yes"},
        {"role": "assistant", "content": "Go ahead and send the description text."},
    ]
    payload = {
        "reply": "Published with your description!",
        "action": {"type": "publish_build", "author_notes": "My own hand-written description."},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "My own hand-written description.",
        history,
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"] == {
        "type": "publish_build",
        "author_notes": "My own hand-written description.",
    }


def test_save_build_action_requires_no_extra_validation_and_passes_validate_action(monkeypatch):
    """_validate_action treats save_build/publish_build as a pass-through
    (no catalog ids/categories to cross-check) — confirm a well-formed
    save_build action (with the now-required name/destination fields) is
    never rejected as a hallucination."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved as 'My Rig'! Publish it too?",
        "action": {
            "type": "save_build",
            "name": "My Rig",
            "destination": "build",
            "explanation": "Saving now.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Save this build", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY, current_build_context=CURRENT_BUILD_CONTEXT
    )
    assert result["source"] == "llm"
    assert result["action"]["type"] == "save_build"
    assert result["action"]["name"] == "My Rig"
    assert result["action"]["destination"] == "build"


# ---------------------------------------------------------------------------
# Intent 8: deep-link to a specific community build (open_community_build)
# ---------------------------------------------------------------------------
def test_concierge_response_parses_open_community_build_action():
    payload = {
        "reply": "Here's your Budget 1440p Gaming Rig.",
        "action": {"type": "open_community_build", "post_id": 1},
    }
    response = ConciergeResponse.model_validate(payload)
    assert response.action is not None
    assert response.action.type == "open_community_build"
    assert response.action.post_id == 1


def test_open_community_build_action_with_real_post_id_passes_through(monkeypatch):
    """A post_id that genuinely exists in the community_summary given for
    this call must pass through unmodified — no re-lookup, no coercion."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Here's your Video Editing Powerhouse.",
        "action": {"type": "open_community_build", "post_id": 2},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Open my Video Editing Powerhouse from community", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )

    assert result["source"] == "llm"
    assert result["action"] == {"type": "open_community_build", "post_id": 2}


def test_open_community_build_action_with_unknown_post_id_falls_back_to_heuristic(monkeypatch):
    """A post_id NOT present in community_summary's real "post_id" values —
    e.g. the model confused a build_id for a post_id, or simply invented one
    — must be rejected by _validate_action's cross-check and fall back to
    the heuristic, the same "never trust the LLM's stated id" precedent as
    load_build/modify_build's catalog-id guard."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Here's your build.",
        # 999 is not a real post_id in COMMUNITY_SUMMARY (nor is it even one
        # of the real build_id values, 1/2 — deliberately a value absent from
        # both fields so this can't accidentally pass via the wrong field).
        "action": {"type": "open_community_build", "post_id": 999},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Open build 999", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_open_community_build_action_using_build_id_in_place_of_post_id_falls_back_to_heuristic(monkeypatch):
    """Guards specifically against the "build_id used where post_id belongs"
    mistake this schema's docstring warns about: in COMMUNITY_SUMMARY, every
    post's post_id happens to equal its build_id (1/1, 2/2), so this test
    alone wouldn't catch a mixup — it instead directly exercises
    _validate_action with a community_summary where the two diverge, proving
    the guard checks "post_id" specifically, not just "any id belonging to
    some post"."""
    from llm.schemas import ConciergeResponse as _Resp

    diverging_summary = [
        {"post_id": 10, "build_id": 55, "title": "Some Build"},
    ]
    response = _Resp.model_validate(
        {
            "reply": "Here's your build.",
            # 55 is the build_id, not the post_id, for the only post given.
            "action": {"type": "open_community_build", "post_id": 55},
        }
    )
    with pytest.raises(concierge.ConciergeUnavailableError):
        concierge._validate_action(response, CATALOG_SUMMARY, diverging_summary)


def test_open_community_build_action_no_match_says_so_and_returns_null_action(monkeypatch):
    """When nothing in community_summary matches what the user described,
    the model is expected to say so plainly and return action: null — this
    only confirms the plumbing passes that shape through cleanly."""
    _set_env(monkeypatch)
    payload = {
        "reply": "I couldn't find a community build matching that description.",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "Open my Nonexistent Rig from community", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY
    )
    assert result["source"] == "llm"
    assert result["action"] is None


# ---------------------------------------------------------------------------
# Intent 9: load an existing draft/build/community post into the studio
# (load_saved_build) — distinct from load_build (a brand new build) and from
# open_community_build (a read-only thread view).
# ---------------------------------------------------------------------------
def test_concierge_response_parses_load_saved_build_action():
    for source in ("draft", "build", "community"):
        payload = {
            "reply": "Loaded it into the Build Studio for editing.",
            "action": {"type": "load_saved_build", "source": source, "id": 1},
        }
        response = ConciergeResponse.model_validate(payload)
        assert response.action is not None
        assert response.action.type == "load_saved_build"
        assert response.action.source == source
        assert response.action.id == 1


def test_load_saved_build_action_requires_source_field():
    payload = {"reply": "Loading it.", "action": {"type": "load_saved_build", "id": 1}}
    with pytest.raises(PydanticValidationError):
        ConciergeResponse.model_validate(payload)


def test_load_saved_build_action_rejects_invalid_source_value():
    """`source` is a closed Literal — a value outside draft/build/community
    must fail validation, the same precedent as every other closed Literal
    in this module (navigate_to, filters.build_type, save_build.destination)."""
    payload = {
        "reply": "Loading it.",
        "action": {"type": "load_saved_build", "source": "my_builds", "id": 1},
    }
    with pytest.raises(PydanticValidationError):
        ConciergeResponse.model_validate(payload)


def test_load_saved_build_draft_with_real_draft_id_passes_through(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded 'pc-master-race' into the Build Studio for editing.",
        "action": {"type": "load_saved_build", "source": "draft", "id": 101},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "open pc-master-race for editing",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
        current_page="drafts",
    )
    assert result["source"] == "llm"
    assert result["action"] == {"type": "load_saved_build", "source": "draft", "id": 101}


def test_load_saved_build_draft_with_unknown_draft_id_falls_back_to_heuristic(monkeypatch):
    """Zero-hallucination guard: a draft_id not present in drafts_summary
    must be rejected, never trusted — same precedent as every other id
    cross-check in this module."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded it.",
        "action": {"type": "load_saved_build", "source": "draft", "id": 9999},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "open my draft called Nonexistent",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_load_saved_build_build_with_real_build_id_passes_through(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded 'Ultra Rig' into the Build Studio for editing.",
        "action": {"type": "load_saved_build", "source": "build", "id": 201},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save build named Ultra Rig",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
        current_page="my_builds",
    )
    assert result["source"] == "llm"
    assert result["action"] == {"type": "load_saved_build", "source": "build", "id": 201}


def test_load_saved_build_build_with_unknown_build_id_falls_back_to_heuristic(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded it.",
        "action": {"type": "load_saved_build", "source": "build", "id": 9999},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "load my build called Nonexistent",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_load_saved_build_community_with_real_post_id_passes_through(monkeypatch):
    """`source == "community"` is cross-checked against community_summary's
    own `post_id` field — the exact same field open_community_build uses,
    confirmed here to also gate this action correctly."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded 'Budget 1440p Gaming Rig' into the Build Studio for editing.",
        "action": {"type": "load_saved_build", "source": "community", "id": 1},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "edit this build",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
        current_page="community",
        viewed_post_id=1,
    )
    assert result["source"] == "llm"
    assert result["action"] == {"type": "load_saved_build", "source": "community", "id": 1}


def test_load_saved_build_community_with_unknown_post_id_falls_back_to_heuristic(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded it.",
        "action": {"type": "load_saved_build", "source": "community", "id": 9999},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "edit the nonexistent community build",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
    )
    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_load_saved_build_no_match_says_so_and_returns_null_action(monkeypatch):
    _set_env(monkeypatch)
    payload = {
        "reply": "I couldn't identify the build to load. Please specify the exact name of the build.",
        "action": None,
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "load my draft",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        drafts_summary=DRAFTS_SUMMARY,
        previous_builds_summary=PREVIOUS_BUILDS_SUMMARY,
    )
    assert result["source"] == "llm"
    assert result["action"] is None
    assert "couldn't identify" in result["reply"].lower()


def test_load_saved_build_with_empty_summaries_still_validates_safely(monkeypatch):
    """drafts_summary/previous_builds_summary default to None -> [] inside
    _build_payload -- confirm a load_saved_build action arriving with no
    summaries at all (the caller passed neither) is correctly rejected as
    unverifiable, never crashes with a missing-argument error."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Loaded it.",
        "action": {"type": "load_saved_build", "source": "draft", "id": 101},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response("open pc-master-race for editing", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)
    assert result["source"] == "heuristic"
    assert result["action"] is None


# ---------------------------------------------------------------------------
# save_build with source == "community" (save/clone a build viewed on the
# Community page directly, without going through the Studio)
# ---------------------------------------------------------------------------
def test_save_build_community_source_with_real_post_id_passes_through(monkeypatch):
    """A save_build action sourced from a real, currently-shared community
    post (post_id 1, present in COMMUNITY_SUMMARY) must pass through
    unchanged -- current_build_context is deliberately omitted/empty here to
    confirm the community source doesn't need an active Studio build at
    all."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved 'Budget Clone' to your drafts.",
        "action": {
            "type": "save_build",
            "name": "Budget Clone",
            "destination": "draft",
            "source": "community",
            "source_post_id": 1,
            "explanation": "Cloning the community build the user is viewing.",
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save the build I'm looking at to my drafts, call it Budget Clone",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_page="community",
        viewed_post_id=1,
    )

    assert result["source"] == "llm"
    assert result["action"]["source"] == "community"
    assert result["action"]["source_post_id"] == 1


def test_save_build_community_source_with_unknown_post_id_falls_back_to_heuristic(monkeypatch):
    """A save_build action claiming source_post_id 9999 (not in
    COMMUNITY_SUMMARY) must be rejected by the zero-hallucination guard, the
    same as open_community_build/load_saved_build treat an invalid id."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved it.",
        "action": {
            "type": "save_build",
            "name": "Budget Clone",
            "destination": "draft",
            "source": "community",
            "source_post_id": 9999,
        },
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save the build I'm looking at to my drafts, call it Budget Clone",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_page="community",
        viewed_post_id=1,
    )

    assert result["source"] == "heuristic"
    assert result["action"] is None


def test_save_build_studio_source_is_still_the_default(monkeypatch):
    """A save_build action with no `source` field at all (every pre-existing
    test/behavior) must still default to "studio" and pass through exactly
    like before this refinement existed -- regression guard."""
    _set_env(monkeypatch)
    payload = {
        "reply": "Saved 'Weekend Rig' as a draft.",
        "action": {"type": "save_build", "name": "Weekend Rig", "destination": "draft"},
    }
    monkeypatch.setattr(concierge.httpx, "post", lambda *a, **k: _fake_openrouter_response(payload))

    result = concierge.get_concierge_response(
        "save this as draft named Weekend Rig",
        [],
        CATALOG_SUMMARY,
        COMMUNITY_SUMMARY,
        current_build_context=CURRENT_BUILD_CONTEXT,
    )

    assert result["source"] == "llm"
    assert result["action"]["source"] == "studio"
    assert result["action"]["source_post_id"] is None


# ---------------------------------------------------------------------------
# Unexpected failures: full traceback logged, never crashes
# ---------------------------------------------------------------------------
def test_unexpected_exception_falls_back_and_logs_traceback(monkeypatch, capsys):
    _set_env(monkeypatch)

    def boom(payload):
        raise RuntimeError("totally unanticipated bug")

    monkeypatch.setattr(concierge, "_call_openrouter", boom)

    result = concierge.get_concierge_response("Hello", [], CATALOG_SUMMARY, COMMUNITY_SUMMARY)

    assert result["source"] == "heuristic"
    assert result["action"] is None

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    assert "Traceback" in combined_output
    assert "totally unanticipated bug" in combined_output
