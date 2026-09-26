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


class ConciergeLoadBuildAction(BaseModel):
    """One machine-executable "load these real catalog components into a new
    build" instruction produced by the Concierge chat feature (llm/concierge.py)
    in response to a "build me a PC with X" style request. `components` maps
    category -> a real catalog component id drawn ONLY from the
    `catalog_summary` the caller supplied — enforced both by the system
    prompt's zero-hallucination rule and, authoritatively, by
    llm/concierge.py's post-parse guard (never trust the prompt alone, same
    precedent as SwapAction/QuantityAction above)."""

    type: Literal["load_build"] = "load_build"
    components: dict[str, int]
    # Real, converted-to-USD working budget (see SYSTEM_PROMPT's BUDGET
    # CURRENCY CONVERSION rule) whenever the user's request stated a
    # ceiling/budget figure ("build me a PC for up to 12000 NIS") — `None`
    # for a plain, no-budget build-me request (the pre-existing, unchanged
    # behavior). This is NOT trusted for arithmetic on its own: the caller
    # (`ui/components/chat_assistant.py`) does not use this model's own
    # `components` picks as the final build when this field is set — it
    # treats `components` as, at most, a seed pin for any part the user
    # explicitly NAMED, and hands the real allocation to
    # `engine.solvers.initialize_budget_build`, which both guarantees the
    # hard ceiling (never exceeded, even by one currency unit) and is
    # empirically tested to converge toward — not just under — that ceiling.
    # Root CLAUDE.md's "compatibility is never LLM-gated" rule, applied here
    # to budget math: the model recognizes the ceiling, Python enforces it.
    budget_cap_usd: float | None = None
    # Optional, no functional consumer anywhere in ui/ — this is a purely
    # internal/audit note, never the user-facing text (that's `reply`, on
    # ConciergeResponse). Made optional after a real, confirmed regression:
    # the STRICT BREVITY RULE (SYSTEM_PROMPT) sometimes led the model to omit
    # this field entirely, which — back when it was required with no default
    # — failed Pydantic validation and fell back to the unhelpful heuristic
    # reply for what was otherwise a perfectly valid, well-formed action.
    explanation: str = ""


class ConciergeNavigateFilters(BaseModel):
    """Optional filter payload on a `navigate` action targeting `"community"`,
    mirroring `ui/views/community.py`'s REAL filter toolbar shape — a
    mutually-exclusive/cascading selection (one mode selectbox, then a
    mode-specific sub-filter), never a flat bag of 3 independently-meaningful
    fields. `build_type` picks which sub-filter (if any) is active;
    `max_price` is only meaningful when `build_type == "Budget"`, and
    `domain`/`tier` only when `build_type == "Workload"` — the caller
    (`ui/views/community.py`) simply ignores whichever field(s) don't apply
    to the chosen `build_type` rather than requiring this model to omit them.

    Deliberately a typed nested model (this project's existing convention for
    fixed-shape structured LLM output, e.g. `advisory.py`'s
    `WithinBudgetAdvice`/`StretchBudgetAdvice`) rather than a plain `dict`,
    so `build_type` gets Pydantic's own `Literal` enforcement for free — the
    exact same "no extra runtime check needed for this one field" precedent
    as `ConciergeNavigateAction.navigate_to` below. `domain`/`tier` stay plain
    `str | None` rather than a `Literal`/enum, though: unlike `build_type`
    (a fixed, closed set of 4 page-filter modes) or `load_build`/
    `modify_build`'s catalog ids, there is no fixed catalog of domains/tiers
    for a `Literal` to close over or for `llm/concierge.py::_validate_action`
    to hallucination-check against — the real, valid option strings are
    "whichever workload profiles/tiers happen to be currently shared right
    now," live data only `ui/views/community.py` computes at render time
    (its own `price_steps`/`domains`/`tiers` lists). `max_price` is a plain
    float for the same reason: the real selectable price-step STRINGS
    (`"$1,000"`, `"$1,500"`, ...) depend on the current most expensive shared
    Budget build, so this model only carries the requested numeric ceiling —
    `ui/views/community.py` resolves it to a real, currently-valid step
    string (or falls back gracefully to "All Prices"/"All" on no match),
    never trusting an LLM-authored string to already be one of its own
    selectbox's real options."""

    build_type: Literal["All", "Budget", "Workload", "Free"] = "All"
    max_price: float | None = None
    domain: str | None = None
    tier: str | None = None


