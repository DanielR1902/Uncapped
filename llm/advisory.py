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
import time
from typing import Any

import httpx
from pydantic import ValidationError as PydanticValidationError

from db.models import Component
from engine import compatibility, scoring, solvers
from engine.compatibility import BuildState
from llm.schemas import BuildAdvisoryResponse, QuantityAction, SwapAction

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Extended from 15.0 — see llm/client.py's own REQUEST_TIMEOUT_SECONDS
# comment (cold-start DNS/connection lookups need more room than a warm one).
REQUEST_TIMEOUT_SECONDS = 25.0

# Cold-start network/DNS resilience — same rationale/behavior as
# llm/client.py's own _post_with_retry (kept duplicated here rather than
# imported, matching this package's existing one-concern-per-file
# convention: OPENROUTER_URL/REQUEST_TIMEOUT_SECONDS above are already
# duplicated the same way rather than shared across client.py/advisory.py/
# concierge.py).
MAX_REQUEST_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
_RETRYABLE_NETWORK_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)


def _post_with_retry(url: str, **kwargs: Any) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            return httpx.post(url, **kwargs)
        except _RETRYABLE_NETWORK_ERRORS as exc:
            last_exc = exc
            if attempt < MAX_REQUEST_ATTEMPTS:
                print(
                    f"OpenRouter request attempt {attempt}/{MAX_REQUEST_ATTEMPTS} "
                    f"failed transiently ({exc!r}) — retrying...",
                    file=sys.stderr,
                )
                time.sleep(RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc

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
  Editing/multitasking-heavy profiles. "stretch_budget" should target whichever high-impact category
  gives the best real push toward the NEXT tier's typical requirements for that profile within the
  allowance — the category that matters most for the profile if it still has catalog headroom, but if
  it doesn't (already the priciest realistic option), evaluate a GPU tier bump, a RAM kit upgrade or an
  additional RAM kit (via a `set_quantity` action, respecting the motherboard's real DIMM slot count),
  an additional or upgraded Storage drive, or a Cooler upgrade instead. Never claim no stretch upgrade
  exists if a high-impact option fits the allowance.
- mode == "Free": There is no budget ceiling here either — optimize for bottleneck mitigation and
  CPU/GPU platform balance. "within_budget" proposes rebalancing: downgrade an over-specced component
  to fund the primary limiting (bottlenecked) one. "stretch_budget" should target whichever high-impact
  category gives the best real improvement within the allowance — the bottleneck component if it has
  catalog headroom, but if it doesn't (already the priciest realistic option), evaluate a GPU tier
  bump, a RAM kit upgrade or an additional RAM kit (via a `set_quantity` action, respecting the
  motherboard's real DIMM slot count), an additional or upgraded Storage drive, or a Cooler upgrade
  instead. Never claim no stretch upgrade exists if a high-impact option fits the allowance.

STRETCH SCOPE (overrides every other mention of RAM, Storage, Case, Cooler, PSU, quantities or peripherals
in the stretch guidance below): "stretch_budget.actions" may ONLY contain `swap` actions for the core
categories CPU, GPU or Motherboard. A Motherboard upgrade is valid on its own or paired with a CPU on a
different socket (list both swaps). RAM capacity, Storage size, Case, Cooler, PSU and every peripheral are
manual adjustments — never propose them, and never use `set_quantity` in stretch_budget. Every action
must strictly improve synergy or bottleneck without worsening the other; if none does, return an empty
`actions` list and `added_cost_usd` 0.0.

PEAKED-CATEGORY FALLTHROUGH RULE (applies to "stretch_budget" in every mode): If a specific category
(e.g. CPU) has peaked in socket/tier compatibility — no pricier compatible option exists for it —
evaluate GPU upgrades, increasing RAM quantity via a `set_quantity` action (up to the motherboard's
real DIMM slot count from catalog data), or increasing/upgrading Storage capacity, before concluding no
stretch upgrade exists. If EVERY core category (CPU/GPU/RAM/Storage/Motherboard/PSU/Case/Cooler) has
genuinely peaked too, evaluate the PERIPHERAL categories in `catalog_alternatives` next — Monitor,
Keyboard, Mouse, Headset, NetworkCard, SoundCard, OpticalDrive — before concluding no stretch upgrade
exists anywhere: real budget headroom spent on a better monitor or an added peripheral is still a real,
worthwhile stretch upgrade, not a fallback of last resort to be skipped. A peripheral entry with
`"current": null` means it isn't selected yet at all — propose a `swap` action naming one of its real
`pricier_alternatives` ids anyway; this both ADDS that peripheral to the build and IS the stretch
upgrade, with `added_cost_usd` equal to that part's own full real price (there is no existing price to
subtract, since nothing was there before). Only after core categories AND every peripheral have been
checked and found to have no headroom left should you say no stretch upgrade exists.

A `set_quantity` action looks like this: {"action": "set_quantity", "category": "RAM", "quantity": 2} —
use it to propose adding another kit/drive of the SAME already-selected part rather than swapping to a
different one. It is only ever valid for "RAM" or "Storage".

STRICT ARITHMETIC / NO-HALLUCINATION RULE: never invent parts or arbitrary prices. Every price delta
you state must exactly equal the difference between two real catalog prices given to you in
catalog_alternatives (or, for a `set_quantity` action, the exact real price of one more unit of the
already-selected part). If the current configuration is already optimal for its stated constraints (no
beneficial swap or quantity increase exists in the needed direction across every high-impact category),
say so explicitly rather than inventing one.

NEVER output a `replace_with_id` for a part not in the `catalog_alternatives` given to you. Every swap
action's `category` and `replace_with_id` must correspond exactly to one of the real `id` values listed
under that category's `cheaper_alternatives`/`pricier_alternatives`/`current` in `catalog_alternatives`.
A `set_quantity` action's `quantity` must never exceed the real motherboard slot count already present
in `catalog_alternatives`/the build's Motherboard entry (`ram_slots` for RAM, `m2_slots` for Storage) —
do not invent an achievable quantity; if slot data isn't available, don't propose a `set_quantity`
action at all. If no beneficial action exists, return an empty `swaps`/`actions` list rather than
inventing one — this is different from inventing a part name, which was already forbidden; now the
numeric id (or quantity) itself must be real too.

Respond with STRICT JSON and nothing else (no prose, no markdown fences), matching exactly this shape:
{
  "pros": ["Concise strength 1", "Concise strength 2"],
  "cons": ["Concise limitation 1", "Concise limitation 2"],
  "within_budget": {
    "explanation": "Concise optimization advice...",
    "swaps": [{"category": "Cooler", "replace_with_id": 42}],
    "can_optimize_further": false
  },
  "stretch_budget": {
    "explanation": "Concise next-tier upgrade advice...",
    "actions": [{"action": "swap", "category": "GPU", "replace_with_id": 105}],
    "added_cost_usd": 150.0
  }
}

"pros": a short list of concrete strengths of the CURRENT build, grounded in the components and
catalog alternatives given (e.g. good CPU/GPU synergy for the stated mode/workload, strong value for
the price tier, no compatibility conflicts) — never invent specs that weren't given.

"cons": a short list of concrete limitations of the CURRENT build, grounded the same way (e.g. a
bottleneck imbalance, an over-provisioned or under-provisioned component, a weak link for the stated
workload).

"within_budget.explanation": ONE concise piece of optimization advice following the mode-specific
objective above — downgrading an over-provisioned component (using the catalog alternatives given) to
fund a weaker one, balancing CPU/GPU synergy for the stated mode/workload, or a 1:1 value swap (same
price tier, better fit). "within_budget.swaps" lists the exact real catalog swap(s) (zero, one, or a
paired downgrade+upgrade) that realize this advice. "within_budget.can_optimize_further" reflects
whether you believe a further beneficial swap remains after this one, within the same mode-specific
objective.

"stretch_budget.explanation": ONE concise next-tier upgrade recommendation following the mode-specific
objective above and the peaked-category fallthrough rule — name an exact part (or an added quantity of
the current part) from the catalog alternatives given and its exact cost delta versus the current pick
in that category, in USD-style formatting (e.g. "an additional 35 USD"). In Budget mode keep this to a
modest increase (roughly 20 to 60 USD, or +10% of the current figure, beyond remaining_budget).
"stretch_budget.actions" lists the exact real catalog action(s) — `swap` and/or `set_quantity` — that
realize this advice (empty if none). "stretch_budget.added_cost_usd" must be the exact numeric total of
all actions' price deltas versus their current picks (not an estimate) — 0.0 when actions is empty.

All keys shown above are required in every response.
"""


def _component_summary(component: Component) -> dict:
    return {
        "id": component.id,
        "category": component.category,
        "name": component.name,
        "price_usd": component.price_usd,
    }


def _catalog_alternatives(build_state: BuildState) -> dict[str, dict]:
    """For each core category currently filled, the current pick plus its
    immediate cheaper/pricier compatible neighbors by price — grounds the
    prompt in real parts instead of letting the model hallucinate plausible-
    sounding fake ones.

    Peripheral categories (solvers.PERIPHERAL_CATEGORIES — Monitor/Keyboard/
    Mouse/Headset/NetworkCard/SoundCard/OpticalDrive) are included too (a
    real, confirmed gap this closes: they used to be skipped entirely on the
    theory that "no compatibility rules differentiate them" made a price-
    neighbor summary meaningless — true for compatibility, irrelevant for a
    STRETCH-BUDGET upgrade recommendation, which is exactly a price-neighbor
    question). Unlike core categories, a peripheral may be entirely
    unselected — real budget headroom can still exist there even after every
    core category has peaked, which is exactly the case this closes. An
    unselected peripheral gets `"current": None` and its `"pricier_
    alternatives"` are simply its top real candidates by price (there's no
    existing pick to compare against), so the model can propose adding one
    for the first time, not just upgrading an existing pick."""
    alternatives: dict[str, dict] = {}
    for category, current in build_state.items():
        if category not in solvers.CATEGORY_ORDER and category not in solvers.PERIPHERAL_CATEGORIES:
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
    for category in solvers.PERIPHERAL_CATEGORIES:
        if category in build_state:
            continue  # already covered by the loop above
        candidates = sorted(
            solvers.get_compatible_candidates(category, build_state),
            key=lambda c: c.price_usd,
        )
        if not candidates:
            continue
        alternatives[category] = {
            "current": None,
            "cheaper_alternatives": [],
            "pricier_alternatives": [_component_summary(c) for c in candidates[-_NEIGHBORS_PER_DIRECTION:]],
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
        response = _post_with_retry(
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
def _heuristic_within_budget(
    build_state: BuildState,
    direction: str,
    mode: str,
    profile: str | None,
    quantities: dict[str, int] | None = None,
) -> dict:
    """Returns a dict shaped like WithinBudgetAdvice.model_dump():
    {"explanation": str, "swaps": [{"category", "replace_with_id"}, ...], "can_optimize_further": bool}.

    The single-downgrade logic is unchanged from before this schema migration
    (same wording, same candidate search). New/additive: once a downgrade is
    found, this also looks for a specific compatible upgrade for the LIMITING
    category affordable within the exact savings just freed up — a real,
    catalog-priced paired reallocation — and appends it as a second swap when
    one exists. can_optimize_further is computed by applying whichever swaps
    were found to a hypothetical BuildState and re-running
    engine.scoring.bottleneck_percentage_baseline on it."""
    if direction == "Balanced":
        base = (
            "Your CPU and GPU are well balanced — there's no bottleneck-driven swap to make; "
            "consider redirecting any spare budget toward storage speed, RAM capacity, or case "
            "airflow instead."
        )
        if mode == "Workload" and profile:
            base += f" This is already a solid balance for a {profile} workload."
        return {"explanation": base, "swaps": [], "can_optimize_further": False}

    limiting_category, _, _ = direction.partition("-")  # "CPU-bound" -> "CPU"
    other_category = "GPU" if limiting_category == "CPU" else "CPU"
    other_component = build_state.get(other_category)

    if other_component is None:
        explanation = (
            f"The build is {direction.lower()} — pick a {other_category} to rebalance against before "
            "an in-budget swap can be suggested."
        )
        return {"explanation": explanation, "swaps": [], "can_optimize_further": False}

    others_fixed = {c: v for c, v in build_state.items() if c != other_category}
    candidates = solvers.get_compatible_candidates(other_category, others_fixed)
    cheaper = [c for c in candidates if c.id != other_component.id and c.price_usd < other_component.price_usd]

    if not cheaper:
        explanation = (
            f"The build is {direction.lower()}, but {other_component.name} is already the cheapest "
            f"compatible {other_category} option — there's no further downgrade available to fund the "
            f"{limiting_category}."
        )
        return {"explanation": explanation, "swaps": [], "can_optimize_further": False}

    downgrade = max(cheaper, key=lambda c: c.price_usd)  # smallest step down that still frees up money
    savings = other_component.price_usd - downgrade.price_usd
    swap_text = (
        f"The build is {direction.lower()}: downgrade {other_category} from {other_component.name} to "
        f"{downgrade.name} (-{savings:,.2f} USD) and put the savings toward a stronger {limiting_category}"
    )

    if mode == "Budget":
        explanation = f"{swap_text} — a cost-neutral reallocation that stays within your budget."
    elif mode == "Workload":
        if profile:
            explanation = f"{swap_text} to better suit your {profile} workload."
        else:
            explanation = f"{swap_text} to better suit your stated workload."
    else:  # Free mode: bottleneck mitigation with explicit CPU/GPU platform balance framing.
        explanation = f"{swap_text} to rebalance the CPU/GPU platform."

    swaps: list[dict] = [{"category": other_category, "replace_with_id": downgrade.id}]

    # Additive paired reallocation: can the freed-up `savings` afford a real
    # compatible upgrade for the limiting side? Evaluated against the build
    # WITH the downgrade already applied, since that's the actual resulting
    # configuration the pair would leave behind.
    hypothetical = dict(build_state)
    hypothetical[other_category] = downgrade

    current_limiting = build_state[limiting_category]
    limiting_candidates = solvers.get_compatible_candidates(limiting_category, hypothetical)
    affordable_upgrades = [
        c
        for c in limiting_candidates
        if c.id != current_limiting.id
        and c.price_usd > current_limiting.price_usd
        and c.price_usd <= current_limiting.price_usd + savings
    ]
    if affordable_upgrades:
        paired_upgrade = max(affordable_upgrades, key=lambda c: c.price_usd)  # best upgrade the savings can buy
        swaps.append({"category": limiting_category, "replace_with_id": paired_upgrade.id})
        hypothetical[limiting_category] = paired_upgrade

    # Monotonic Improvement Gate: the heuristic's own candidates are held to
    # the same strict rule as LLM-returned ones (a lone downgrade that only
    # pays off when paired is judged together with its paired upgrade).
    kept, gated_state, _gated_q = _gate_actions(
        build_state, quantities, [{"action": "swap", **swap} for swap in swaps]
    )
    if not kept:
        return {"explanation": _NO_IN_BUDGET_IMPROVEMENT, "swaps": [], "can_optimize_further": False}
    if len(kept) != len(swaps):
        explanation = "Swap " + _describe_kept(build_state, kept, gated_state) + " to improve balance within your budget."
        hypothetical = gated_state
    swaps = [{"category": s["category"], "replace_with_id": s["replace_with_id"]} for s in kept]

    _, new_direction = scoring.bottleneck_percentage_baseline(hypothetical)
    can_optimize_further = new_direction != "Balanced"

    return {"explanation": explanation, "swaps": swaps, "can_optimize_further": can_optimize_further}


def _try_swap_upgrade(build_state: BuildState, category: str) -> tuple[Component, Component, float] | None:
    """The pricier-compatible-option search that used to be the ENTIRE
    _heuristic_stretch_budget body, now factored out so the fallthrough chain
    below can try it against several categories in turn. Returns
    (current, upgrade, delta) for the cheapest real catalog option pricier
    than the current pick, or None if `category` isn't in the build or has no
    pricier compatible option (already the priciest realistic pick)."""
    current = build_state.get(category)
    if current is None:
        return None
    others_fixed = {c: v for c, v in build_state.items() if c != category}
    candidates = solvers.get_compatible_candidates(category, others_fixed)
    pricier = [c for c in candidates if c.id != current.id and c.price_usd > current.price_usd]
    if not pricier:
        return None
    upgrade = min(pricier, key=lambda c: c.price_usd)  # cheapest upgrade that still helps
    return current, upgrade, upgrade.price_usd - current.price_usd


def _try_peripheral_upgrade_or_add(build_state: BuildState, category: str) -> tuple[Component, Component, float] | None:
    """The peripheral analogue of _try_swap_upgrade — a real, confirmed gap
    this closes: _try_swap_upgrade requires `category` to already be in
    build_state (true for every CORE category, since a complete build always
    has all 8), but `solvers.PERIPHERAL_CATEGORIES` (Monitor/Keyboard/Mouse/
    Headset/NetworkCard/SoundCard/OpticalDrive) are all OPTIONAL — a build can
    have real, substantial budget headroom left with one genuinely unselected,
    with nothing in the old bottleneck->GPU->RAM->Storage->Cooler fallthrough
    chain ever considering "add a peripheral that isn't picked yet" as a real
    stretch upgrade, even though that's exactly where the remaining money
    should go once every core category has peaked. If `category` is already
    selected, behaves like _try_swap_upgrade (cheapest real option pricier
    than the current pick). If NOT yet selected, "current" is a real $0.0
    baseline (nothing bought yet) and the upgrade is the single MOST
    EXPENSIVE real compatible candidate — mirroring engine.solvers.
    _fill_peripherals_with_surplus's own "spend surplus on the highest tier
    that fits" philosophy for a first-time peripheral pick, rather than
    _try_swap_upgrade's "cheapest step up" (there's no existing pick to step
    up from cheaply here, and a lowball first pick would waste real headroom
    the exact same way this whole gap already does). Returns None only if
    the category has no real candidates at all (should not happen for a
    non-empty catalog category) or, when already selected, no pricier option
    exists."""
    current = build_state.get(category)
    if current is not None:
        return _try_swap_upgrade(build_state, category)
    candidates = solvers.get_compatible_candidates(category, build_state)
    if not candidates:
        return None
    priciest = max(candidates, key=lambda c: c.price_usd)
    placeholder_current = Component(
        id=-1, category=category, name=f"(no {category} selected)", brand="", price_usd=0.0,
    )
    return placeholder_current, priciest, priciest.price_usd


def _try_quantity_increment(
    build_state: BuildState, category: str, quantities: dict[str, int]
) -> tuple[Component, int, int, float] | None:
    """The RAM/Storage analogue of _try_swap_upgrade: proposing ONE more of
    the already-selected part rather than swapping to a different one.
    Reuses _real_slot_count's real motherboard-backed bound (ram_slots for
    RAM, m2_slots for Storage — and only when the current Storage pick's
    interface actually contains "NVMe", same gating engine.compatibility.
    check_storage_slot_capacity uses). Returns
    (component, current_qty, new_qty, added_cost) — added_cost is one more
    unit at the SAME price as the current pick, the only defensible number
    for "one more of what's already selected" — or None if the category
    isn't in the build, has no real slot-count data, or is already at its
    real slot limit."""
    component = build_state.get(category)
    if component is None:
        return None
    slot_count = _real_slot_count(build_state, category)
    if slot_count is None:
        return None
    current_qty = quantities.get(category, 1)
    new_qty = current_qty + 1
    if new_qty > slot_count:
        return None
    return component, current_qty, new_qty, component.price_usd


def _stretch_reason(mode: str, profile: str | None, direction: str, category: str, limiting_category: str) -> str:
    """Mode-specific trailing-sentence framing for a stretch-budget action on
    `category`, which may or may not be the actual bottleneck
    (`limiting_category`) — the fallthrough chain in
    _heuristic_stretch_budget can land on GPU/RAM/Storage/Cooler even when
    the bottleneck itself is CPU. Budget/Free framing calls that out
    explicitly when the winning category isn't the bottleneck; Workload
    framing is profile-centric regardless of which category won, matching
    this project's existing wording there."""
    is_bottleneck_category = category == limiting_category and direction != "Balanced"

    if mode == "Workload":
        return (
            f"to push this build toward the next tier for a {profile} workload"
            if profile
            else "to push this build toward the next performance tier"
        )

    if mode == "Free":
        if is_bottleneck_category:
            return f"to directly target the {direction.lower()} bottleneck and improve platform synergy"
        if direction != "Balanced":
            return f"since {limiting_category} has no further upgrade headroom in the catalog"
        return "for extra platform headroom"

    # Budget
    if is_bottleneck_category:
        return f"to ease the {direction.lower()} imbalance"
    if direction != "Balanced":
        return f"since {limiting_category} has no further upgrade headroom in the catalog"
    return "for extra headroom"


