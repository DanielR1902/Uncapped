"""OpenRouter-backed AI Build Advisory: in-budget optimization tips + a
stretch-budget upgrade suggestion. get_build_advisory() is the only entry
point callers should use — it never raises for an expected failure mode; it
returns a heuristic dict (source="heuristic") instead. Mirrors llm/client.py's
never-raise, source-tagged pattern exactly. See llm/CLAUDE.md.

Compatibility is never re-derived or overridden here: the bottleneck
direction this module reasons about always comes from
engine.scoring.bottleneck_percentage_baseline (or a caller-supplied
bottleneck_info already computed by engine/), and every part named in a
suggestion — LLM or heuristic — is fetched through engine.solvers, which is
the only thing in this package's allowed-import list that legitimately talks
to db.repositories.components_repo. llm/ itself never imports
db.repositories directly (see llm/CLAUDE.md's "Allowed imports").
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
from pydantic import ValidationError as PydanticValidationError

from db.models import Component
from engine import compatibility, scoring, solvers
from engine.compatibility import BuildState
from llm.schemas import BuildAdvisoryResponse

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT_SECONDS = 15.0

# How many cheaper/pricier neighbors (by price) to surface per category when
# grounding the prompt in real catalog data — enough for the model to have
# concrete next-tier-up/down options without dumping the whole catalog.
_NEIGHBORS_PER_DIRECTION = 2


class AdvisoryUnavailableError(Exception):
    """Internal signal for any failure mode that should fall back to the
    heuristic advisory: missing API key/model config, timeout, connection
    error, non-2xx/429 HTTP response, or a response that fails schema
    validation. get_build_advisory() always catches this — it never escapes
    to callers of get_build_advisory() itself."""


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise AdvisoryUnavailableError("OPENROUTER_API_KEY is not set")
    return key


def _model() -> str:
    model = os.getenv("OPENROUTER_MODEL")
    if not model:
        raise AdvisoryUnavailableError("OPENROUTER_MODEL is not set")
    return model


SYSTEM_PROMPT = """You are a PC-hardware advisor for Uncapped, an AI-assisted PC configuration platform.

You are given a build's components, its creation mode and a budget/cost figure, the remaining_budget
figure (only meaningful in Budget mode — see the per-mode objectives below), a deterministic
bottleneck analysis that has ALREADY been computed (never re-derive or contradict it — only reason
on top of it), and a summary of REAL catalog alternatives for each category: the current pick plus a
couple of cheaper and a couple of pricier compatible options with their exact prices. Use only these
real part names and prices — never invent a plausible-sounding part that wasn't listed.

NEVER format prices or monetary amounts using a standalone dollar sign like "$600" or "$140" — two
dollar-prefixed amounts in the same response create a matching pair of "$" delimiters, and Streamlit's
markdown renderer treats text between a matching "$" pair as inline LaTeX/math, garbling plain prices
into italic math notation. Always write amounts as "600 USD" or "USD 600" instead.

MODE-SPECIFIC OBJECTIVES — read the payload's "mode" field and apply the matching objective below;
these change what "within_budget" and "stretch_budget" should optimize for:

- mode == "Budget": Optimize strictly within the payload's "remaining_budget" figure. If
  remaining_budget is 0 or very small (the budget is already maxed out), "within_budget" must ONLY
  propose a cost-neutral swap or a paired downgrade-to-upgrade reallocation (total price delta close
  to zero) — never suggest a plain upgrade that would push total cost past the ceiling.
- mode == "Workload": There is no budget ceiling here — optimize for alignment with the payload's
  "workload_profile" instead (e.g. Gaming, Video Editing, Programming/AI, Design, General).
  "within_budget" swaps must prioritize whichever component category matters most for that profile:
  GPU/VRAM for Gaming/Design/3D-heavy profiles, CPU cores/RAM capacity for Programming/Video
  Editing/multitasking-heavy profiles. "stretch_budget" should propose an upgrade that pushes the
  build toward the NEXT tier's typical requirements for that same profile.
- mode == "Free": There is no budget ceiling here either — optimize for bottleneck mitigation and
  CPU/GPU platform balance. "within_budget" proposes rebalancing: downgrade an over-specced component
  to fund the primary limiting (bottlenecked) one. "stretch_budget" should target the primary
  bottleneck component directly with a real catalog upgrade that improves synergy the most.