class ConciergeNavigateAction(BaseModel):
    """A pure page-navigation intent (e.g. "take me to Community") — no build
    mutation involved. `navigate_to` is constrained to this app's real page
    keys by the Literal type itself (Pydantic rejects anything else at parse
    time, no extra runtime check needed for this one field).

    `filters` (optional, `None` when the request carries no build-type/
    domain/tier/price constraint) is only ever meaningful when
    `navigate_to == "community"` — see `ConciergeNavigateFilters`'s own
    docstring for its cascading shape and why it's a typed nested model.
    `ui/components/chat_assistant.py` stages a non-`None` `filters` payload
    into a one-shot session-state key for `ui/views/community.py` to consume
    on its next render, rather than writing directly to that view's own
    widget keys (see that module's docstring).

    NOTE on drafts: this action itself carries no draft-save field, and
    leaving `create_build` via `navigate` performs NO database write of any
    kind — `ui.state.teardown_builder()` (called from `ui/components/
    chat_assistant.py`'s `navigate` handler) only resets session state. This
    is the CURRENT of several designs this exact concern has gone through
    (spec.md §7.9): an LLM-controlled `save_as_draft: bool` field, then an
    explicit "Save Draft or Discard?" confirmation dialog, then a silent
    unconditional auto-save-on-exit — ALL removed by a later, explicit
    product decision. The "Save as draft" checkbox in `ui/views/
    create_build.py`'s manual Save UI is now the single, explicit source of
    truth for creating a draft; leaving the builder without using it simply
    discards the in-progress build.

    `reset_mode` (optional, default `False`) is only ever meaningful when
    `navigate_to == "create_build"` — it mirrors the existing "⬅ Change mode"
    button in `ui/views/create_build.py` (which also routes through
    `ui.state.teardown_builder()`), exposing that SAME mechanism via chat for
    a request like "let me select a new mode"/"choose a different mode"/
    "reset the builder". Deliberately not a new action type: conceptually
    this is still "navigate to create_build," just with an extra instruction
    to clear the in-progress pick set first."""

    type: Literal["navigate"] = "navigate"
    navigate_to: Literal["landing", "create_build", "my_builds", "community", "drafts"]
    filters: ConciergeNavigateFilters | None = None
    reset_mode: bool = False


class ConciergeModifyBuildAction(BaseModel):
    """An incremental patch to the user's CURRENTLY ACTIVE build draft (e.g.
    "add a network card and bump storage to 2") — as opposed to
    ConciergeLoadBuildAction, which describes a whole NEW build from scratch.
    `components` maps ONLY the categories being added/changed (never a full
    8-category set) to a real catalog id; `quantities` maps ONLY "RAM"/
    "Storage" to a requested count — the ACTUAL physical-slot/budget clamp on
    that number is NOT this module's job (llm/ has no engine/db access) —
    the caller (ui/) re-validates the real achievable quantity via
    engine.compatibility.resolve_quantity_limit /
    ui.state.resolve_effective_quantity_limit before applying, exactly like
    the manual quantity stepper already does. All three of `components`/
    `quantities`/`remove_categories` may be empty (e.g. a components-only,
    quantities-only, or removal-only patch) but not all empty AND no
    navigate_to — that combination means nothing was actually asked to
    change, which should just be a plain conversational reply with
    action: null instead of a no-op action.

    `remove_categories` (e.g. "remove the soundcard and network card") is a
    real, confirmed gap this closes: before this field existed, there was NO
    way to express "clear this category entirely" — `components` only ever
    maps a category to a real catalog id, never `null`/`None`, so a live call
    asked to remove several peripherals was caught trying exactly that
    (`{"SoundCard": None, "OpticalDrive": None, ...}`), which Pydantic
    correctly rejects (an `int` field can't hold `None`), silently falling
    back to the generic "having trouble reaching the AI assistant" reply —
    not a crash, but indistinguishable from one to the user, for a request
    that should have worked. A category named in BOTH `components` and
    `remove_categories` is a contradiction the caller resolves by preferring
    the removal (clearing wins) — the model should never do this on purpose,
    but the caller doesn't trust that either."""

    type: Literal["modify_build"] = "modify_build"
    components: dict[str, int] = {}
    quantities: dict[str, int] = {}
    remove_categories: list[str] = []
    # Optional — see ConciergeLoadBuildAction's identical field for why: no
    # functional consumer in ui/, and a real, confirmed regression where the
    # STRICT BREVITY RULE led the model to sometimes omit it, failing
    # validation for an otherwise well-formed quantity/component change.
    explanation: str = ""


