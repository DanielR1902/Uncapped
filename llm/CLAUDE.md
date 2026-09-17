# llm/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §6 before editing here.

## Responsibility
All OpenRouter interaction: request construction, structured-output parsing/validation, prompt templates, and response caching. This package interprets and contextualizes deterministic results from `engine/` — it never computes or overrides compatibility/pass-fail verdicts itself.

## Files
- `client.py` — `httpx`-based OpenRouter wrapper. Reads `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` from env; no hardcoded model string committed to source. 15s timeout. Request headers include `HTTP-Referer`/`X-Title` alongside `Authorization`/`Content-Type` (harmless, standard OpenRouter attribution headers — not required for the API to function, confirmed by direct testing against the live endpoint). A non-2xx/429 response prints its `response.text` to stderr before raising `LLMUnavailableError`, so a real failure's exact cause is visible immediately rather than only "HTTP 404" with no body. `analyze_build(...)` is the only function `ui/` calls, and it **never raises** for an expected failure (missing key, timeout, connection error, non-2xx/429, schema-validation failure) — those all funnel through the internal `LLMUnavailableError` and come back out as a heuristic `BuildAnalysisResponse` (`source="heuristic"`) instead.
- `prompts.py` — the combined system prompt (synergy + bottleneck + insights in one call, per `spec.md` §6.3) and `build_request(...)`, which assembles `BuildAnalysisRequest` from a build state using `engine.compatibility`/`engine.scoring` pre-check results.
- `schemas.py` — `pydantic` models `BuildAnalysisRequest` and `BuildAnalysisResponse` (nesting `SynergyEvaluation`, `BottleneckAnalysis`, `ArchitecturalInsights`) matching `spec.md` §6.2/§6.4 exactly. All response parsing goes through these models; a validation failure is treated the same as an API failure (fallback, not a crash).
- `cache.py` — cache-key computation (`sha256` of `prompt_type|workload_profile|sorted(component_ids)`) and read/write against the `llm_cache` table via `db.repositories.llm_cache_repo`. Only a successful LLM response is cached — a heuristic fallback is recomputed fresh each call, never persisted, so it can't permanently shadow a later real call.
- `concierge.py` — a third, independent OpenRouter-backed feature: `get_concierge_response(user_message, conversation_history, catalog_summary, community_summary, current_build_context=None) -> dict` is a conversational assistant answering catalog questions, recommending shared community builds, assembling a whole new build from scratch (`load_build`), patching the user's ALREADY-ACTIVE build with an incremental request like "add a network card" (`modify_build` — needs `current_build_context`, a caller-assembled snapshot of the active `build_draft`), or handling a pure page-navigation request (`navigate`, `navigate_to` constrained to this app's 3 real page keys by the schema's own `Literal` type). Follows `client.py`'s/`advisory.py`'s exact never-raises/`source`-tagged, single-shot pattern (its own internal `ConciergeUnavailableError`) — ONE system prompt + ONE payload + ONE `/chat/completions` call, deliberately not genuine multi-turn tool-calling (this project has never used OpenRouter's function-calling API and doesn't need it here). `catalog_summary`/`community_summary`/`current_build_context` are all pre-fetched/assembled by the CALLER (`ui/`) and passed in as plain arguments — this module never queries `db.repositories` or `engine/` itself, and never runs compatibility checks or real slot/budget quantity clamps on an action's contents (that's the caller's job after this returns, via `engine.compatibility`/`ui.state.resolve_effective_quantity_limit`). Zero-hallucination enforcement is authoritative in Python: `_validate_action` re-checks every `(category, id)` pair in a `load_build`/`modify_build` action's `components` against the real `catalog_summary` given, and every `modify_build.quantities` key against the `"RAM"`/`"Storage"`-only constraint, raising `ConciergeUnavailableError` (-> heuristic) on any mismatch, exactly like `advisory.py::_validate_advisory_actions`. A genuinely unexpected (non-anticipated) exception gets its full traceback logged to stderr before the same heuristic fallback, so a real bug is never silently invisible. The system prompt enforces a hard "at most 2 sentences" conciseness rule — prompt-only, no runtime rejection, since a slightly-long reply is a style issue, not a correctness one. Not persisted to `llm_cache` (a free-form chat message has no stable build-state cache key). See `spec.md` §6.7/§7.8.
- `advisory.py` — a second, independent OpenRouter-backed feature: `get_build_advisory(...)` returns pros/cons of the current build (`pros`, `cons`) plus a structured, **machine-executable** in-budget optimization tip (`within_budget`) and stretch-budget upgrade suggestion (`stretch_budget`) — `within_budget.swaps` is a plain list of `{"action": "swap", "category", "replace_with_id"}`; `stretch_budget.actions` is a DISCRIMINATED UNION of that same swap shape OR `{"action": "set_quantity", "category": "RAM"|"Storage", "quantity"}`, so `ui/`'s "Apply" buttons (§7.4) can act directly rather than parsing text — and a stretch recommendation is no longer confined to swapping the single bottleneck category (see below). `stretch_budget`'s system-prompt objective and heuristic (`_heuristic_stretch_budget`) both now fall through a priority chain — bottleneck category, then GPU, then RAM quantity+1, then Storage quantity+1 (NVMe only), then Cooler — stopping at the first real, catalog-priced option, instead of giving up the moment the single bottleneck category alone has no headroom (a real bug that existed before this). Separate from `client.py`'s synergy/bottleneck/compatibility scoring (different question — "what should I change" vs "how good is what I have" — kept as a separate module rather than folded into `client.py`, matching this package's one-concern-per-file pattern). Follows `client.py`'s exact never-raises/`source`-tagged pattern (its own internal `AdvisoryUnavailableError`, never `LLMUnavailableError`). All four top-level content fields (and every nested `WithinBudgetAdvice`/`StretchBudgetAdvice` field) are REQUIRED on `BuildAdvisoryResponse` (no defaults) — a payload missing any of them fails validation and falls to the heuristic path; `swaps` being an empty list is fine (no beneficial swap found), that's not itself a failure. **Zero-hallucination id enforcement is authoritative in Python, not just prompt wording**: `_validate_swap_ids` re-checks every `replace_with_id` against a fresh `engine.solvers.get_compatible_candidates(category, build_state)` call after parsing succeeds, raising `AdvisoryUnavailableError` (→ heuristic fallback) on any id/category that doesn't check out — the LLM is never trusted to have followed the "only real catalog ids" instruction on its own. Real RAM/Storage slot-count math (real per-module math for RAM, real NVMe-only `m2_slots` for Storage) is NOT recomputed here: `_real_slot_count` is a thin wrapper delegating to `engine.compatibility.resolve_quantity_limit`, the one place in the codebase that owns this math — used by `_validate_advisory_actions`'s `set_quantity` guard and `_try_quantity_increment`'s heuristic fallthrough alike, so the LLM/heuristic path can never suggest a quantity the compatibility engine would then reject. `SYSTEM_PROMPT` has a distinct optimization objective per `mode` (Budget: stay within an internally-computed `remaining_budget`, only cost-neutral/paired swaps once maxed; Workload: align with `workload_profile` rather than any ceiling; Free: bottleneck mitigation and CPU/GPU platform balance), plus a strict-arithmetic rule requiring every stated price delta to exactly match a real catalog difference — mirrored deterministically in the heuristic fallback's per-mode wording and real swap-finding (`_heuristic_within_budget`/`_heuristic_stretch_budget` take `mode`/`profile`, return the same structured shape, and `_heuristic_within_budget` additively looks for a paired upgrade for the limiting category affordable within the exact savings its downgrade freed up). `remaining_budget` is derived from `build_state` (never a new parameter) and is `0.0` outside Budget mode. The system prompt also instructs the model to never use a standalone `$` in prices (write "600 USD" instead of "$600"), since a matching `$...$` pair in one Streamlit markdown string triggers KaTeX/math-mode rendering; `ui.format.sanitize_markdown` (a shared helper, not private to one view — also guards free-text like a community post title) is a client-side backstop for the same issue (now applied to `explanation`, not a raw string). Grounds its prompt in REAL catalog alternatives (current pick + a couple of cheaper/pricier compatible neighbors per category, each with its real `id`) fetched via `engine.solvers.get_compatible_candidates` — never `db.repositories.components_repo` directly, staying inside this package's allowed-imports boundary the same way `client.py`'s heuristic fallback does. Unlike `client.py`'s result, **not** persisted to `llm_cache` — this feature only has UI-session-level caching (`ui/views/create_build.py`'s `advisory_cache`), a deliberate scope decision since `llm_cache`'s schema/cache-key scheme is shaped for `analyze_build`'s request/response, not this one.