def _quantity_explanation(
    component: Component,
    category: str,
    new_qty: int,
    mode: str,
    profile: str | None,
    direction: str,
    limiting_category: str,
) -> str:
    """A distinct, sensible explanation sentence for a set_quantity action —
    a RAM/Storage quantity increment obviously can't say "Upgrade X to Y" the
    same way a swap does."""
    ordinal = {2: "second", 3: "third", 4: "fourth", 5: "fifth"}.get(new_qty, f"{new_qty}th")
    if category == "RAM":
        total_gb = (component.capacity_gb or 0) * new_qty
        detail = (
            f"Add a {ordinal} {component.name} kit (+{component.price_usd:,.2f} USD) to reach "
            f"{total_gb}GB total"
        )
    else:  # Storage
        detail = (
            f"Add a {ordinal} {component.name} drive (+{component.price_usd:,.2f} USD) for {new_qty}x "
            "NVMe drives total"
        )
    reason = _stretch_reason(mode, profile, direction, category, limiting_category)
    return f"{detail}, {reason}."


# Stretch Budget scope (spec.md §6.6.3): only these core categories may be
# proposed. RAM/Storage/Case/Cooler/PSU and peripherals stay manual.
STRETCH_CATEGORIES: tuple[str, ...] = ("CPU", "GPU", "Motherboard")