class ConciergeSaveBuildAction(BaseModel):
    """The user's explicit request to persist their CURRENTLY ACTIVE build
    draft (`current_build_context`) to the database, e.g. "Save this PC to my
    list". This is the chat equivalent of clicking "Save build" in
    `ui/views/create_build.py`'s normal flow, and is acceptable as a direct
    database write triggered from chat specifically because it's the user's
    own explicit request acting on their own account's own data.

    INTERACTIVE-FIRST DESIGN (reversed from an earlier "zero-click" draft of
    this action): the model must NOT return this action on the very first
    "save this build" message. It must first ASK the user for both `name`
    and `destination` and wait for their answer across one or more turns
    (see SYSTEM_PROMPT's SAVE & PUBLISH REQUESTS intent for the exact
    two-question flow) — only once the user has actually supplied both
    pieces of information does this action get returned, carrying the
    user's own literal answers. No more auto-naming (the old
    `"Concierge Build – {date} {time}"` default) and no more silently
    always targeting the real `builds` table.

    `name` and `destination` are both REQUIRED fields with no default. This
    is a deliberate, load-bearing schema choice, not just a prompt
    instruction: if the model tries to return this action before it has
    genuinely gathered a name/destination from the user (e.g. it omits the
    field, or the prompt-only instruction above is ignored), Pydantic
    validation fails outright and `get_concierge_response` falls back to the
    heuristic response — the exact same "Python enforces what a prompt
    instruction alone can't guarantee" precedent as this module's other
    zero-hallucination guards. An `Optional[str] = None` field would let a
    non-compliant model return the action anyway with a missing/empty name
    and only rely on prompt wording to prevent that; a required field makes
    "the model must have asked and received an answer first" a structural
    guarantee enforced at parse time, not a hope.

    `destination` is constrained to the two real, currently-existing
    persistence targets in this app's actual schema: `"draft"` (the real
    `draft_builds` table, via `db.repositories.drafts_repo.save_draft`) or
    `"build"` (the real `builds` table, via
    `db.repositories.builds_repo.create_build`) — there is no `saved_builds`
    table anywhere in this app.

    `source`/`source_post_id` (both optional, defaulting to `"studio"`/`None`)
    are a SECOND, independent refinement (alongside `publish_immediately`):
    which build's data actually gets persisted. `source: "studio"` (the
    default, and the ONLY behavior that existed before this refinement) acts
    on `current_build_context` exactly as this class's main docstring
    describes — the user's own active, uncommitted Studio `build_draft`.
    `source: "community"` instead persists a specific, ALREADY-shared
    `CommunityPost`'s build — for when the user asks to save/clone/add-to-my-
    drafts a build they are VIEWING on the Community page, not their own
    in-progress Studio build (e.g. "save the build I'm looking at to my
    drafts", "clone this community build as a saved build too") — see
    SYSTEM_PROMPT's SAVE & PUBLISH REQUESTS intent's SOURCE RESOLUTION rule
    for exactly when this applies. `source_post_id` (the real `post_id` of
    that `CommunityPost`, resolved the SAME way `ConciergeLoadSavedBuildAction`
    resolves its own community `id` — via `viewed_post_id` or a name match
    against `community_summary`) is REQUIRED whenever `source == "community"`
    and is cross-checked by `_validate_action` against the real `"post_id"`
    values in `community_summary`, the same zero-hallucination precedent as
    `open_community_build`/`load_saved_build`. It is simply ignored when
    `source == "studio"` (nothing to check there — see `_validate_action`'s
    docstring). Carries no other new catalog ids — for either source, the
    actual component/quantity data comes from already-known-real rows this
    module was given (`current_build_context` for `"studio"`, the community
    post's own persisted `Build` row for `"community"`), never something the
    model asserts about individual parts."""

    type: Literal["save_build"] = "save_build"
    name: str
    destination: Literal["draft", "build"]
    # FAST-TRACK refinement (only meaningful when destination == "build" —
    # a draft has no publish path anywhere in this app's real architecture,
    # per this class's own docstring): true only when the user's OWN message
    # that triggered this action ALSO explicitly asked to publish/share to
    # Community in the same breath (e.g. "save build as X and publish it to
    # the community"), never inferred or guessed. Lets `save_build` and the
    # publish step both apply in ONE turn without a second round-trip,
    # instead of requiring the normal "Saved! Would you like to publish it
    # too?" follow-up question — see SYSTEM_PROMPT's SAVE & PUBLISH REQUESTS
    # intent for exactly which phrasing qualifies. `author_notes` (only
    # meaningful together with `publish_immediately: true`) carries the
    # user's own verbatim description text ONLY if they also supplied one in
    # that same message; left `null` otherwise (never invented) — the model
    # does NOT compose one for this fast-track path (unlike the normal
    # publish flow's later, explicit "generate one for me" request), since
    # nothing here asked it to.
    publish_immediately: bool = False
    author_notes: str | None = None
    # Publication tag/status (spec.md §3.6/§3.7, db.models.CommunityPost.flair
    # — the SAME column `ui/views/create_build.py`'s own "Share / Rate My
    # Build" dialog and "Also publish to Community" checkbox now write to,
    # via `db.repositories.community_repo.create_post`'s existing `flair`
    # param). Only ever meaningful together with `publish_immediately: true`
    # (a "draft" destination never publishes, and an ordinary non-fast-track
    # publish captures its own flair on the LATER `publish_build` turn
    # instead — see that action's own `flair` field). REQUIRED to be
    # non-null whenever `publish_immediately` is true: the model must NEVER
    # publish "silently" with no tag — if the user's fast-track message
    # didn't also name one, `publish_immediately` itself must stay `false`
    # and the flair question asked as a follow-up instead (see SYSTEM_
    # PROMPT's SAVE & PUBLISH REQUESTS intent).
    flair: Literal["Rate My Build", "Looking for Help"] | None = None
    # Which build's data gets persisted — see this class's own docstring for
    # the full explanation. `source_post_id` is only meaningful (and only
    # cross-checked by `_validate_action`) when `source == "community"`.
    source: Literal["studio", "community"] = "studio"
    source_post_id: int | None = None
    # Optional — see ConciergeLoadBuildAction's identical field for why.
    # `name`/`destination` stay REQUIRED (the actual load-bearing fields for
    # this action's whole interactive-first design) — only this vestigial,
    # unconsumed note is relaxed.
    explanation: str = ""


