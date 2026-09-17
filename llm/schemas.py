"""Request/response pydantic models for the OpenRouter build-analysis call.

Matches spec.md §6.2/§6.4 exactly. All response parsing goes through
BuildAnalysisResponse — a validation failure here is treated the same as a
network failure by llm/client.py (heuristic fallback, never a crash).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComponentPayload(BaseModel):
    category: str
    name: str
    critical_specs: dict = Field(default_factory=dict)
    extended_specs: dict = Field(default_factory=dict)


class DeterministicPrecheck(BaseModel):
    compatibility_score: float = Field(ge=0, le=100)
    failed_rules: list[str] = Field(default_factory=list)
    baseline_bottleneck_percentage: float = Field(ge=0, le=100)
    bottleneck_direction: Literal["CPU-bound", "GPU-bound", "Balanced"]


class BuildAnalysisRequest(BaseModel):
    workload_profile: str | None = None
    budget_ceiling: float | None = None
    components: list[ComponentPayload]
    deterministic_precheck: DeterministicPrecheck


class SynergyEvaluation(BaseModel):
    overall_score: float = Field(ge=0, le=100)
    breakdown: dict[str, float] = Field(default_factory=dict)
    positive_synergies: list[str] = Field(default_factory=list)
    negative_conflicts: list[str] = Field(default_factory=list)


class ResolutionImpact(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    p1080: str = Field(alias="1080p")
    p1440: str = Field(alias="1440p")
    p4k: str = Field(alias="4K")


class BottleneckAnalysis(BaseModel):
    bottleneck_percentage: float = Field(ge=0, le=100)
    limiting_component: Literal["CPU", "GPU", "None"]
    resolution_impact: ResolutionImpact


class ArchitecturalInsights(BaseModel):
    summary: str
    upgrade_path: list[str] = Field(default_factory=list)
    quirks: list[str] = Field(default_factory=list)


class BuildAnalysisResponse(BaseModel):
    synergy: SynergyEvaluation
    bottleneck: BottleneckAnalysis
    insights: ArchitecturalInsights
    source: Literal["llm", "heuristic"] = "llm"


class SwapAction(BaseModel):
    """One machine-executable "replace the current pick in `category` with
    catalog component `replace_with_id`" instruction. Every id referenced
    here must be a real, currently-valid compatible-candidate id for that
    category — enforced both by the SYSTEM_PROMPT's zero-hallucination rule
    and, authoritatively, by llm/advisory.py's post-parse guard against
    engine.solvers.get_compatible_candidates (never trust the prompt alone).

    `action` defaults to "swap" so within_budget.swaps' existing construction
    (which never set this field) keeps working unmodified — it's also the
    discriminator tag used to distinguish this from QuantityAction inside
    StretchBudgetAdvice.actions' union."""

    action: Literal["swap"] = "swap"
    category: str
    replace_with_id: int


class QuantityAction(BaseModel):
    """One machine-executable "set the quantity of `category` to `quantity`"
    instruction — the RAM/Storage multi-slot analogue of SwapAction, only
    valid for the two quantity-eligible categories ("RAM", "Storage").
    `quantity` must never exceed the real motherboard slot count
    (ram_slots/m2_slots) — enforced authoritatively by
    llm/advisory.py's post-parse guard, never trusted from the prompt alone."""

    action: Literal["set_quantity"] = "set_quantity"
    category: str
    quantity: int


class WithinBudgetAdvice(BaseModel):
    """Structured in-budget optimization advice: free-text explanation plus
    zero or more concrete SwapActions a caller (a future "Apply" button) can
    execute directly, and whether the model believes a further beneficial
    swap remains after this one within the same mode-specific objective.

    Out of scope for the stretch_budget multi-category fix: unchanged field
    name/shape (plain SwapAction list only)."""

    explanation: str
    swaps: list[SwapAction]
    can_optimize_further: bool


class StretchBudgetAdvice(BaseModel):
    """Structured stretch-budget upgrade advice: free-text explanation plus
    zero or more concrete actions — either a SwapAction or a QuantityAction,
    discriminated by each model's `action` Literal field — and the exact
    numeric total of all actions' price deltas versus the current picks (not
    an estimate). Renamed from `swaps` to `actions` (and widened from
    list[SwapAction] to the union) so a stretch-budget upgrade recommendation
    is no longer limited to swapping the single bottleneck component — it can
    also propose increasing RAM/Storage quantity via a QuantityAction."""

    explanation: str
    actions: list[SwapAction | QuantityAction]
    added_cost_usd: float


class BuildAdvisoryResponse(BaseModel):
    """Response shape for the AI Build Advisory feature (pros/cons of the
    current build, one in-budget optimization tip, and one stretch-budget
    upgrade suggestion). See llm/advisory.py.

    pros/cons/within_budget/stretch_budget are all REQUIRED (no defaults),
    matching this project's existing precedent for "must always be present"
    LLM fields (BuildAnalysisResponse's synergy/bottleneck/insights): a real
    LLM response missing any of these four keys must fail validation here and
    fall through to the heuristic fallback, rather than silently succeeding
    with a half-empty result. within_budget/stretch_budget are nested
    WithinBudgetAdvice/StretchBudgetAdvice models (breaking change from the
    prior plain-string shape, same category of change as the earlier
    list->string migration on this same project) so advice is machine-
    executable, not just descriptive text. `swaps` may legitimately be an
    empty list (no beneficial swap exists) — that's not itself a validation
    failure, only a hallucinated id inside a non-empty swaps list is (see
    llm/advisory.py's post-parse guard)."""

    pros: list[str]
    cons: list[str]
    within_budget: WithinBudgetAdvice
    stretch_budget: StretchBudgetAdvice
    source: Literal["llm", "heuristic"] = "llm"
