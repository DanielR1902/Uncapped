# engine/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §5 before editing here.

## Responsibility
Deterministic compatibility rules, the budget-constrained greedy solver, workload profile allocation, and all scoring math (compatibility %, bottleneck baseline, value/ratio index). This package is the single authority on "is this build valid" — nothing downstream may override its pass/fail verdicts.

## Files
- `compatibility.py` — the 8 rule functions from `spec.md` §5.1, `run_all_checks(build_state)`, and `evaluate_build(build_state) -> CompatibilityReport` (the `is_compatible`/`compatibility_score`/`issues` audit report). Each rule returns a `RuleResult{passed, message}` and only fires when both required components are present in `build_state`. This is the only module that owns the compatibility-score formula.
- `solvers.py` — all three creation-mode engines: `initialize_budget_build`/`on_user_pins_component` (Mode A, §5.2), `get_compatible_candidates`/`get_all_compatible_candidates` (Mode C free-custom filter, §5.2.1), and `allocate_workload_baseline` (Mode B, §5.6). Mode A must never return a selection that exceeds the ceiling. When the caller's own pinned/pre-selected parts alone make that impossible (over ceiling, or leaving less than `cheapest_fill_cost(...)` for what's left), both Mode A entry points call `_downgrade_pinned_until_feasible` to step the most expensive pinned *core* categories down to progressively cheaper compatible alternatives — never an exception, never a silent overspend. A pinned peripheral (a category outside `CATEGORY_ORDER`, riding along in `selection` because a user picked one before generating) is deliberately excluded from this pass — its cost is a fixed, off-the-top deduction from the core budget, not something this best-effort preservation step may swap out. An *empty* selection is untouched by this and degrades gracefully as before (see `_greedy_fill`'s docstring for the exact distinction). As a final safety net, `_greedy_fill` re-checks the total once more after its normal repair pass (`_enforce_ceiling` on just the solver-chosen categories); if still over the ceiling — a rare residual case where `cheapest_fill_cost`'s fixed-order estimate and `_enforce_ceiling`'s own descending-price repair order diverge under non-separable compatibility constraints, or a pinned peripheral alone makes it unsatisfiable — it re-runs `_enforce_ceiling` treating *every* category (pinned core categories AND any peripheral) as adjustable. If a FROM-SCRATCH (unpinned) build is still over the ceiling even after that, it falls back to `_true_minimum_build()` (see below) when that fits — `_enforce_ceiling` only ever swaps one category at a time and can get stuck above a ceiling that's only reachable by changing two categories together (verified on this seed catalog: Case+Cooler jointly reach $632, but $661 is as low as one-at-a-time swapping can get). The ceiling invariant outranks pin-preservation, peripherals included, once nothing else is left to trim. `minimum_possible_build_cost()`/`_true_minimum_build()` expose the TRUE floor via an exhaustive, compatibility-pruned branch-and-bound search — NOT `cheapest_fill_cost({}, CATEGORY_ORDER)`, which is greedy and can get stuck above the real minimum for the same one-category-at-a-time reason (verified: greedy finds $661, the true minimum is $632, a real and non-negotiable gap). This search takes ~2s against a realistically-sized catalog; `ui/views/create_build.py` caches the result in `st.session_state` so it only pays that cost once per session, not on every click. `initialize_budget_build(..., fill_peripherals_with_surplus=True)` opts into a Phase 2: after the 8 core categories are filled and within ceiling, `_fill_peripherals_with_surplus` spends whatever's left of the ceiling on `PERIPHERAL_CATEGORIES` (`NetworkCard`, `SoundCard`, `OpticalDrive` — the only peripheral categories that exist; there is no Monitor/Keyboard/Mouse category anywhere in this project), picking the single most expensive still-affordable option per category in that priority order. Defaults to `False` so every other caller's behavior is unchanged — only `ui/views/create_build.py`'s "Apply budget & generate build" opts in.
- `scoring.py` — `compatibility_score(build_state)` (thin delegator to `compatibility.evaluate_build`, not a re-implementation), `bottleneck_percentage_baseline(build_state)`, `value_index(component, category_candidates, build_state)` per `spec.md` §5.3–§5.5.

## Allowed imports
- `db.repositories.components_repo`, `db.repositories.workload_mappings_repo` (or equivalent) for **read-only** catalog access.
- Standard library only otherwise (no third-party math/ML deps needed at this scale).

## Forbidden
- No `streamlit` import, anywhere in this package — must remain runnable and testable with plain `pytest`, no UI runtime.
- No `httpx`/network calls — no imports from `llm/`. The LLM layer depends on `engine`, never the reverse.
- No imports from `ui/` or `auth/`.
- No writes to the database — this package reads the catalog and returns computed values; persistence of a finished build happens through `db.repositories.builds_repo`, called from `ui/`.

## Interface contract (used by both `ui/` and `llm/`)
```python
def run_all_checks(build_state: dict[str, Component]) -> list[RuleResult]: ...
def evaluate_build(build_state: dict[str, Component]) -> CompatibilityReport: ...  # {is_compatible, compatibility_score, issues, results}
def compatibility_score(build_state: dict[str, Component]) -> float: ...  # 0-100
def bottleneck_percentage_baseline(build_state: dict[str, Component]) -> tuple[float, str]: ...  # (pct, "CPU-bound"|"GPU-bound"|"Balanced")
def initialize_budget_build(ceiling: float, seed_selection: dict[str, Component] | None = None, fill_peripherals_with_surplus: bool = False) -> dict[str, Component]: ...  # auto-downgrades infeasible pins; never raises for this; opt-in peripheral surplus-fill defaults False (unchanged behavior) for every caller that doesn't pass True
def on_user_pins_component(selection: dict, category: str, component: Component, ceiling: float) -> dict[str, Component]: ...  # same auto-downgrade guarantee
def cheapest_fill_cost(build_state: dict[str, Component], categories: list[str]) -> float: ...  # cheapest compatible cost to fill the not-yet-present categories; single source of truth also used directly by ui/components/part_picker.py's reserve-threshold check (not a re-implementation)
def minimum_possible_build_cost() -> float: ...  # TRUE floor via exhaustive branch-and-bound search (NOT cheapest_fill_cost, which is greedy and can be higher than truly necessary) — used by ui/views/create_build.py to clamp+toast a too-low ceiling entry; ~2s, cache the result rather than calling on every interaction
def get_compatible_candidates(category: str, build_state: dict[str, Component]) -> list[Component]: ...
def get_all_compatible_candidates(build_state: dict[str, Component]) -> dict[str, list[Component]]: ...
def allocate_workload_baseline(profile: str, target_tier: str = "Mid") -> dict[str, Component]: ...
def value_index(component: Component, category_candidates: list[Component], build_state: dict) -> float: ...
def heuristic_synergy_score(compatibility_score: float, bottleneck_percentage: float) -> float: ...  # shared by llm/client.py's fallback and db/seed_demo.py
```
Named constants (safety multiplier `1.3`, baseline system draw `50` watts) live at module top-level in `compatibility.py`, not inlined.