class ConciergePublishBuildAction(BaseModel):
    """The user's confirmation, on a LATER turn (after the Concierge asked
    "would you like to publish it to the Community as well?" in response to a
    prior `save_build`), that the just-saved build should be shared to the
    community. Resolved via the same "read your own immediately-preceding
    turn in `conversation_history`" discipline as the budget guardrail — see
    SYSTEM_PROMPT's SAVE & PUBLISH REQUESTS intent.

    Deliberately carries NO build id: the model cannot know which real
    database row a prior `save_build` action produced (that id only exists
    once `ui/` actually calls `builds_repo.create_build`, entirely outside
    this module's visibility). The caller (`ui/`) resolves which build to
    publish via its own `st.session_state["concierge_last_saved_build"]` key,
    set when that earlier `save_build` action was applied — never re-derived
    or guessed here.

    `author_notes` is optional free text for the "would you like to add a
    description?" follow-up branch — `None` when the user declined to add
    one, or a real value on the specific later turn where the user typed the
    actual description text.

    `flair` is REQUIRED (no default) — the model must have already asked
    "Would you like to publish this as 'Rate My Build' or 'Looking for
    Help'?" and received one of those two answers on an earlier turn of this
    SAME publish flow before this action can ever be returned (see SYSTEM_
    PROMPT's SAVE & PUBLISH REQUESTS intent) — the same "a required field
    makes it a structural guarantee, not just a prompt hope" precedent as
    `ConciergeSaveBuildAction.name`/`.destination`. This is what makes "never
    publish silently with no tag" an enforced guarantee: a non-compliant
    response missing this field fails Pydantic validation outright and falls
    back to the heuristic reply instead of silently publishing untagged."""

    type: Literal["publish_build"] = "publish_build"
    author_notes: str | None = None
    flair: Literal["Rate My Build", "Looking for Help"]


