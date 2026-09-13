# llm/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §6 before editing here.

## Responsibility
All OpenRouter interaction: request construction, structured-output parsing/validation, prompt templates, and response caching. This package interprets and contextualizes deterministic results from `engine/` — it never computes or overrides compatibility/pass-fail verdicts itself.

## Files
- `client.py` — `httpx`-based OpenRouter wrapper. Reads `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` from env; no hardcoded model string committed to source. 15s timeout. `analyze_build(...)` is the only function `ui/` calls, and it **never raises** for an expected failure (missing key, timeout, connection error, non-2xx/429, schema-validation failure) — those all funnel through the internal `LLMUnavailableError` and come back out as a heuristic `BuildAnalysisResponse` (`source="heuristic"`) instead.
- `prompts.py` — the combined system prompt (synergy + bottleneck + insights in one call, per `spec.md` §6.3) and `build_request(...)`, which assembles `BuildAnalysisRequest` from a build state using `engine.compatibility`/`engine.scoring` pre-check results.
- `schemas.py` — `pydantic` models `BuildAnalysisRequest` and `BuildAnalysisResponse` (nesting `SynergyEvaluation`, `BottleneckAnalysis`, `ArchitecturalInsights`) matching `spec.md` §6.2/§6.4 exactly. All response parsing goes through these models; a validation failure is treated the same as an API failure (fallback, not a crash).
- `cache.py` — cache-key computation (`sha256` of `prompt_type|workload_profile|sorted(component_ids)`) and read/write against the `llm_cache` table via `db.repositories.llm_cache_repo`. Only a successful LLM response is cached — a heuristic fallback is recomputed fresh each call, never persisted, so it can't permanently shadow a later real call.

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
```
Bounding rule from `spec.md` §6.3: the LLM-refined `bottleneck.bottleneck_percentage` must stay within the request's `deterministic_precheck.baseline_bottleneck_percentage` ± 10 percentage points. This is enforced in `client.py::analyze_build` right after a successful parse (clamped against the request that produced it) — not in `schemas.py`, which has no access to the baseline it would need to check against.