## Allowed imports
- `httpx`, `pydantic`.
- `engine.*` — to build the deterministic pre-check payload (`evaluate_build`, `bottleneck_percentage_baseline`) and to compute the heuristic fallback. This is a **read-only, compute-only** dependency — `llm/` calls `engine/` functions, never the reverse.
- `db.repositories.llm_cache_repo` — **only** this repository. No access to `users`, `builds`, or `components` tables from this package beyond what `engine/` already read.

## Forbidden
- No `streamlit` import — this package must be testable with mocked HTTP and no UI runtime.
- No compatibility/scoring logic duplicated here — always consume `engine/`'s output, never recompute or contradict it.
- No live network calls in tests — mock `httpx` responses.
- No silent failures with no signal at all: every failure path still produces a `BuildAnalysisResponse` with `source="heuristic"`, so `ui/` can always tell a real LLM answer from an estimate — the difference from earlier drafts of this doc is that the signal is a field on the return value, not a raised exception `ui/` must catch.

## Interface contract
```python
# client.py
class LLMUnavailableError(Exception): ...  # internal; analyze_build catches it, never lets it escape
def analyze_build(
    build_state: dict[str, Component],
    workload_profile: str | None = None,
    budget_ceiling: float | None = None,
) -> BuildAnalysisResponse: ...  # never raises; response.source tells you "llm" vs "heuristic"

# prompts.py
def build_request(build_state, workload_profile=None, budget_ceiling=None) -> BuildAnalysisRequest: ...

# cache.py
def cache_key(build_state: dict[str, Component], workload_profile: str | None, prompt_type: str = "build_analysis") -> str: ...

# advisory.py
class AdvisoryUnavailableError(Exception): ...  # internal; get_build_advisory catches it, never lets it escape
def get_build_advisory(
    build_state: dict[str, Component],
    mode: str,                            # "Budget" | "Workload" | "Free"
    current_budget_or_cost: float,        # Budget-mode ceiling if mode == "Budget", else current total cost
    profile: str | None = None,
    bottleneck_info: dict | None = None,  # computed via engine.scoring if omitted
    quantities: dict[str, int] | None = None,  # current RAM/Storage quantity, default 1 per category if omitted
) -> dict: ...  # never raises; {"pros": [str, ...], "cons": [str, ...],
                 #  "within_budget": {"explanation": str, "swaps": [{"action": "swap", "category": str, "replace_with_id": int}, ...], "can_optimize_further": bool},
                 #  "stretch_budget": {"explanation": str, "actions": [{"action": "swap", "category": str, "replace_with_id": int} | {"action": "set_quantity", "category": "RAM"|"Storage", "quantity": int}, ...], "added_cost_usd": float},
                 #  "source": "llm"|"heuristic"}

# concierge.py
class ConciergeUnavailableError(Exception): ...  # internal; get_concierge_response catches it, never lets it escape
def get_concierge_response(
    user_message: str,
    conversation_history: list[dict],  # [{"role": "user"|"assistant", "content": str}, ...], most recent last
    catalog_summary: list[dict],       # ALL catalog components, pre-fetched by the caller (ui/)
    community_summary: list[dict],     # every currently-shared community post, pre-fetched by the caller (ui/)
    current_build_context: dict | None = None,  # {"mode", "components": {cat: {"id","name","price_usd"}}, "quantities"} snapshot of the active build_draft, or None
) -> dict: ...  # never raises; {"reply": str (<=2 sentences, prompt-only enforced),
                 #  "action": {"type": "load_build", "components": {category: id}, "explanation": str}
                 #          | {"type": "modify_build", "components": {category: id}, "quantities": {category: int}, "explanation": str}
                 #          | {"type": "navigate", "navigate_to": "create_build"|"my_builds"|"community"}
                 #          | None,
                 #  "source": "llm" | "heuristic"}
```
Bounding rule from `spec.md` §6.3: the LLM-refined `bottleneck.bottleneck_percentage` must stay within the request's `deterministic_precheck.baseline_bottleneck_percentage` ± 10 percentage points. This is enforced in `client.py::analyze_build` right after a successful parse (clamped against the request that produced it) — not in `schemas.py`, which has no access to the baseline it would need to check against. `concierge.py`'s zero-hallucination guard (`_validate_action`) is the analogous authoritative-in-Python check for its own feature, covering all three action types — see `spec.md` §6.7 for the full behavioral contract (five intents, heuristic fallback wording, traceback logging for unexpected failures).