def _swap_candidates(category: str, build_state: BuildState, platform: bool = False) -> dict[int, Component]:
    """Compatible candidates by id. With `platform=True`, CPU/Motherboard also
    include parts that are only compatible once their socket partner is
    swapped too (a CPU on a new socket needs a new board); the monotonic gate
    then judges the pair jointly against the full build."""
    found = {c.id: c for c in solvers.get_compatible_candidates(category, build_state)}
    if platform and category in ("CPU", "Motherboard"):
        partner = "Motherboard" if category == "CPU" else "CPU"
        without_partner = {c: v for c, v in build_state.items() if c != partner}
        for c in solvers.get_compatible_candidates(category, without_partner):
            found.setdefault(c.id, c)
    return found


def _try_platform_upgrade(build_state: BuildState, quantities: dict[str, int], limiting_category: str) -> dict | None:
    """CPU-on-a-new-socket + cheapest compatible Motherboard, gated as a unit."""
    cpu, board = build_state.get("CPU"), build_state.get("Motherboard")
    if limiting_category != "CPU" or cpu is None or board is None:
        return None
    current_pct, _direction = scoring.bottleneck_percentage_baseline(build_state)
    base_without_pair = {c: v for c, v in build_state.items() if c not in ("CPU", "Motherboard")}
    for candidate in sorted(
        (c for c in _swap_candidates("CPU", build_state, platform=True).values() if c.price_usd > cpu.price_usd),
        key=lambda c: c.price_usd,
    ):
        new_pct, _d = scoring.bottleneck_percentage_baseline({**build_state, "CPU": candidate})
        if new_pct >= current_pct:
            continue  # cheap pre-filter: can't improve, skip the expensive board search
        boards = sorted(
            solvers.get_compatible_candidates("Motherboard", {**base_without_pair, "CPU": candidate}),
            key=lambda m: m.price_usd,
        )
        for new_board in boards:
            actions = [
                {"action": "swap", "category": "CPU", "replace_with_id": candidate.id},
                {"action": "swap", "category": "Motherboard", "replace_with_id": new_board.id},
            ]
            kept, final_state, _q = _gate_actions(build_state, quantities, actions)
            if len(kept) == 2:
                cost = _real_cost_delta(build_state, quantities, kept, final_state)
                if cost <= 0:
                    continue
                explanation = (
                    f"Upgrade the platform: CPU to {candidate.name} with Motherboard {new_board.name} "
                    f"(+{cost:,.2f} USD) to strictly improve synergy."
                )
                return {"explanation": explanation, "actions": kept, "added_cost_usd": cost}
    return None