class ConciergeOpenCommunityBuildAction(BaseModel):
    """A request to deep-link straight to ONE specific, already-shared
    community build's thread view (e.g. "open the build we just submitted",
    "show build Weekend Gaming Rig", "view my Gaming Rig from community") —
    as opposed to intent 2's COMMUNITY RECOMMENDATIONS, which merely
    describes/recommends posts in `reply` without navigating anywhere.

    WHY `post_id`, NOT `build_id`: the real view-routing mechanism this
    action drives is `ui/views/community.py::render()`'s existing
    `st.session_state.get("selected_post_id")` check at the very top of that
    function — when set, it resolves that id via `community_repo.get_post`
    and renders `_thread_view(post)` directly instead of the feed list. That
    lookup is keyed on a `CommunityPost.id` (a "post id"), never a
    `Build.id` — a `CommunityPost` and the `Build` it wraps are two distinct
    rows with two distinct id sequences (see `db/models.py::CommunityPost`,
    which stores its own `id` plus a separate `build_id` foreign key).
    Carrying `build_id` here instead would force the caller (`ui/`) to
    re-look-up the matching post via `community_repo` before it could set
    `selected_post_id` — genuinely redundant work, since `community_summary`
    (already handed to this model on every call, see
    `ui/components/chat_assistant.py::_community_summary`) already includes
    BOTH `post_id` and `build_id` for every currently-shared post. `post_id`
    is therefore the correct, zero-extra-lookup field: the model resolves
    "the build we just submitted" / "show build {name}" directly against
    `community_summary`'s real `post_id` values (by title match, or via
    whatever recency signal `community_summary` actually carries — see
    `SYSTEM_PROMPT`'s own intent for what it can and can't infer), and the
    caller uses that id completely unchanged.

    ZERO-HALLUCINATION: `post_id` must be a real value already present in
    the `community_summary` this model was given for this call — enforced
    both by the system prompt's instruction and, authoritatively, by
    `llm/concierge.py::_validate_action`'s post-parse guard (never trust the
    prompt alone, the same precedent as every other action type's id
    cross-check in this module). If nothing in `community_summary` matches
    what the user described, the model must say so plainly in `reply` and
    return `action: null` instead of inventing a `post_id`."""

    type: Literal["open_community_build"] = "open_community_build"
    post_id: int


class ConciergeLoadSavedBuildAction(BaseModel):
    """Loads an EXISTING, already-persisted draft, previously-saved build, or
    community post's build directly into the Build Studio for viewing or
    editing (e.g. "open pc-master-race for editing", "load my draft Beast
    Rig", "edit this build") — distinct from `ConciergeLoadBuildAction`
    (which assembles a brand NEW build from named catalog parts, never an
    existing saved row) and from `ConciergeOpenCommunityBuildAction` (which
    deep-links to a post's READ-ONLY thread view, never into the editable
    studio).

    `source` + `id` together name exactly one real, already-persisted row —
    the model never invents either. ZERO-HALLUCINATION: `id` must already be
    present in whichever caller-supplied summary matches `source` —
    `drafts_summary` (`"draft_id"` field) for `"draft"`,
    `previous_builds_summary` (`"build_id"` field) for `"build"`, or
    `community_summary` (`"post_id"` field, the SAME field
    `ConciergeOpenCommunityBuildAction` uses) for `"community"` — enforced
    both by the system prompt's instruction and, authoritatively, by
    `llm/concierge.py::_validate_action`'s post-parse guard, the same
    precedent as every other action type's id cross-check in this module. If
    nothing in the relevant summary matches what the user described (or the
    request is ambiguous — no name given while multiple candidates exist),
    the model must say so plainly in `reply` and return `action: null`
    instead of inventing an id."""

    type: Literal["load_saved_build"] = "load_saved_build"
    source: Literal["draft", "build", "community"]
    id: int


