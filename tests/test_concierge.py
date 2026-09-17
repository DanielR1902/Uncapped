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
@pytest.mark.parametrize("page_key", ["create_build", "my_builds", "community"])
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
    assert result["action"] == {"type": "navigate", "navigate_to": page_key}


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