def _heuristic_stretch_budget(
    build_state: BuildState,
    direction: str,
    mode: str,
    profile: str | None,
    quantities: dict[str, int] | None = None,
) -> dict:
    """Returns a dict shaped like StretchBudgetAdvice.model_dump():
    {"explanation": str, "actions": [{"action": "swap"|"set_quantity", ...}] or [], "added_cost_usd": float}.

    Priority-ordered fallthrough chain — tries each category in turn and
    stops at the first one with a real, catalog-priced upgrade available,
    instead of giving up the moment the single bottleneck category has
    peaked in socket/tier compatibility:
      1. The bottleneck-category swap (CPU or GPU, whichever `direction`
         names as limiting) — same logic this function always had.
      2. A GPU swap to a pricier compatible option (skipped if GPU was
         already tried in step 1).
      3. Incrementing RAM quantity by 1, if the motherboard's real DIMM slot
         count allows it.
      4. Incrementing Storage quantity by 1 (NVMe drives only — the only
         storage interface with a real slot-count constraint in this
         catalog).
      5. A Cooler swap to a pricier compatible option.
      6. Only if ALL of the above fail: the honest "no stretch-budget
         upgrade is available" explanation, now describing that every
         high-impact category was checked, not just one.
    """
    quantities = quantities or {}

    if direction == "Balanced":
        limiting_category = "GPU" if "GPU" in build_state else ("CPU" if "CPU" in build_state else None)
    else:
        limiting_category, _, _ = direction.partition("-")

    if limiting_category is None or limiting_category not in build_state:
        explanation = "Add a CPU and a GPU to the build to get a stretch-budget upgrade recommendation."
        return {"explanation": explanation, "actions": [], "added_cost_usd": 0.0}

    # Every proposal below is held to the Monotonic Improvement Gate
    # (`_gate_actions`): an action that does not strictly improve synergy or
    # bottleneck without degrading the other is skipped, so the chain
    # continues to the next option instead of suggesting something that only
    # costs money (RAM/Storage quantity, Cooler and peripherals do not move
    # either metric, so in practice they are rejected here).
    def _passes(action: dict) -> bool:
        kept, _state, _q = _gate_actions(build_state, quantities, [action])
        return bool(kept)

    tried_swap_categories: set[str] = set()

    for candidate_category in (limiting_category, "GPU"):
        if candidate_category in tried_swap_categories or candidate_category not in build_state:
            continue
        tried_swap_categories.add(candidate_category)
        current = build_state[candidate_category]
        others_fixed = {c: v for c, v in build_state.items() if c != candidate_category}
        pricier = sorted(
            (
                c
                for c in solvers.get_compatible_candidates(candidate_category, others_fixed)
                if c.id != current.id and c.price_usd > current.price_usd
            ),
            key=lambda c: c.price_usd,
        )
        for upgrade in pricier:  # cheapest upgrade that also strictly improves the build
            action = {"action": "swap", "category": candidate_category, "replace_with_id": upgrade.id}
            if not _passes(action):
                continue
            delta = upgrade.price_usd - current.price_usd
            reason = _stretch_reason(mode, profile, direction, candidate_category, limiting_category)
            explanation = f"Upgrade {candidate_category} to {upgrade.name} (+{delta:,.2f} USD) {reason}."
            return {"explanation": explanation, "actions": [action], "added_cost_usd": round(delta, 2)}

    # Motherboard is a core stretch category alongside CPU/GPU (spec.md §6.6.3).
    # It is not itself scored, so a standalone board swap only survives the
    # gate if it genuinely moves synergy/bottleneck (e.g. resolving a warning);
    # the real payoff is a PLATFORM upgrade — a stronger CPU on a new socket
    # together with the cheapest board that makes the build compatible again,
    # judged as one unit.
    current_board = build_state.get("Motherboard")
    if current_board is not None:
        others_fixed = {c: v for c, v in build_state.items() if c != "Motherboard"}
        pricier_boards = sorted(
            (
                c
                for c in solvers.get_compatible_candidates("Motherboard", others_fixed)
                if c.id != current_board.id and c.price_usd > current_board.price_usd
            ),
            key=lambda c: c.price_usd,
        )
        for board in pricier_boards:
            action = {"action": "swap", "category": "Motherboard", "replace_with_id": board.id}
            if _passes(action):
                delta = board.price_usd - current_board.price_usd
                explanation = f"Upgrade Motherboard to {board.name} (+{delta:,.2f} USD) to improve platform synergy."
                return {"explanation": explanation, "actions": [action], "added_cost_usd": round(delta, 2)}

    platform = _try_platform_upgrade(build_state, quantities, limiting_category)
    if platform is not None:
        return platform

    return {"explanation": _NO_STRETCH_IMPROVEMENT, "actions": [], "added_cost_usd": 0.0}


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