class ConciergeFixWarningsAction(BaseModel):
    """The user's explicit request to resolve the CURRENTLY ACTIVE build's
    real compatibility issues (e.g. "fix the warnings in my build", "resolve
    the compatibility issue") — requires `current_build_context` to carry a
    non-empty `compatibility_issues` list (see llm/concierge.py's SYSTEM_
    PROMPT); if there's no active build, or it has no real issues right now,
    the model must say so plainly in `reply` and return `action: null`
    instead. Carries NO LLM-asserted catalog id or category — per this
    project's own architecture rule that compatibility is never LLM-gated
    (root CLAUDE.md), the actual fix is computed entirely by
    `engine.solvers.resolve_compatibility_issues` (deterministic, catalog-
    grounded, re-validated against `engine.compatibility` after every
    candidate swap); this action only carries the RECOGNIZED INTENT, exactly
    like `ConciergeNavigateAction`/`ConciergePublishBuildAction` are pure
    pass-throughs with nothing for `_validate_action` to zero-hallucination-
    check."""

    type: Literal["fix_warnings"] = "fix_warnings"
    explanation: str = ""


class ConciergeOptimizeBottleneckAction(BaseModel):
    """The user's explicit request to reduce the CURRENTLY ACTIVE build's
    bottleneck percentage (e.g. "optimize the bottleneck", "reduce the
    bottleneck", "rebalance my CPU and GPU") — requires `current_build_
    context` to carry a `bottleneck` reading above the target (see
    llm/concierge.py SYSTEM_PROMPT); if there's no active build, or it's
    already well-balanced, the model must say so plainly in `reply` and
    return `action: null` instead. Carries NO LLM-asserted catalog id or
    category — the actual rebalancing swap comes from
    `llm.advisory.get_build_advisory`'s own already-zero-hallucination-
    validated `within_budget.swaps` (Free mode's own stated optimization
    objective is exactly "bottleneck mitigation and CPU/GPU platform
    balance", llm/CLAUDE.md), applied by the caller — never a fresh part
    choice invented by THIS response. A pure pass-through, same reasoning as
    `ConciergeFixWarningsAction` above."""

    type: Literal["optimize_bottleneck"] = "optimize_bottleneck"
    # The user's own explicit target ceiling, when they stated one (e.g. "get
    # it under 10%", "reduce the bottleneck to below 8 percent") — a plain
    # percentage number, never a fraction (10.0, not 0.10). `None` when no
    # explicit number was given; the caller falls back to a sensible default
    # (see ui/components/chat_assistant.py's own constant) rather than
    # guessing a number here.
    target_percentage: float | None = None
    explanation: str = ""