STRICT ARITHMETIC / NO-HALLUCINATION RULE: never invent parts or arbitrary prices. Every price delta
you state must exactly equal the difference between two real catalog prices given to you in
catalog_alternatives. If the current configuration is already optimal for its stated constraints (no
beneficial swap exists in the needed direction), say so explicitly rather than inventing one.

Respond with STRICT JSON and nothing else (no prose, no markdown fences), matching exactly this shape:
{
  "pros": ["Concise strength 1", "Concise strength 2"],
  "cons": ["Concise limitation 1", "Concise limitation 2"],
  "within_budget": "Concise optimization advice...",
  "stretch_budget": "Concise next-tier upgrade advice..."
}

"pros": a short list of concrete strengths of the CURRENT build, grounded in the components and
catalog alternatives given (e.g. good CPU/GPU synergy for the stated mode/workload, strong value for
the price tier, no compatibility conflicts) — never invent specs that weren't given.

"cons": a short list of concrete limitations of the CURRENT build, grounded the same way (e.g. a
bottleneck imbalance, an over-provisioned or under-provisioned component, a weak link for the stated
workload).

"within_budget": ONE concise piece of optimization advice following the mode-specific objective above
— downgrading an over-provisioned component (using the catalog alternatives given) to fund a weaker
one, balancing CPU/GPU synergy for the stated mode/workload, or a 1:1 value swap (same price tier,
better fit).

"stretch_budget": ONE concise next-tier upgrade recommendation following the mode-specific objective
above — name an exact part from the catalog alternatives given and its exact cost delta versus the
current pick in that category, in USD-style formatting (e.g. "an additional 35 USD"). In Budget mode
keep this to a modest increase (roughly 20 to 60 USD, or +10% of the current figure, beyond
remaining_budget).

All four keys are required in every response.
"""


def _component_summary(component: Component) -> dict:
    return {"category": component.category, "name": component.name, "price_usd": component.price_usd}


def _catalog_alternatives(build_state: BuildState) -> dict[str, dict]:
    """For each core category currently filled, the current pick plus its
    immediate cheaper/pricier compatible neighbors by price — grounds the
    prompt in real parts instead of letting the model hallucinate plausible-
    sounding fake ones. Peripherals (outside solvers.CATEGORY_ORDER) are
    skipped: they have no deterministic compatibility rules to differentiate
    alternatives by (see engine/solvers.py), so a price-neighbor summary for
    them wouldn't be meaningful."""
    alternatives: dict[str, dict] = {}
    for category, current in build_state.items():
        if category not in solvers.CATEGORY_ORDER:
            continue
        candidates = solvers.get_compatible_candidates(category, build_state)
        peers = [c for c in candidates if c.id != current.id]
        cheaper = sorted(
            (c for c in peers if c.price_usd < current.price_usd), key=lambda c: c.price_usd
        )[-_NEIGHBORS_PER_DIRECTION:]
        pricier = sorted(
            (c for c in peers if c.price_usd > current.price_usd), key=lambda c: c.price_usd
        )[:_NEIGHBORS_PER_DIRECTION]
        alternatives[category] = {
            "current": _component_summary(current),
            "cheaper_alternatives": [_component_summary(c) for c in cheaper],
            "pricier_alternatives": [_component_summary(c) for c in pricier],
        }
    return alternatives


def _resolve_bottleneck(build_state: BuildState, bottleneck_info: dict | None) -> tuple[dict, float, str]:
    """Returns (info_to_send_to_the_prompt, bottleneck_percentage, direction).

    If the caller didn't supply bottleneck_info, compute it fresh via
    engine.scoring. If they did, accept either shape already used elsewhere
    in this codebase: a plain {"bottleneck_percentage", "bottleneck_direction"}
    pair, or a prior llm.client.analyze_build() result's `.bottleneck` field
    (model_dump()'d), which instead carries {"bottleneck_percentage",
    "limiting_component", "resolution_impact"}."""
    if bottleneck_info is None:
        pct, direction = scoring.bottleneck_percentage_baseline(build_state)
        return {"bottleneck_percentage": pct, "bottleneck_direction": direction}, pct, direction

    pct = bottleneck_info.get("bottleneck_percentage")
    if pct is None:
        pct, _ = scoring.bottleneck_percentage_baseline(build_state)

    direction = bottleneck_info.get("bottleneck_direction") or bottleneck_info.get("direction")
    if direction is None:
        limiting_component = bottleneck_info.get("limiting_component")
        direction = "Balanced" if limiting_component in (None, "None") else f"{limiting_component}-bound"

    return bottleneck_info, pct, direction


