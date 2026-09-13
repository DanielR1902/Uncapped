# engine/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §5 before editing here.

## Responsibility
Deterministic compatibility rules, the budget-constrained greedy solver, workload profile allocation, and all scoring math (compatibility %, bottleneck baseline, value/ratio index). This package is the single authority on "is this build valid" — nothing downstream may override its pass/fail verdicts.

## Files
- `compatibility.py` — the 8 rule functions from `spec.md` §5.1, `run_all_checks(build_state)`, and `evaluate_build(build_state) -> CompatibilityReport` (the `is_compatible`/`compatibility_score`/`issues` audit report). Each rule returns a `RuleResult{passed, message}` and only fires when both required components are present in `build_state`. This is the only module that owns the compatibility-score formula.
- `solvers.py` — all three creation-mode engines: `initialize_budget_build`/`on_user_pins_component` (Mode A, §5.2), `get_compatible_candidates`/`get_all_compatible_candidates` (Mode C free-custom filter, §5.2.1), and `allocate_workload_baseline` (Mode B, §5.6). Mode A must never return a selection that exceeds the ceiling.
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
def initialize_budget_build(ceiling: float, seed_selection: dict[str, Component] | None = None) -> dict[str, Component]: ...
def on_user_pins_component(selection: dict, category: str, component: Component, ceiling: float) -> dict[str, Component]: ...
def get_compatible_candidates(category: str, build_state: dict[str, Component]) -> list[Component]: ...
def get_all_compatible_candidates(build_state: dict[str, Component]) -> dict[str, list[Component]]: ...
def allocate_workload_baseline(profile: str, target_tier: str = "Mid") -> dict[str, Component]: ...
def value_index(component: Component, category_candidates: list[Component], build_state: dict) -> float: ...
def heuristic_synergy_score(compatibility_score: float, bottleneck_percentage: float) -> float: ...  # shared by llm/client.py's fallback and db/seed_demo.py
```
Named constants (safety multiplier `1.3`, baseline system draw `50` watts) live at module top-level in `compatibility.py`, not inlined.