def _heuristic_advisory(
    build_state: BuildState,
    direction: str,
    mode: str,
    profile: str | None,
    quantities: dict[str, int] | None = None,
) -> dict:
    return {
        "pros": _heuristic_pros(build_state, direction, profile),
        "cons": _heuristic_cons(build_state, direction, profile),
        "within_budget": _heuristic_within_budget(build_state, direction, mode, profile, quantities),
        "stretch_budget": _heuristic_stretch_budget(build_state, direction, mode, profile, quantities),
        "source": "heuristic",
    }


_QUANTITY_ELIGIBLE_CATEGORIES = frozenset({"RAM", "Storage"})


def _real_slot_count(build_state: BuildState, category: str) -> int | None:
    """Thin delegate to engine.compatibility.resolve_quantity_limit — kept as
    a named wrapper (rather than inlining the call at every use site) so
    this module's existing call sites/tests don't need to change shape.
    engine.compatibility.resolve_quantity_limit is the ONE place that owns
    the real RAM-module-count / Storage-NVMe-slot math (real `ram_slots //
    modules-per-kit` for RAM via `_ram_kit_module_count`'s "(NxYGB)" name
    parse, real `m2_slots` for NVMe Storage only) — this module must never
    duplicate that math itself. Returns just the `max_allowed` half of the
    `(max_allowed, reason)` tuple; the reason string is a UI concern."""
    max_allowed, _reason = compatibility.resolve_quantity_limit(build_state, category)
    return max_allowed