class ConciergeUseRemainingBudgetAction(BaseModel):
    """The user's explicit request to spend whatever budget headroom is left
    on the CURRENTLY ACTIVE build (e.g. "is there any upgrade possible within
    my budget?", "how much can you add without going over budget?", "upgrade
    what you can with the remaining budget", "you have 13000 NIS, upgrade it
    accordingly") — requires EITHER `current_build_context["mode"] ==
    "Budget"` with a real numeric ceiling already set, OR a fresh ceiling
    figure stated in THIS message (`budget_cap_usd` below); if neither
    applies, or the build is already effectively maxed out, the model must
    say so plainly in `reply` and return `action: null` instead.

    Carries NO LLM-asserted catalog id, category, or price delta — a real,
    confirmed failure mode this replaces: the model is NOT reliable at
    computing "current total + delta <= budget cap" arithmetic itself (it has
    both falsely rejected a real, affordable upgrade and, separately,
    proposed a single small upgrade while leaving hundreds of real currency
    units of headroom unspent). The actual multi-tier upgrade sequence — CPU/
    GPU swap, then RAM capacity, then Storage volume, then Cooler/PSU
    headroom, exactly mirroring `llm.advisory.py`'s own priority-ordered
    `stretch_budget` fallthrough chain — is computed entirely in Python by
    `ui/components/chat_assistant.py::_apply_concierge_action`, which calls
    `llm.advisory.get_build_advisory` in a small bounded loop (mirroring
    `ConciergeOptimizeBottleneckAction`'s own verification loop exactly),
    applying one real, catalog-priced `stretch_budget` action per round and
    re-checking the real remaining headroom after each one, until either the
    headroom is exhausted or no further real upgrade exists. This action only
    carries the RECOGNIZED INTENT — a pure pass-through, same reasoning as
    `ConciergeFixWarningsAction`/`ConciergeOptimizeBottleneckAction` above."""

    type: Literal["use_remaining_budget"] = "use_remaining_budget"
    # Real, converted-to-USD ceiling the user stated FRESH in this message
    # (e.g. "you have 13000 NIS, upgrade it accordingly" against a build that
    # was never in Budget mode at all) — the SAME BUDGET CURRENCY CONVERSION
    # mechanism `ConciergeLoadBuildAction.budget_cap_usd` uses. `None` when
    # the user didn't state a new figure, in which case the caller falls
    # back to `current_build_context`'s own EXISTING `budget_ceiling` (only
    # meaningful when `mode == "Budget"` already). When this IS set, the
    # caller converts/keeps the active build in Budget mode under this
    # ceiling going forward, even if it was previously Free/Workload mode.
    budget_cap_usd: float | None = None
    explanation: str = ""


class ConciergeRebalanceBudgetAction(BaseModel):
    """The user's explicit request to fund an upgrade by downgrading a
    DIFFERENT, named category first (e.g. "downgrade the screen a bit and
    use the money to upgrade the cpu and gpu", "step down the monitor and
    put the savings into a better GPU") — a real, confirmed gap this closes:
    a live call was caught either refusing this combined request outright or
    (more often) applying it wildly inconsistently — sometimes leaving the
    named upgrade categories completely untouched despite a real downgrade
    freeing up real money, sometimes downgrading far more than any upgrade
    recouped — because the model was trusted to invent BOTH which specific
    part to downgrade to AND which specific parts to upgrade to AND the
    exact price arithmetic connecting them, all at once, in one JSON
    response. This action reverses that: the model's ONLY job is category
    RECOGNITION (which category is being downgraded, which are being
    upgraded — including common synonyms: "screen"/"display" -> "Monitor",
    "graphics card"/"video card" -> "GPU", "processor" -> "CPU" — never
    demanding the user name an exact model). `engine.solvers.
    rebalance_budget` then deterministically steps `downgrade_category` down
    one real tier (the priciest real compatible option still cheaper than
    the current pick), computes the real freed cash, and spends it — plus
    any budget headroom that already existed — upgrading each of
    `upgrade_categories`, in the given order, to the priciest real
    compatible option that still fits, never exceeding the ceiling. Requires
    an active build; if `downgrade_category`/`upgrade_categories` name
    anything not a real core (`engine.solvers.CATEGORY_ORDER`) or peripheral
    (`engine.solvers.PERIPHERAL_CATEGORIES`) category, or the named
    downgrade category isn't currently selected, `_validate_action` rejects
    it (falls back to a plain informational reply) rather than guessing."""

    type: Literal["rebalance_budget"] = "rebalance_budget"
    downgrade_category: str
    upgrade_categories: list[str]
    explanation: str = ""


