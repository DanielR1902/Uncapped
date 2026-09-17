"""OpenRouter-backed Concierge chat: answers catalog questions, recommends
shared community builds, and (on a "build me a PC" style request) resolves
named parts to real catalog ids and returns a machine-executable
`load_build` action. get_concierge_response() is the only entry point callers
should use — it never raises for an expected failure mode; it returns a
heuristic dict (source="heuristic") instead. Mirrors llm/advisory.py's
never-raise, source-tagged, single-shot-request pattern exactly (one system
prompt + one payload + one `/chat/completions` call, no real multi-turn
tool-calling loop — this project has never used OpenRouter's function-calling
API and doesn't need it here either). See llm/CLAUDE.md.

This module never queries the database or `engine/` itself: `catalog_summary`
and `community_summary` are pre-fetched by the CALLER (always in `ui/`, which
is the only layer allowed to call `db.repositories.*` directly) and handed in
as plain arguments — exactly like `llm/advisory.py` receives `build_state`
from its caller rather than querying anything itself. Compatibility is never
computed or overridden here either: a `load_build` action names real catalog
ids, and it is the CALLER's job (via `engine.compatibility`) to run a real
compatibility pass over them after this returns, the same "engine is the only
source of pass/fail" rule that governs every other LLM feature in this
package (see llm/CLAUDE.md's "Responsibility" section).
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

import httpx
from pydantic import ValidationError as PydanticValidationError

from llm.schemas import ConciergeResponse

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT_SECONDS = 15.0

# The 8 core build categories a "build me a PC" request should end up filling
# in (peripherals like NetworkCard/SoundCard/OpticalDrive are optional
# add-ons, not part of a base build) — used only to describe the task to the
# model in the prompt; validation itself only ever checks against the real
# `catalog_summary` the caller supplied, never this list.
CORE_CATEGORIES = ("CPU", "Motherboard", "GPU", "RAM", "Storage", "PSU", "Case", "Cooler")

_HEURISTIC_REPLY = (
    "I'm having trouble reaching the AI assistant right now. Try browsing the catalog or "
    "community pages directly, or ask again in a moment."
)


class ConciergeUnavailableError(Exception):
    """Internal signal for any failure mode that should fall back to the
    heuristic reply: missing API key/model config, timeout, connection error,
    non-2xx/429 HTTP response, a response that fails schema validation, or a
    `load_build` action that references a component id/category not present
    in the `catalog_summary` it was given. get_concierge_response() always
    catches this — it never escapes to callers of get_concierge_response()
    itself."""


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ConciergeUnavailableError("OPENROUTER_API_KEY is not set")
    return key


def _model() -> str:
    model = os.getenv("OPENROUTER_MODEL")
    if not model:
        raise ConciergeUnavailableError("OPENROUTER_MODEL is not set")
    return model


SYSTEM_PROMPT = """You are the Uncapped Concierge — a friendly, knowledgeable hardware assistant embedded
in Uncapped, an AI-assisted PC configuration platform.

CONCISENESS RULE (applies to every "reply" you write, regardless of intent): keep all conversational
replies to AT MOST 2 sentences. State plainly what was done or found, then stop — never describe every
part in a long paragraph.

You are given the user's message, the recent conversation history, the FULL component catalog
(`catalog_summary` — every part currently available, with its real id/category/name/price and key specs),
every currently-shared community build (`community_summary` — real posts with their title, creation
mode, workload profile/tier, total cost, and author notes), and the user's CURRENTLY ACTIVE build draft
(`current_build_context` — `{"mode": "Free"|"Budget"|"Workload"|None, "components": {category: {"id", "name",
"price_usd"}, ...} (only categories already filled in), "quantities": {"RAM"|"Storage": int} (only when >1)}`,
or `None`/empty when there is no build in progress). Ground every factual claim ONLY in this data.
NEVER invent a part name, price, spec, or community build that isn't literally present in what you were
given — if you don't have it, say so plainly instead of guessing.

NEVER format prices or monetary amounts using a standalone dollar sign like "$600" or "$140" — two
dollar-prefixed amounts in the same response create a matching pair of "$" delimiters, and Streamlit's
markdown renderer treats text between a matching "$" pair as inline LaTeX/math, garbling plain prices
into italic math notation. Always write amounts as "600 USD" or "USD 600" instead.

You handle five kinds of requests:

1. CATALOG QUESTIONS (e.g. "What CPUs do you have?", "What's your cheapest GPU?") — answer using
   `catalog_summary` only, citing exact real part names and prices. Return `action: null`.

2. COMMUNITY RECOMMENDATIONS (e.g. "recommend a gaming build under 2000 USD") — answer using
   `community_summary` only: filter/reason over the given posts (e.g. by `workload_profile`,
   `workload_tier`, `total_cost`) and name real post titles, exact prices, and why each one fits the
   request. Return `action: null`.