def _validate_advisory_actions(
    build_state: BuildState, response: BuildAdvisoryResponse, quantities: dict[str, int] | None = None,
) -> None:
    """Post-parse hallucination guard (authoritative — the SYSTEM_PROMPT rule
    is just a request, this is what actually enforces it).

    within_budget.swaps (still plain SwapActions) are validated exactly as
    before: category must be one of build_state's core (solvers.
    CATEGORY_ORDER) categories, and replace_with_id must be a real,
    currently-valid compatible-candidate id for that category.

    stretch_budget.actions is the new mixed SwapAction | QuantityAction
    union — each item is branched on `.action`: a "swap" gets the same
    real-id check; a "set_quantity" is only valid for "RAM"/"Storage", and
    its `quantity` must be a positive int that does not exceed the real
    motherboard slot count for that category (see _real_slot_count).

    STRETCH COST/DIRECTION ENFORCEMENT (a real, confirmed bug this closes):
    the SYSTEM_PROMPT tells the model every stretch price delta must exactly
    match a real catalog difference and that a stretch action is always a
    genuine upgrade — but that was only ever a prompt request, never checked
    in Python, and a live call was caught doing exactly what that gap
    allows: proposing a "swap" to the SAME already-selected GPU with a
    fabricated "+400 USD" delta, then on the next round proposing a swap to
    a CHEAPER compatible GPU labeled as a further upgrade at another
    fabricated positive delta. `ui/components/chat_assistant.py`'s
    `use_remaining_budget` loop trusts `stretch_budget.added_cost_usd` being
    non-zero to mean "real forward progress" and applies the action
    unconditionally, so a hallucinated action like this doesn't just misquote
    a number — it silently makes the build WORSE per available budget
    (verified live: a "no-op" swap it then downgraded traded real GPU tiers
    away from the one the deterministic solver had already picked) while
    leaving real, genuine headroom (a 2nd RAM kit, additional NVMe drives)
    completely unexplored. Every stretch action's REAL price delta is now
    recomputed here directly from catalog data (a swap's real delta must be
    strictly positive — a stretch action can never be a lateral or backward
    move) and `response.stretch_budget.added_cost_usd` must match the sum of
    those real deltas (a small float-rounding tolerance aside); any mismatch
    raises, funneling to the same deterministic heuristic fallback below,
    whose own `_try_swap_upgrade`/`_try_quantity_increment` already only
    ever propose a strictly-pricier real option by construction.

    Raises AdvisoryUnavailableError on any violation so get_build_advisory's
    existing `except (AdvisoryUnavailableError, PydanticValidationError):`
    block funnels a hallucinated/invalid action into the same heuristic
    fallback as any other failure mode — never a separate code path."""
    quantities = quantities or {}
    valid_candidates_by_category: dict[str, dict[int, Component]] = {}

    def _check_swap(swap: SwapAction) -> None:
        is_core = swap.category in solvers.CATEGORY_ORDER
        is_peripheral = swap.category in solvers.PERIPHERAL_CATEGORIES
        if not is_core and not is_peripheral:
            raise AdvisoryUnavailableError(
                f"Advisory swap references an unknown category: {swap.category!r}"
            )
        # A CORE category is always present in a complete build — absence
        # means a hallucinated category, not a real gap to fill. A PERIPHERAL
        # category is legitimately optional (spec.md §5.2/§7.4) — a stretch
        # action may propose adding one for the first time, so its absence
        # from build_state is expected, not an error (see
        # _try_peripheral_upgrade_or_add's own docstring for why this matters).
        if is_core and swap.category not in build_state:
            raise AdvisoryUnavailableError(
                f"Advisory swap references a core category missing from the current build: "
                f"{swap.category!r}"
            )
        if swap.category not in valid_candidates_by_category:
            valid_candidates_by_category[swap.category] = _swap_candidates(
                swap.category, build_state, platform=(swap.category in STRETCH_CATEGORIES)
            )
        if swap.replace_with_id not in valid_candidates_by_category[swap.category]:
            raise AdvisoryUnavailableError(
                f"Advisory swap references a non-catalog id {swap.replace_with_id} for category "
                f"{swap.category!r}"
            )

    def _check_quantity(action: QuantityAction) -> None:
        if action.category not in _QUANTITY_ELIGIBLE_CATEGORIES:
            raise AdvisoryUnavailableError(
                f"Advisory set_quantity action references a non-quantity-eligible category: "
                f"{action.category!r}"
            )
        slot_count = _real_slot_count(build_state, action.category)
        if slot_count is None:
            raise AdvisoryUnavailableError(
                f"Advisory set_quantity action for {action.category!r} has no real slot-count data "
                "to validate against"
            )
        if action.quantity < 1 or action.quantity > slot_count:
            raise AdvisoryUnavailableError(
                f"Advisory set_quantity action for {action.category!r} requests quantity "
                f"{action.quantity}, exceeding the real slot count {slot_count}"
            )

    for swap in response.within_budget.swaps:
        _check_swap(swap)

    real_added_cost = 0.0
    for action in response.stretch_budget.actions:
        if isinstance(action, QuantityAction):
            _check_quantity(action)
            component = build_state[action.category]
            current_qty = quantities.get(action.category, 1)
            if action.quantity <= current_qty:
                raise AdvisoryUnavailableError(
                    f"Advisory stretch set_quantity action for {action.category!r} requests quantity "
                    f"{action.quantity}, which is not an increase over the current quantity {current_qty}"
                )
            real_added_cost += component.price_usd * (action.quantity - current_qty)
        else:
            _check_swap(action)
            # 0.0 baseline for a not-yet-selected peripheral (a real, valid
            # "first add" — _check_swap already confirmed this is only ever
            # allowed for a peripheral category, never a core one).
            existing = build_state.get(action.category)
            current_price = existing.price_usd if existing is not None else 0.0
            new_price = valid_candidates_by_category[action.category][action.replace_with_id].price_usd
            if new_price <= current_price:
                raise AdvisoryUnavailableError(
                    f"Advisory stretch swap for {action.category!r} to id {action.replace_with_id} is not "
                    f"strictly pricier than the current pick ({new_price} <= {current_price}) — a stretch "
                    "action must always be a genuine upgrade, never a lateral or backward move"
                )
            real_added_cost += new_price - current_price

    if response.stretch_budget.actions and abs(real_added_cost - response.stretch_budget.added_cost_usd) > 0.5:
        raise AdvisoryUnavailableError(
            f"Advisory stretch_budget.added_cost_usd ({response.stretch_budget.added_cost_usd}) doesn't "
            f"match the real catalog-derived total ({real_added_cost:.2f})"
        )


_NO_IN_BUDGET_IMPROVEMENT = (
    "No in-budget swap strictly improves this build's synergy or bottleneck without degrading the "
    "other — it is already optimal for this budget."
)
_NO_STRETCH_IMPROVEMENT = (
    "No stretch-budget upgrade strictly improves this build's synergy or bottleneck without "
    "degrading the other, so there is nothing worth adding right now."
)


def _apply_action(
    state: BuildState,
    quantities: dict[str, int],
    action: dict,
    lookup: dict[str, dict[int, Component]],
) -> tuple[BuildState, dict[str, int]] | None:
    """Simulate ONE swap/set_quantity on copies of `state`/`quantities`.
    Returns None for a no-op (same id / same quantity) or an unknown part.
    Parts are identified by unique `id` only (never chipset/name similarity)."""
    category = action["category"]
    if action.get("action", "swap") == "set_quantity":
        if category not in state or quantities.get(category, 1) == action["quantity"]:
            return None
        return state, {**quantities, category: action["quantity"]}
    component = lookup.get(category, {}).get(action["replace_with_id"])
    current = state.get(category)
    if component is None or (current is not None and current.id == component.id):
        return None
    return {**state, category: component}, quantities