def _remaining_budget(build_state: BuildState, mode: str, current_budget_or_cost: float) -> float:
    """Budget-mode headroom left under the ceiling; 0.0 in Workload/Free,
    where there is no ceiling concept to begin with."""
    if mode != "Budget":
        return 0.0
    total_cost = sum(component.price_usd for component in build_state.values())
    return max(0.0, current_budget_or_cost - total_cost)


def _build_request_payload(
    build_state: BuildState,
    mode: str,
    current_budget_or_cost: float,
    profile: str | None,
    bottleneck_info: dict,
) -> dict:
    return {
        "mode": mode,
        "current_budget_or_cost": current_budget_or_cost,
        "remaining_budget": _remaining_budget(build_state, mode, current_budget_or_cost),
        "workload_profile": profile,
        "bottleneck": bottleneck_info,
        "components": [_component_summary(component) for component in build_state.values()],
        "catalog_alternatives": _catalog_alternatives(build_state),
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
        raise AdvisoryUnavailableError("OpenRouter request timed out") from exc
    except httpx.HTTPError as exc:
        raise AdvisoryUnavailableError(f"OpenRouter request failed: {exc}") from exc

    if response.status_code == 429:
        print(f"OpenRouter rate-limited the advisory request: {response.text}", file=sys.stderr)
        raise AdvisoryUnavailableError("OpenRouter rate-limited the request")
    if response.status_code >= 400:
        print(f"OpenRouter returned HTTP {response.status_code}: {response.text}", file=sys.stderr)
        raise AdvisoryUnavailableError(f"OpenRouter returned HTTP {response.status_code}")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AdvisoryUnavailableError(f"Malformed OpenRouter response: {exc}") from exc


# ---------------------------------------------------------------------------
# Heuristic fallback (network-free) — used on ANY failure above.
#
# Direction semantics come straight from engine.scoring.bottleneck_percentage_
# baseline: "<X>-bound" means X has the LOWER benchmark_score, i.e. X is the
# weaker/limiting part. The sensible, deterministic swap is therefore to
# downgrade the OTHER (over-provisioned/stronger) component to fund an
# upgrade of the limiting one — the same "trim the strong side, reinforce the
# weak side" logic engine/scoring.py's value_index and the rest of this
# project's balancing philosophy already assume.
# ---------------------------------------------------------------------------
def _heuristic_within_budget(build_state: BuildState, direction: str, mode: str, profile: str | None) -> str:
    if direction == "Balanced":
        base = (
            "Your CPU and GPU are well balanced — there's no bottleneck-driven swap to make; "
            "consider redirecting any spare budget toward storage speed, RAM capacity, or case "
            "airflow instead."
        )
        if mode == "Workload" and profile:
            base += f" This is already a solid balance for a {profile} workload."
        return base

    limiting_category, _, _ = direction.partition("-")  # "CPU-bound" -> "CPU"
    other_category = "GPU" if limiting_category == "CPU" else "CPU"
    other_component = build_state.get(other_category)

    if other_component is None:
        return (
            f"The build is {direction.lower()} — pick a {other_category} to rebalance against before "
            "an in-budget swap can be suggested."
        )

    others_fixed = {c: v for c, v in build_state.items() if c != other_category}
    candidates = solvers.get_compatible_candidates(other_category, others_fixed)
    cheaper = [c for c in candidates if c.id != other_component.id and c.price_usd < other_component.price_usd]

    if not cheaper:
        return (
            f"The build is {direction.lower()}, but {other_component.name} is already the cheapest "
            f"compatible {other_category} option — there's no further downgrade available to fund the "
            f"{limiting_category}."
        )

    downgrade = max(cheaper, key=lambda c: c.price_usd)  # smallest step down that still frees up money
    savings = other_component.price_usd - downgrade.price_usd
    swap = (
        f"The build is {direction.lower()}: downgrade {other_category} from {other_component.name} to "
        f"{downgrade.name} (-{savings:,.2f} USD) and put the savings toward a stronger {limiting_category}"
    )

    if mode == "Budget":
        return f"{swap} — a cost-neutral reallocation that stays within your budget."
    if mode == "Workload":
        if profile:
            return f"{swap} to better suit your {profile} workload."
        return f"{swap} to better suit your stated workload."
    # Free mode: bottleneck mitigation with explicit CPU/GPU platform balance framing.
    return f"{swap} to rebalance the CPU/GPU platform."


def _heuristic_stretch_budget(build_state: BuildState, direction: str, mode: str, profile: str | None) -> str:
    if direction == "Balanced":
        limiting_category = "GPU" if "GPU" in build_state else ("CPU" if "CPU" in build_state else None)
    else:
        limiting_category, _, _ = direction.partition("-")

    if limiting_category is None or limiting_category not in build_state:
        return "Add a CPU and a GPU to the build to get a stretch-budget upgrade recommendation."

    current = build_state[limiting_category]
    others_fixed = {c: v for c, v in build_state.items() if c != limiting_category}
    candidates = solvers.get_compatible_candidates(limiting_category, others_fixed)
    pricier = [c for c in candidates if c.id != current.id and c.price_usd > current.price_usd]

    if not pricier:
        return (
            f"{current.name} is already the priciest compatible {limiting_category} option in the "
            "catalog — no stretch-budget upgrade is available there right now."
        )

    upgrade = min(pricier, key=lambda c: c.price_usd)  # cheapest upgrade that still helps
    delta = upgrade.price_usd - current.price_usd

    if mode == "Workload":
        reason = (
            f"to push this build toward the next tier for a {profile} workload"
            if profile
            else "to push this build toward the next performance tier"
        )
    elif mode == "Free":
        reason = (
            f"to directly target the {direction.lower()} bottleneck and improve platform synergy"
            if direction != "Balanced"
            else "for extra platform headroom"
        )
    else:  # Budget
        reason = f"to ease the {direction.lower()} imbalance" if direction != "Balanced" else "for extra headroom"

    return f"Upgrade {limiting_category} to {upgrade.name} (+{delta:,.2f} USD) {reason}."


def _heuristic_pros(build_state: BuildState, direction: str, profile: str | None = None) -> list[str]:
    report = compatibility.evaluate_build(build_state)
    pros: list[str] = []

    if report.is_compatible:
        pros.append("Every component is fully compatible — no conflicts to resolve.")
    if direction == "Balanced":
        if profile:
            pros.append(f"CPU and GPU are well balanced for a {profile} workload.")
        else:
            pros.append("CPU and GPU are well balanced for this workload.")
    if build_state:
        pros.append(f"The build currently includes {len(build_state)} selected component(s).")

    if not pros:
        pros.append("The build has a valid starting point to optimize from.")
    return pros


def _heuristic_cons(build_state: BuildState, direction: str, profile: str | None = None) -> list[str]:
    report = compatibility.evaluate_build(build_state)
    cons: list[str] = []

    if direction != "Balanced":
        suffix = f" for a {profile} workload" if profile else " in demanding scenarios"
        cons.append(f"The build is {direction.lower()}, which may limit performance{suffix}.")
    if not report.is_compatible:
        cons.extend(report.issues)

    if not cons:
        cons.append("No significant limitations detected in this configuration.")
    return cons


def _heuristic_advisory(build_state: BuildState, direction: str, mode: str, profile: str | None) -> dict:
    return {
        "pros": _heuristic_pros(build_state, direction, profile),
        "cons": _heuristic_cons(build_state, direction, profile),
        "within_budget": _heuristic_within_budget(build_state, direction, mode, profile),
        "stretch_budget": _heuristic_stretch_budget(build_state, direction, mode, profile),
        "source": "heuristic",
    }


def get_build_advisory(
    build_state: BuildState,
    mode: str,
    current_budget_or_cost: float,
    profile: str | None = None,
    bottleneck_info: dict | None = None,
) -> dict:
    """Public entry point. `mode` is one of "Budget" | "Workload" | "Free"
    (this project's three creation modes). `current_budget_or_cost` is the
    Budget-mode ceiling when mode == "Budget", otherwise the build's current
    total cost (Workload/Free have no ceiling concept). `profile` is the
    workload profile string when relevant. `bottleneck_info`, if omitted, is
    computed internally via engine.scoring.bottleneck_percentage_baseline.

    Never raises. Returns
    {"pros": [str, ...], "cons": [str, ...], "within_budget": str, "stretch_budget": str,
    "source": "llm" | "heuristic"}.
    """
    resolved_bottleneck_info, _pct, direction = _resolve_bottleneck(build_state, bottleneck_info)

    try:
        payload = _build_request_payload(build_state, mode, current_budget_or_cost, profile, resolved_bottleneck_info)
        raw = _call_openrouter(payload)
        response = BuildAdvisoryResponse.model_validate(raw)
    except (AdvisoryUnavailableError, PydanticValidationError):
        return _heuristic_advisory(build_state, direction, mode, profile)

    response.source = "llm"
    return response.model_dump()