3. BUILD-ME REQUESTS (e.g. "Build me a PC with an RTX 4070 and a Ryzen 5800X3D") — for every part the
   user named, resolve it to a real entry in `catalog_summary` using fuzzy/substring, case-insensitive
   matching on `name` (e.g. "RTX 4070" should match a catalog entry whose name contains "RTX 4070"). Fill
   in every REMAINING unmentioned core category — CPU, Motherboard, GPU, RAM, Storage, PSU, Case, Cooler —
   with a sensible, real, reasonably-compatible-looking pick from `catalog_summary` (you do not need to run
   exhaustive compatibility rules yourself — a deterministic compatibility engine re-checks everything you
   propose after you respond). Return a `load_build` action whose `components` field maps EVERY category
   you filled in (named + inferred) to that category's real catalog `id`, plus a short `explanation` of
   what you picked and why. If you cannot find a named part anywhere in `catalog_summary` (it genuinely
   doesn't exist in the catalog), say so plainly in `reply` and return `action: null` — never invent a
   placeholder id for a part that isn't real.

4. INCREMENTAL MODIFICATION REQUESTS (e.g. "add a network card and optical drive", "bump my storage to 2",
   "add another 2TB drive") — when `current_build_context` shows an ACTIVE build already in progress and the
   user is asking to ADD to or ADJUST it rather than start over, return a `modify_build` action instead of
   `load_build`. `components` should include ONLY the categories being newly added or changed (e.g.
   {"NetworkCard": 87, "OpticalDrive": 42}) — never repeat unchanged categories from `current_build_context`,
   since this is a PATCH, not a replacement. For "add another N of the current [RAM/Storage]" style requests,
   set `quantities` to the TOTAL desired count for that category (e.g. current quantity 1, "add another" ->
   `quantities: {"Storage": 2}`, not a delta) — you do not need to verify this fits real motherboard slots or
   the budget; a deterministic check re-validates it after you respond, exactly like `load_build`'s components
   are re-checked by a compatibility engine. If `current_build_context` is `None`/empty (no active build), a
   "modify my build" request has nothing to modify — say so plainly in `reply` and return `action: null`,
   don't invent a `modify_build` against nothing.

5. PURE NAVIGATION REQUESTS (e.g. "take me to Community", "show my previous builds", "go to the build
   studio") with NO build-related content — return a `navigate` action instead of a reply-only answer:
   `{"reply": "...", "action": {"type": "navigate", "navigate_to": "community"|"my_builds"|"create_build"}}`.
   Use "my_builds" for "previous builds"/"my builds", "create_build" for "build studio"/"start a new build",
   and "community" for "community"/"shared builds". Do not combine a `navigate` action with any build
   mutation — a request that both asks to go somewhere AND asks to change the build should be treated as
   whichever the user actually wants acted on now.

ZERO-HALLUCINATION RULE for `load_build`/`modify_build` actions: every `(category, id)` pair in `components`
MUST correspond exactly to a real entry in `catalog_summary` with that same `category` and that same `id`.
Never make up an id, and never assign a real id to the wrong category. For `modify_build`, every key in
`quantities` MUST be `"RAM"` or `"Storage"` — never any other category.

Respond with STRICT JSON and nothing else (no prose, no markdown fences), matching exactly one of these
shapes:
{
  "reply": "<conversational answer to the user, grounded in the data given>",
  "action": null
}
or, for a build-me request that resolved successfully:
{
  "reply": "<short summary of what you built and why>",
  "action": {
    "type": "load_build",
    "components": {"CPU": 12, "Motherboard": 7, "GPU": 41, "RAM": 9, "Storage": 22, "PSU": 3, "Case": 15, "Cooler": 6},
    "explanation": "<short explanation of the picks>"
  }
}
or, for an incremental modification to the active build draft:
{
  "reply": "<short summary of what was added/changed>",
  "action": {
    "type": "modify_build",
    "components": {"NetworkCard": 87, "OpticalDrive": 42},
    "quantities": {"Storage": 2},
    "explanation": "<short explanation of the patch>"
  }
}
or, for a pure navigation request:
{
  "reply": "<short acknowledgement of where you're taking the user>",
  "action": {"type": "navigate", "navigate_to": "community"}
}

"reply" is always required. "action" is required to be present but may be `null` — omit it entirely only
never; always include the key, set to `null` when there is no action to return.
"""


def _build_payload(
    user_message: str,
    conversation_history: list[dict],
    catalog_summary: list[dict],
    community_summary: list[dict],
    current_build_context: dict | None = None,
) -> dict:
    return {
        "user_message": user_message,
        "conversation_history": conversation_history,
        "catalog_summary": catalog_summary,
        "community_summary": community_summary,
        "current_build_context": current_build_context or {},
    }


def _messages(payload: dict) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload)},
    ]


def _call_openrouter(payload: dict) -> dict[str, Any]:
    try:
        response = httpx.post(
            OPENROUTER_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:8501",
                "X-Title": "Uncapped Studio",
            },
            json={
                "model": _model(),
                "messages": _messages(payload),
                "response_format": {"type": "json_object"},
            },
        )
    except httpx.TimeoutException as exc:
        raise ConciergeUnavailableError("OpenRouter request timed out") from exc
    except httpx.HTTPError as exc:
        raise ConciergeUnavailableError(f"OpenRouter request failed: {exc}") from exc

    if response.status_code == 429:
        print(f"OpenRouter rate-limited the concierge request: {response.text}", file=sys.stderr)
        raise ConciergeUnavailableError("OpenRouter rate-limited the request")
    if response.status_code >= 400:
        print(f"OpenRouter returned HTTP {response.status_code}: {response.text}", file=sys.stderr)
        raise ConciergeUnavailableError(f"OpenRouter returned HTTP {response.status_code}")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ConciergeUnavailableError(f"Malformed OpenRouter response: {exc}") from exc


def _heuristic_response() -> dict:
    """Network-free fallback used on ANY failure above. Deliberately not
    clever: there is no deterministic non-LLM way to "answer a hardware
    question" the way engine/scoring.py gives advisory.py a real heuristic to
    fall back on, so the honest thing to do is admit degraded service rather
    than fake competence."""
    return {"reply": _HEURISTIC_REPLY, "action": None, "source": "heuristic"}


_QUANTITY_ELIGIBLE_CATEGORIES = frozenset({"RAM", "Storage"})


def _validate_action(response: ConciergeResponse, catalog_summary: list[dict]) -> None:
    """Post-parse hallucination/shape guard (authoritative — the SYSTEM_PROMPT
    rule is just a request, this is what actually enforces it), branching on
    the action's type:

    - `load_build`/`modify_build`: every (category, id) pair in `components`
      must correspond to a REAL entry in `catalog_summary` with that same
      category and that same id.
    - `modify_build` additionally: every key in `quantities` must be "RAM" or
      "Storage" — the only two categories where a quantity is meaningful.
    - `navigate`: nothing extra to check here — Pydantic's `Literal` on
      `navigate_to` already rejected an invalid page key at parse time.

    Raises ConciergeUnavailableError on any violation so
    get_concierge_response's existing except block funnels it into the same
    heuristic fallback as any other failure mode."""
    action = response.action
    if action is None or action.type == "navigate":
        return

    valid_ids_by_category: dict[str, set[int]] = {}
    for entry in catalog_summary:
        category = entry.get("category")
        component_id = entry.get("id")
        if category is None or component_id is None:
            continue
        valid_ids_by_category.setdefault(category, set()).add(component_id)

    for category, component_id in action.components.items():
        if component_id not in valid_ids_by_category.get(category, set()):
            raise ConciergeUnavailableError(
                f"Concierge {action.type} action references a non-catalog id {component_id} for "
                f"category {category!r}"
            )

    if action.type == "modify_build":
        for category in action.quantities:
            if category not in _QUANTITY_ELIGIBLE_CATEGORIES:
                raise ConciergeUnavailableError(
                    f"Concierge modify_build action references a quantity for non-quantity-eligible "
                    f"category {category!r}"
                )


def get_concierge_response(
    user_message: str,
    conversation_history: list[dict],
    catalog_summary: list[dict],
    community_summary: list[dict],
    current_build_context: dict | None = None,
) -> dict:
    """Public entry point. `conversation_history` is
    `[{"role": "user"|"assistant", "content": str}, ...]` with the most recent
    turn last (NOT including `user_message` itself, which is the new incoming
    message). `catalog_summary`/`community_summary` are pre-fetched by the
    caller (`ui/`) — this module never queries the database or `engine/`
    itself. `current_build_context` is an optional, caller-assembled snapshot
    of the user's currently active build draft (`{"mode", "components",
    "quantities"}`) used to support incremental `modify_build` requests; pass
    `None` (the default) when there is no active draft.

    Never raises. Returns
    {"reply": str,
     "action": {"type": "load_build", "components": {category: component_id}, "explanation": str}
              | {"type": "modify_build", "components": {category: component_id}, "quantities": {category: int}, "explanation": str}
              | {"type": "navigate", "navigate_to": "create_build" | "my_builds" | "community"}
              | None,
     "source": "llm" | "heuristic"}.
    """
    try:
        payload = _build_payload(
            user_message, conversation_history, catalog_summary, community_summary, current_build_context
        )
        raw = _call_openrouter(payload)
        response = ConciergeResponse.model_validate(raw)
        _validate_action(response, catalog_summary)
    except (ConciergeUnavailableError, PydanticValidationError):
        return _heuristic_response()
    except Exception:
        # A genuinely UNEXPECTED failure (a real bug, not one of the
        # anticipated failure modes above) — log the full traceback so it's
        # never silently invisible to whoever's running the app, but still
        # never crash the user's chat session.
        traceback.print_exc(file=sys.stderr)
        return _heuristic_response()

    response.source = "llm"
    return response.model_dump()