def _gate_actions(
    build_state: BuildState,
    quantities: dict[str, int] | None,
    actions: list[dict],
) -> tuple[list[dict], BuildState, dict[str, int]]:
    """MONOTONIC IMPROVEMENT GATE (spec.md §6.6.3). Applies `actions`
    cumulatively on a copy of the build and keeps only those that move the
    build strictly forward per `engine.solvers.is_monotonic_improvement`
    (warnings 0 or strictly fewer; synergy never down; bottleneck never up;
    at least one strictly better). Degrading, lateral, no-op, duplicate
    (same category + action kind) or invalid actions are discarded. A paired
    reallocation (e.g. downgrade GPU + upgrade CPU) is judged as a unit: an
    action that fails alone is held pending and re-judged together with the
    next one. Returns (kept, resulting_state, resulting_quantities)."""
    quantities = dict(quantities or {})
    lookup: dict[str, dict[int, Component]] = {}
    for action in actions:
        if action.get("action", "swap") == "swap" and action["category"] not in lookup:
            lookup[action["category"]] = _swap_candidates(action["category"], build_state, platform=True)

    accepted_state, accepted_q = dict(build_state), quantities
    accepted_metrics = solvers.build_metrics(accepted_state, accepted_q)
    kept: list[dict] = []
    pending: list[dict] = []
    work_state, work_q = accepted_state, accepted_q
    seen: set[tuple[str, str]] = set()
    for action in actions:
        key = (action.get("action", "swap"), action["category"])
        if key in seen:
            continue
        applied = _apply_action(work_state, work_q, action, lookup)
        if applied is None:
            continue
        seen.add(key)
        trial_state, trial_q = applied
        if solvers.is_monotonic_improvement(accepted_metrics, solvers.build_metrics(trial_state, trial_q)):
            kept.extend(pending + [action])
            pending = []
            accepted_state, accepted_q = trial_state, trial_q
            accepted_metrics = solvers.build_metrics(accepted_state, accepted_q)
            work_state, work_q = accepted_state, accepted_q
            continue
        if pending:
            alone = _apply_action(accepted_state, accepted_q, action, lookup)
            if alone is not None and solvers.is_monotonic_improvement(
                accepted_metrics, solvers.build_metrics(*alone)
            ):
                kept.append(action)
                accepted_state, accepted_q = alone
                accepted_metrics = solvers.build_metrics(accepted_state, accepted_q)
                pending = []
                work_state, work_q = accepted_state, accepted_q
                continue
        pending.append(action)
        work_state, work_q = trial_state, trial_q
    return kept, accepted_state, accepted_q


def _describe_kept(build_state: BuildState, kept: list[dict], final_state: BuildState) -> str:
    parts = []
    for action in kept:
        category = action["category"]
        if action.get("action", "swap") == "set_quantity":
            parts.append(f"set {category} quantity to {action['quantity']}")
        else:
            old = build_state.get(category)
            parts.append(f"{category} from {old.name if old is not None else 'none'} to {final_state[category].name}")
    return "; ".join(parts)


def _real_cost_delta(
    build_state: BuildState, quantities: dict[str, int] | None, kept: list[dict], final_state: BuildState
) -> float:
    quantities = quantities or {}
    total = 0.0
    for action in kept:
        category = action["category"]
        if action.get("action", "swap") == "set_quantity":
            component = build_state[category]
            total += component.price_usd * (action["quantity"] - quantities.get(category, 1))
        else:
            old = build_state.get(category)
            total += final_state[category].price_usd - (old.price_usd if old is not None else 0.0)
    return round(total, 2)


def _sanitize_within_budget(
    build_state: BuildState,
    within_budget: dict,
    direction: str,
    mode: str,
    profile: str | None,
    quantities: dict[str, int] | None,
) -> dict:
    """Run `within_budget.swaps` through the monotonic improvement gate
    (`_gate_actions`) so every remaining swap is executable AND strictly
    improves the build (no no-ops, duplicates, incompatible or degrading
    swaps — e.g. synergy 95->85 / bottleneck 1%->10% is discarded). If
    nothing was dropped, the LLM's own text is kept; if some were dropped, the
    explanation is regenerated from what remains; if none remain, the (also
    gated) deterministic heuristic is consulted, and if it finds nothing the
    result is an honest empty `swaps: []`."""
    swaps = within_budget["swaps"]
    if not swaps:
        return within_budget
    kept, final_state, _final_q = _gate_actions(build_state, quantities, swaps)
    if len(kept) == len(swaps):
        return within_budget
    if not kept:
        return _heuristic_within_budget(build_state, direction, mode, profile, quantities)
    return {
        "explanation": "Swap " + _describe_kept(build_state, kept, final_state) + " to improve balance within your budget.",
        "swaps": kept,
        "can_optimize_further": within_budget["can_optimize_further"],
    }


def _sanitize_stretch(
    build_state: BuildState,
    stretch: dict,
    quantities: dict[str, int] | None,
) -> dict:
    """Stretch counterpart of `_sanitize_within_budget`: only actions that
    strictly improve without degrading the other metric survive;
    `added_cost_usd` is recomputed from the kept actions' real catalog
    deltas; when all are dropped the result is `actions: []`,
    `added_cost_usd: 0.0` with an honest explanation (never a fallback that
    reintroduces a non-monotonic action)."""
    actions = [
        a for a in stretch["actions"]
        if a.get("action", "swap") == "swap" and a["category"] in STRETCH_CATEGORIES
    ]
    if not actions:
        return stretch if not stretch["actions"] else {
            "explanation": _NO_STRETCH_IMPROVEMENT, "actions": [], "added_cost_usd": 0.0,
        }
    kept, final_state, _final_q = _gate_actions(build_state, quantities, actions)
    if not kept:
        return {"explanation": _NO_STRETCH_IMPROVEMENT, "actions": [], "added_cost_usd": 0.0}
    cost = _real_cost_delta(build_state, quantities, kept, final_state)
    if len(kept) == len(stretch["actions"]):
        return {**stretch, "added_cost_usd": cost}
    return {
        "explanation": f"Apply {_describe_kept(build_state, kept, final_state)} (+{cost:,.2f} USD) "
        "to strictly improve synergy or bottleneck.",
        "actions": kept,
        "added_cost_usd": cost,
    }


def _is_stale_action(action: dict, build_state: BuildState, quantities: dict[str, int] | None) -> bool:
    """True when `action` would be a no-op against `build_state`: a swap whose
    target id is already the selected component of that category, or a
    set_quantity equal to the current quantity."""
    category = action.get("category")
    if action.get("action", "swap") == "set_quantity":
        return category in build_state and (quantities or {}).get(category, 1) == action.get("quantity")
    current = build_state.get(category)
    return current is not None and current.id == action.get("replace_with_id")


