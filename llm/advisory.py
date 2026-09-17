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
from llm.schemas import BuildAdvisoryResponse, QuantityAction, SwapAction

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

PEAKED-CATEGORY FALLTHROUGH RULE (applies to "stretch_budget" in every mode): If a specific category
(e.g. CPU) has peaked in socket/tier compatibility — no pricier compatible option exists for it —
evaluate GPU upgrades, increasing RAM quantity via a `set_quantity` action (up to the motherboard's
real DIMM slot count from catalog data), or increasing/upgrading Storage capacity, before concluding no
stretch upgrade exists.

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
def _heuristic_within_budget(build_state: BuildState, direction: str, mode: str, profile: str | None) -> dict:
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

    tried_swap_categories: set[str] = set()

    for candidate_category in (limiting_category, "GPU"):
        if candidate_category in tried_swap_categories or candidate_category not in build_state:
            continue
        tried_swap_categories.add(candidate_category)
        result = _try_swap_upgrade(build_state, candidate_category)
        if result is None:
            continue
        current, upgrade, delta = result
        reason = _stretch_reason(mode, profile, direction, candidate_category, limiting_category)
        explanation = f"Upgrade {candidate_category} to {upgrade.name} (+{delta:,.2f} USD) {reason}."
        return {
            "explanation": explanation,
            "actions": [{"action": "swap", "category": candidate_category, "replace_with_id": upgrade.id}],
            "added_cost_usd": round(delta, 2),
        }

    for quantity_category in ("RAM", "Storage"):
        result = _try_quantity_increment(build_state, quantity_category, quantities)
        if result is None:
            continue
        component, _current_qty, new_qty, added_cost = result
        explanation = _quantity_explanation(
            component, quantity_category, new_qty, mode, profile, direction, limiting_category
        )
        return {
            "explanation": explanation,
            "actions": [{"action": "set_quantity", "category": quantity_category, "quantity": new_qty}],
            "added_cost_usd": round(added_cost, 2),
        }

    result = _try_swap_upgrade(build_state, "Cooler")
    if result is not None:
        current, upgrade, delta = result
        reason = _stretch_reason(mode, profile, direction, "Cooler", limiting_category)
        explanation = f"Upgrade Cooler to {upgrade.name} (+{delta:,.2f} USD) {reason}."
        return {
            "explanation": explanation,
            "actions": [{"action": "swap", "category": "Cooler", "replace_with_id": upgrade.id}],
            "added_cost_usd": round(delta, 2),
        }

    explanation = (
        f"{build_state[limiting_category].name} is already the priciest compatible {limiting_category} "
        "option in the catalog, and GPU, RAM, Storage, and Cooler upgrades were checked too — no "
        "stretch-budget upgrade is available right now."
    )
    return {"explanation": explanation, "actions": [], "added_cost_usd": 0.0}


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
        "within_budget": _heuristic_within_budget(build_state, direction, mode, profile),
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


def _validate_advisory_actions(build_state: BuildState, response: BuildAdvisoryResponse) -> None:
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

    Raises AdvisoryUnavailableError on any violation so get_build_advisory's
    existing `except (AdvisoryUnavailableError, PydanticValidationError):`
    block funnels a hallucinated/invalid action into the same heuristic
    fallback as any other failure mode — never a separate code path."""
    valid_ids_by_category: dict[str, set[int]] = {}

    def _check_swap(swap: SwapAction) -> None:
        if swap.category not in build_state or swap.category not in solvers.CATEGORY_ORDER:
            raise AdvisoryUnavailableError(
                f"Advisory swap references an unknown/non-core category: {swap.category!r}"
            )
        if swap.category not in valid_ids_by_category:
            candidates = solvers.get_compatible_candidates(swap.category, build_state)
            valid_ids_by_category[swap.category] = {c.id for c in candidates}
        if swap.replace_with_id not in valid_ids_by_category[swap.category]:
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

    for action in response.stretch_budget.actions:
        if isinstance(action, QuantityAction):
            _check_quantity(action)
        else:
            _check_swap(action)


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
        _validate_advisory_actions(build_state, response)
    except (AdvisoryUnavailableError, PydanticValidationError):
        return _heuristic_advisory(build_state, direction, mode, profile, quantities)

    response.source = "llm"
    return response.model_dump()