class ConciergeResponse(BaseModel):
    """Response shape for the Concierge chat feature (see llm/concierge.py).
    `reply` is always present (conversational answer to the user's message).
    `action` is populated for a "build me a PC" style request that resolved to
    real catalog ids (`load_build`), an incremental patch to the active build
    draft (`modify_build`), a pure page-navigation intent (`navigate` —
    optionally carrying a `filters` payload for a "community" destination,
    see `ConciergeNavigateAction`), an explicit "save my build" request
    (`save_build`, only returned once the model has actually gathered BOTH a
    `name` and a `destination` from the user across one or more turns — see
    `ConciergeSaveBuildAction`'s docstring), a later-turn confirmation to
    publish a just-saved "build"-destination save (`publish_build`), a
    request to deep-link straight to one specific, already-shared community
    build's thread view (`open_community_build`, resolved against
    `community_summary`'s real `post_id` values — see
    `ConciergeOpenCommunityBuildAction`'s docstring), or a request to load an
    EXISTING draft/saved build/community post directly into the Build Studio
    for editing (`load_saved_build`, resolved against `drafts_summary`/
    `previous_builds_summary`/`community_summary` — see
    `ConciergeLoadSavedBuildAction`'s docstring), a request to resolve the
    active build's real compatibility issues (`fix_warnings` — requires a
    non-empty `current_build_context["compatibility_issues"]`; the actual
    fix is computed by `engine.solvers.resolve_compatibility_issues`, never
    an LLM-chosen part — see `ConciergeFixWarningsAction`'s docstring), a
    request to reduce the active build's bottleneck percentage
    (`optimize_bottleneck` — the actual rebalancing swap comes from
    `llm.advisory.get_build_advisory`'s own zero-hallucination-validated
    `stretch_budget.actions` — see `ConciergeOptimizeBottleneckAction`'s
    docstring), or a request to spend remaining Budget-mode headroom
    (`use_remaining_budget` — a bounded, multi-tier (CPU/GPU -> RAM ->
    Storage -> Cooler/PSU) upgrade sequence computed entirely in Python, same
    zero-LLM-arithmetic precedent — see `ConciergeUseRemainingBudgetAction`'s
    docstring); it is `None` for catalog-question, community-recommendation,
    and optimization/analysis intents, and also `None` (with `reply` saying
    so) when a named part could
    not be found in `catalog_summary` at all, when a modify/save request has
    no active build to act on, when no post in `community_summary` matches a
    requested deep-link target, when no item in the relevant summary matches
    a requested load-into-studio target (or the request is ambiguous), or
    when a mid-flow reply is still gathering information (e.g. asking for
    the still-missing name or destination, asking whether to publish, or
    asking for a description) before there's anything to act on yet, or
    when a `fix_warnings`/`optimize_bottleneck`/`use_remaining_budget` request
    has no active build, no real issues/ceiling, or is already
    acceptable/maxed-out.

    `currency_switch` (optional, default `None`) is a SEPARATE, top-level
    field — deliberately NOT nested inside `action` — because it must be able
    to co-occur with ANY action type (or `None`): a build-me request can name
    a budget in a different currency than the one currently active ("build me
    a gaming PC for 10000 NIS" while `active_currency == "USD"`) in the exact
    same turn it returns a `load_build` action, and a bare "switch to NIS"/"I
    asked it to be in NIS" request has no OTHER action at all. Set it to the
    real currency code the user explicitly named in THIS message — never
    guessed, never defaulted to `active_currency`, and left `None` whenever no
    currency is mentioned at all, even if the user's wording is otherwise
    currency-adjacent (e.g. a bare "what's the total?" with no currency named
    leaves this `None` — see llm/concierge.py SYSTEM_PROMPT's CURRENCY SWITCH
    REQUESTS rule for the exact extraction wording recognized). The caller
    (`ui/`) applies this by updating `st.session_state["selected_currency"]`
    and forcing a rerun so the sidebar selector and every price on screen
    switch immediately — this module itself never touches session state."""

    reply: str
    action: (
        ConciergeLoadBuildAction
        | ConciergeModifyBuildAction
        | ConciergeNavigateAction
        | ConciergeSaveBuildAction
        | ConciergePublishBuildAction
        | ConciergeOpenCommunityBuildAction
        | ConciergeLoadSavedBuildAction
        | ConciergeFixWarningsAction
        | ConciergeOptimizeBottleneckAction
        | ConciergeUseRemainingBudgetAction
        | ConciergeRebalanceBudgetAction
        | None
    ) = None
    currency_switch: Literal["USD", "EUR", "NIS"] | None = None
    source: Literal["llm", "heuristic"] = "llm"


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