def prune_stale_actions(
    advisory: dict, build_state: BuildState, quantities: dict[str, int] | None = None
) -> dict:
    """STALE-PROPOSAL GUARD (spec.md §7.4.1). Returns a copy of `advisory`
    with every within_budget swap / stretch action that targets the component
    already selected in `build_state` (same category) removed — an advisory
    computed for a build that has since changed (e.g. right after an applied
    optimization) must never keep proposing the swap that was just applied.
    When nothing is pruned the input is returned unchanged. When every
    within_budget swap is pruned the explanation becomes the existing
    "already optimal for this budget" wording and `can_optimize_further` is
    False; when every stretch action is pruned the stretch text becomes the
    existing no-improvement wording with `added_cost_usd` 0.0. A partial prune
    regenerates the text from the survivors."""
    within = advisory.get("within_budget") or {}
    stretch = advisory.get("stretch_budget") or {}
    swaps = list(within.get("swaps") or [])
    actions = list(stretch.get("actions") or [])
    kept_swaps = [s for s in swaps if not _is_stale_action(s, build_state, quantities)]
    kept_actions = [a for a in actions if not _is_stale_action(a, build_state, quantities)]
    if len(kept_swaps) == len(swaps) and len(kept_actions) == len(actions):
        return advisory

    result = dict(advisory)

    def _describe(kept: list[dict]) -> str:
        final_state = dict(build_state)
        for action in kept:
            if action.get("action", "swap") == "swap":
                candidate = _swap_candidates(action["category"], build_state, platform=True).get(
                    action["replace_with_id"]
                )
                if candidate is not None:
                    final_state[action["category"]] = candidate
        try:
            return _describe_kept(build_state, kept, final_state)
        except KeyError:
            return "the remaining suggested changes"

    if len(kept_swaps) != len(swaps):
        if kept_swaps:
            result["within_budget"] = {
                **within,
                "explanation": "Swap " + _describe(kept_swaps) + " to improve balance within your budget.",
                "swaps": kept_swaps,
            }
        else:
            result["within_budget"] = {
                **within, "explanation": _NO_IN_BUDGET_IMPROVEMENT, "swaps": [], "can_optimize_further": False,
            }
    if len(kept_actions) != len(actions):
        if kept_actions:
            cost = _real_cost_delta(build_state, quantities, kept_actions, {
                **build_state,
                **{
                    a["category"]: c
                    for a in kept_actions
                    if a.get("action", "swap") == "swap"
                    for c in [_swap_candidates(a["category"], build_state, platform=True).get(a["replace_with_id"])]
                    if c is not None
                },
            })
            result["stretch_budget"] = {
                **stretch,
                "explanation": f"Apply {_describe(kept_actions)} (+{cost:,.2f} USD) to strictly improve synergy or bottleneck.",
                "actions": kept_actions,
                "added_cost_usd": cost,
            }
        else:
            result["stretch_budget"] = {
                **stretch, "explanation": _NO_STRETCH_IMPROVEMENT, "actions": [], "added_cost_usd": 0.0,
            }
    return result


def get_build_advisory(
    build_state: BuildState,
    mode: str,
    current_budget_or_cost: float,
    profile: str | None = None,
    bottleneck_info: dict | None = None,
    quantities: dict[str, int] | None = None,
) -> dict:
    """Public entry point. `mode` is one of "Budget" | "Workload" | "Free"
    (this project's three creation modes). `current_budget_or_cost` is the
    Budget-mode ceiling when mode == "Budget", otherwise the build's current
    total cost (Workload/Free have no ceiling concept). `profile` is the
    workload profile string when relevant. `bottleneck_info`, if omitted, is
    computed internally via engine.scoring.bottleneck_percentage_baseline.
    `quantities` (optional, e.g. `{"RAM": 1, "Storage": 2}`) is the current
    per-category quantity for the RAM/Storage multi-slot feature — every
    category defaults to 1 when omitted or not a key of this dict. It is only
    ever consumed by the heuristic stretch-budget fallback's RAM/Storage
    quantity-increment step (_heuristic_stretch_budget); it never reaches the
    LLM payload and within_budget's heuristic is untouched by it. Omitting it
    is fully backward compatible with every existing caller.

    Never raises. Returns
    {"pros": [str, ...], "cons": [str, ...],
     "within_budget": {"explanation": str, "swaps": [{"category", "replace_with_id"}, ...], "can_optimize_further": bool},
     "stretch_budget": {"explanation": str, "actions": [{"action": "swap", "category", "replace_with_id"} | {"action": "set_quantity", "category", "quantity"}, ...], "added_cost_usd": float},
     "source": "llm" | "heuristic"}.
    """
    resolved_bottleneck_info, _pct, direction = _resolve_bottleneck(build_state, bottleneck_info)

    try:
        payload = _build_request_payload(build_state, mode, current_budget_or_cost, profile, resolved_bottleneck_info)
        raw = _call_openrouter(payload)
        response = BuildAdvisoryResponse.model_validate(raw)
        _validate_advisory_actions(build_state, response, quantities)
    except (AdvisoryUnavailableError, PydanticValidationError):
        return _heuristic_advisory(build_state, direction, mode, profile, quantities)

    # FALSE-NEGATIVE CROSS-CHECK (a real, confirmed bug this closes, the
    # mirror image of the cost/direction enforcement above): a validly-
    # parsed response with an EMPTY stretch_budget.actions isn't a
    # hallucination this module can reject — it's a legitimate "no upgrade
    # needed" verdict, structurally. But a live call was caught returning
    # exactly that (`Delta: +€0.00`) for a build sitting on real, substantial
    # headroom (e.g. €1,864.68 of a €4,000+ ceiling unspent) purely because
    # every CORE category had peaked and the model's own reasoning stopped
    # there, never considering an unselected/upgradeable peripheral (Monitor/
    # Keyboard/Mouse/Headset/etc.) as a real stretch option — even with the
    # PEAKED-CATEGORY FALLTHROUGH RULE telling it to. Since
    # `_heuristic_stretch_budget` is fully deterministic and, by
    # construction, only ever proposes a real, catalog-priced, genuinely-
    # pricier-or-newly-added option, an empty LLM verdict is cross-checked
    # against it: if the heuristic finds a real action the LLM missed, that
    # action is authoritative and replaces the LLM's empty one (the
    # LLM-authored `pros`/`cons`/`within_budget` are kept — only
    # `stretch_budget` itself is swapped in, and `source` is corrected to
    # "heuristic" since the part that actually matters here came from
    # Python, not the model).
    response.source = "llm"
    result = response.model_dump()
    result["within_budget"] = _sanitize_within_budget(
        build_state, result["within_budget"], direction, mode, profile, quantities
    )
    result["stretch_budget"] = _sanitize_stretch(
        build_state, result["stretch_budget"], quantities
    )
    if not result["stretch_budget"]["actions"]:
        heuristic_stretch = _heuristic_stretch_budget(build_state, direction, mode, profile, quantities)
        if heuristic_stretch["actions"]:
            result["stretch_budget"] = heuristic_stretch
            result["source"] = "heuristic"
    return result
