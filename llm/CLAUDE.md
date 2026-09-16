# llm/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §6 before editing here.

## Responsibility
All OpenRouter interaction: request construction, structured-output parsing/validation, prompt templates, and response caching. This package interprets and contextualizes deterministic results from `engine/` — it never computes or overrides compatibility/pass-fail verdicts itself.

## Files
- `client.py` — `httpx`-based OpenRouter wrapper. Reads `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` from env; no hardcoded model string committed to source. 15s timeout. Request headers include `HTTP-Referer`/`X-Title` alongside `Authorization`/`Content-Type` (harmless, standard OpenRouter attribution headers — not required for the API to function, confirmed by direct testing against the live endpoint). A non-2xx/429 response prints its `response.text` to stderr before raising `LLMUnavailableError`, so a real failure's exact cause is visible immediately rather than only "HTTP 404" with no body. `analyze_build(...)` is the only function `ui/` calls, and it **never raises** for an expected failure (missing key, timeout, connection error, non-2xx/429, schema-validation failure) — those all funnel through the internal `LLMUnavailableError` and come back out as a heuristic `BuildAnalysisResponse` (`source="heuristic"`) instead.
- `prompts.py` — the combined system prompt (synergy + bottleneck + insights in one call, per `spec.md` §6.3) and `build_request(...)`, which assembles `BuildAnalysisRequest` from a build state using `engine.compatibility`/`engine.scoring` pre-check results.
- `schemas.py` — `pydantic` models `BuildAnalysisRequest` and `BuildAnalysisResponse` (nesting `SynergyEvaluation`, `BottleneckAnalysis`, `ArchitecturalInsights`) matching `spec.md` §6.2/§6.4 exactly. All response parsing goes through these models; a validation failure is treated the same as an API failure (fallback, not a crash).
- `cache.py` — cache-key computation (`sha256` of `prompt_type|workload_profile|sorted(component_ids)`) and read/write against the `llm_cache` table via `db.repositories.llm_cache_repo`. Only a successful LLM response is cached — a heuristic fallback is recomputed fresh each call, never persisted, so it can't permanently shadow a later real call.
- `advisory.py` — a second, independent OpenRouter-backed feature: `get_build_advisory(...)` returns pros/cons of the current build (`pros`, `cons`) plus a single in-budget optimization tip (`within_budget`) and a single stretch-budget upgrade suggestion (`stretch_budget`), separate from `client.py`'s synergy/bottleneck/compatibility scoring (different question — "what should I change" vs "how good is what I have" — kept as a separate module rather than folded into `client.py`, matching this package's one-concern-per-file pattern). Follows `client.py`'s exact never-raises/`source`-tagged pattern (its own internal `AdvisoryUnavailableError`, never `LLMUnavailableError`). All four content fields are REQUIRED on `BuildAdvisoryResponse` (no defaults) — a payload missing any of them fails validation and falls to the heuristic path. `SYSTEM_PROMPT` has a distinct optimization objective per `mode` (Budget: stay within an internally-computed `remaining_budget`, only cost-neutral/paired swaps once maxed; Workload: align with `workload_profile` rather than any ceiling; Free: bottleneck mitigation and CPU/GPU platform balance), plus a strict-arithmetic rule requiring every stated price delta to exactly match a real catalog difference — mirrored deterministically in the heuristic fallback's per-mode wording (`_heuristic_within_budget`/`_heuristic_stretch_budget` take `mode`/`profile`). `remaining_budget` is derived from `build_state` (never a new parameter) and is `0.0` outside Budget mode. The system prompt also instructs the model to never use a standalone `$` in prices (write "600 USD" instead of "$600"), since a matching `$...$` pair in one Streamlit markdown string triggers KaTeX/math-mode rendering; `ui/views/create_build.py::_sanitize_markdown` is a client-side backstop for the same issue. Grounds its prompt in REAL catalog alternatives (current pick + a couple of cheaper/pricier compatible neighbors per category) fetched via `engine.solvers.get_compatible_candidates` — never `db.repositories.components_repo` directly, staying inside this package's allowed-imports boundary the same way `client.py`'s heuristic fallback does. Unlike `client.py`'s result, **not** persisted to `llm_cache` — this feature only has UI-session-level caching (`ui/views/create_build.py`'s `advisory_cache`), a deliberate scope decision since `llm_cache`'s schema/cache-key scheme is shaped for `analyze_build`'s request/response, not this one.

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
) -> dict: ...  # never raises; {"pros": [str, ...], "cons": [str, ...], "within_budget": str, "stretch_budget": str, "source": "llm"|"heuristic"}
```
Bounding rule from `spec.md` §6.3: the LLM-refined `bottleneck.bottleneck_percentage` must stay within the request's `deterministic_precheck.baseline_bottleneck_percentage` ± 10 percentage points. This is enforced in `client.py::analyze_build` right after a successful parse (clamped against the request that produced it) — not in `schemas.py`, which has no access to the baseline it would need to check against.
