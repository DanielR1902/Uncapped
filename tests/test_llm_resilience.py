"""Streaming/response resilience for llm/concierge.py and llm/client.py:
retry on malformed/truncated bodies, finish_reason == "length", the
conservative JSON repair helper, and the "repair never bypasses validation"
guarantee. HTTP is always mocked (no live OpenRouter)."""
from __future__ import annotations

import json

import httpx
import pytest

from llm import _json_utils
from llm import client as llm_client
from llm import concierge

CATALOG = [
    {"id": 1, "category": "CPU", "name": "Ryzen 7 5800X3D", "price_usd": 329.0},
    {"id": 11, "category": "NetworkCard", "name": "TP-Link Archer", "price_usd": 39.0},
]
GOOD = {"reply": "Hello there.", "action": None}


def _resp(content, finish_reason=None, status=200):
    choice = {"message": {"content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return httpx.Response(
        status, json={"choices": [choice]}, request=httpx.Request("POST", concierge.OPENROUTER_URL)
    )


def _env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")


def _sequence(monkeypatch, responses, bodies=None):
    calls = {"n": 0}

    def fake_post(url, timeout=None, headers=None, json=None):
        if bodies is not None:
            bodies.append(json)
        response = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return response

    monkeypatch.setattr(concierge.httpx, "post", fake_post)
    return calls


# ---------------------------------------------------------------------------
# _json_utils.repair_json
# ---------------------------------------------------------------------------
def test_repair_closes_unterminated_reply_string():
    repaired = _json_utils.repair_json('{\n  "reply": "Sure, adding that for', {"reply"})
    assert repaired == {"reply": "Sure, adding that for"}


def test_repair_strips_fences_and_trailing_garbage():
    assert _json_utils.repair_json('```json\n{"a": 1}\n``` thanks') == {"a": 1}


def test_repair_balances_open_containers_after_complete_value():
    assert _json_utils.repair_json('{"reply": "x", "action": {"type": "navigate", "n": [1, 2') is None
    assert _json_utils.repair_json('{"reply": "x", "action": null, "tags": ["a", "b"') == {
        "reply": "x", "action": None, "tags": ["a", "b"],
    }


@pytest.mark.parametrize(
    "text",
    ['{"reply": "x", "act', '{"reply": "x", "n":', '{"reply": "x", "n": 12', '{"reply": "x", "b": tr', "no json"],
)
def test_repair_refuses_unsafe_truncations(text):
    assert _json_utils.repair_json(text) is None


def test_repair_refuses_to_close_string_under_unsafe_key():
    assert _json_utils.repair_json('{"reply": "ok", "action": {"type": "navigate", "name": "abc', {"reply"}) is None


# ---------------------------------------------------------------------------
# concierge
# ---------------------------------------------------------------------------
def test_request_payload_sets_generous_max_tokens(monkeypatch):
    _env(monkeypatch)
    bodies: list = []
    _sequence(monkeypatch, [_resp(json.dumps(GOOD))], bodies)
    concierge.get_concierge_response("hi", [], CATALOG, [])
    assert bodies[0]["max_tokens"] >= 2048


def test_truncated_json_retries_then_succeeds(monkeypatch):
    _env(monkeypatch)
    truncated = '{\n  "reply": "Hello th'  # "Unterminated string" flavour
    calls = _sequence(monkeypatch, [_resp(truncated), _resp(json.dumps(GOOD))])
    result = concierge.get_concierge_response("hi", [], CATALOG, [])
    assert calls["n"] == 2
    assert result["source"] == "llm"
    assert result["reply"] == "Hello there."


def test_finish_reason_length_triggers_retry(monkeypatch):
    _env(monkeypatch)
    calls = _sequence(monkeypatch, [_resp(json.dumps(GOOD), finish_reason="length"), _resp(json.dumps(GOOD), "stop")])
    result = concierge.get_concierge_response("hi", [], CATALOG, [])
    assert calls["n"] == 2
    assert result["source"] == "llm"


def test_repair_success_after_retries_exhausted(monkeypatch):
    _env(monkeypatch)
    truncated = '{"reply": "Partial but usable reply'
    calls = _sequence(monkeypatch, [_resp(truncated)])
    result = concierge.get_concierge_response("hi", [], CATALOG, [])
    assert calls["n"] == 1 + len(concierge.MALFORMED_RETRY_DELAYS)
    assert result["source"] == "llm"
    assert result["reply"] == "Partial but usable reply"
    assert result["action"] is None


def test_repair_rejected_by_schema_falls_back_to_heuristic(monkeypatch):
    _env(monkeypatch)
    # Repairable JSON, but missing the required "reply" field -> pydantic rejects it.
    calls = _sequence(monkeypatch, [_resp('{"action": null, "explanation": "cut off mid')])
    result = concierge.get_concierge_response("hi", [], CATALOG, [])
    assert calls["n"] == 3
    assert result["source"] == "heuristic"


def test_repair_never_accepts_hallucinated_ids(monkeypatch):
    _env(monkeypatch)
    body = (
        '{"reply": "ok", "action": {"type": "modify_build", "components": {"NetworkCard": 9999}, '
        '"explanation": "cut off'
    )
    _sequence(monkeypatch, [_resp(body)])
    result = concierge.get_concierge_response(
        "add a network card", [], CATALOG, [], current_build_context={"components": {"CPU": {"id": 1}}}
    )
    assert result["source"] == "heuristic"


def test_repair_refuses_truncated_component_map(monkeypatch):
    _env(monkeypatch)
    body = '{"reply": "ok", "action": {"type": "load_build", "components": {"CPU": 1, "NetworkCard": 1'
    _sequence(monkeypatch, [_resp(body)])
    result = concierge.get_concierge_response("build me a pc", [], CATALOG, [])
    assert result["source"] == "heuristic"


def test_non_json_content_retries_and_never_raises(monkeypatch):
    _env(monkeypatch)
    calls = _sequence(monkeypatch, [_resp("total nonsense")])
    result = concierge.get_concierge_response("hi", [], CATALOG, [])
    assert calls["n"] == 3
    assert result["source"] == "heuristic"


# ---------------------------------------------------------------------------
# client.analyze_build path
# ---------------------------------------------------------------------------
def _analysis_call(monkeypatch, responses):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test-model")
    calls = {"n": 0}

    def fake_post(url, timeout=None, headers=None, json=None):
        assert json["max_tokens"] >= 2048
        response = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return response

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    request = object()
    monkeypatch.setattr(llm_client, "build_messages", lambda r: [])
    return llm_client._call_openrouter(request), calls


def _client_resp(content, finish_reason=None):
    choice = {"message": {"content": content}}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    return httpx.Response(200, json={"choices": [choice]}, request=httpx.Request("POST", llm_client.OPENROUTER_URL))


def test_client_retries_truncated_body_then_succeeds(monkeypatch):
    parsed, calls = _analysis_call(monkeypatch, [_client_resp('{"synergy": {"overall_sc'), _client_resp('{"ok": 1}')])
    assert parsed == {"ok": 1}
    assert calls["n"] == 2


def test_client_finish_reason_length_retries(monkeypatch):
    parsed, calls = _analysis_call(monkeypatch, [_client_resp('{"ok": 1}', "length"), _client_resp('{"ok": 2}', "stop")])
    assert parsed == {"ok": 2}
    assert calls["n"] == 2


def test_client_exhausted_retries_raise_internal_error_for_fallback(monkeypatch):
    with pytest.raises(llm_client.LLMUnavailableError):
        _analysis_call(monkeypatch, [_client_resp('{"synergy": {"overall_sc')])
