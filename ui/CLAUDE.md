# ui/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §7 before editing here.

## Responsibility
All Streamlit rendering, page routing, and session-state orchestration. This package is the only one allowed to import `streamlit` broadly (aside from `auth/session.py`'s narrow exception). It contains no SQL and no compatibility/scoring/solver math — it calls into `auth/`, `engine/`, `db/repositories/`, and `llm/` and renders their results.

## Files
- `router.py` — reads `st.session_state["page"]`, dispatches to the matching `views/*.py::render()`. Enforces the auth hard-gate: unauthenticated users are always forced to `landing` regardless of requested page (`spec.md` §7.2).
- `state.py` — the single source of truth for `st.session_state` shape: `init_session_state()` (defaults, called once from `app.py`), `new_build_draft()`, and the `build_draft` (plain dict, category -> component **id**) <-> `BuildState` (category -> `Component` row) bridge: `resolve_build_state`, `set_component`, `remove_component`, `build_total_cost`, `is_build_complete`.
- `theme.py` — the single source of color/typography constants (`spec.md` §7.7) plus `inject_css()` and small helpers like `tag(text, kind)` for colored inline spans. No hex codes or font sizes hardcoded in any `views/` or `components/` file — import from here.
- `format.py` — tiny shared display-formatting helpers (e.g. `humanize_profile("VideoEditing") -> "Video Editing"`). Purely string formatting, no business logic.
- `views/landing.py`, `views/create_build.py`, `views/my_builds.py`, `views/community.py` — one file per top-level page, matching `spec.md` §7.4–§7.6.
- `components/auth_modal.py`, `components/build_card.py`, `components/part_picker.py` — reusable widgets shared across views.

## Allowed imports
- `streamlit`.
- `auth.service`, `auth.session`.
- `engine.*` (compatibility, solvers, scoring) — for computing what to display, never to persist.
- `db.repositories.*` — for reading/writing builds, components, community data.
- `llm.client` — for build analysis display. `analyze_build(...)` never raises (see `llm/CLAUDE.md`), so there is no fallback branch to write here — just read `response.source` (`"llm"` vs `"heuristic"`) to pick the badge.

## Forbidden
- No SQL, no SQLAlchemy imports — go through `db.repositories`.
- No compatibility/scoring math inlined in a view — call `engine/`.
- No direct `httpx`/OpenRouter calls — go through `llm.client`.
- No view may bypass the auth gate in `router.py` by setting `st.session_state["page"]` directly to a gated page without an `auth_user` present.

## State ownership
Own and mutate only the `st.session_state` keys listed in `spec.md` §7.1 (`page`, `auth_user`, `auth_mode`, `auth_error`, `create_mode`, `build_draft`, `build_draft_analysis`, `sort_criteria`, `previous_builds_filter`, `selected_post_id`, `fork_source_build_id`), and prefer the helpers in `state.py` over raw dict writes scattered across view files. `auth_user`/`auth_mode`/`auth_error` should be mutated only via `auth.session` helpers (`log_in`, `log_out`, `set_auth_mode`), never as raw dict writes.

## LLM debounce/cache contract
`llm.client.analyze_build(...)` already checks `llm_cache` internally before calling OpenRouter — `ui/` doesn't need to compute the cache key itself. What `ui/` *is* responsible for: not calling `analyze_build` on every single part-picker rerun (e.g. gate it behind an explicit "Analyze" button or a debounce), since each distinct build state that isn't already cached is a real network call.
