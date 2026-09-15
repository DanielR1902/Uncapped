# ui/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §7 before editing here.

## Responsibility
All Streamlit rendering, page routing, and session-state orchestration. This package is the only one allowed to import `streamlit` broadly (aside from `auth/session.py`'s narrow exception). It contains no SQL and no compatibility/scoring/solver math — it calls into `auth/`, `engine/`, `db/repositories/`, and `llm/` and renders their results.

## Files
- `router.py` — reads `st.session_state["page"]`, dispatches to the matching `views/*.py::render()`. Enforces the auth hard-gate: unauthenticated users are always forced to `landing` regardless of requested page (`spec.md` §7.2).
- `state.py` — the single source of truth for `st.session_state` shape: `init_session_state()` (defaults, called once from `app.py`), `new_build_draft()`, and the `build_draft` (plain dict, category -> component **id**) <-> `BuildState` (category -> `Component` row) bridge: `resolve_build_state`, `set_component`, `remove_component` (both also clear `build_draft_analysis` via `_invalidate_analysis` — a component change always makes the last synergy/bottleneck read stale), `build_total_cost`.
- `theme.py` — the single source of color/typography constants (`spec.md` §7.7) plus `inject_css()` and `tag(text, kind)` for colored inline spans (danger/success/warning). Status badges ("Selected"/"Empty", "Compatible") use the native `st.badge` widget instead, not a `theme.*` helper. No hex codes or font sizes hardcoded in any `views/` or `components/` file — import from here.
- `format.py` — tiny shared display-formatting helpers (e.g. `humanize_profile("VideoEditing") -> "Video Editing"`). Purely string formatting, no business logic.
- `views/landing.py`, `views/create_build.py`, `views/my_builds.py`, `views/community.py` — one file per top-level page, matching `spec.md` §7.4–§7.6.
- `components/auth_modal.py`, `components/build_card.py`, `components/part_picker.py` — reusable widgets shared across views. `build_card.py`'s `render_build_card(..., confirm_labels=frozenset({"Delete"}))` gates any action named in `confirm_labels` behind a Yes/Cancel step before its callback fires — use this for any future destructive action, not just Delete. `part_picker.py` renders each slot as a status-badged card with a `st.popover` drawer (not `st.expander`) for changing the pick, plus a slot-level `"✕"` clear button (`key=f"clear_{category}"`) next to the popover trigger — not inside the drawer — so a filled slot can be emptied in one click; the drawer itself holds only the candidate list, no bottom "Remove" button.

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
`llm.client.analyze_build(...)` already checks `llm_cache` internally before calling OpenRouter — `ui/` doesn't need to compute the cache key itself. What `ui/` *is* responsible for: not calling `analyze_build` on every single part-picker rerun. `create_build.py`'s `_maybe_auto_analyze` is the one deliberate exception to "gate behind an explicit click" — by product decision, it auto-calls `analyze_build` the instant a build first becomes complete (all 8 core categories filled), across every creation mode. This is still bounded, not "every rerun": the trigger condition is `build_draft_analysis is None AND build is complete`, and `ui/state.py`'s `set_component`/`remove_component` only set `build_draft_analysis` to `None` on an actual component change — so a rerun that doesn't change the build (opening a popover, adjusting an unrelated widget) never re-fires it. The compact "🔮 Analyze" button in the summary banner remains for a manual re-trigger. **Any test that drives `create_build.py` through `streamlit.testing.v1.AppTest`** must neutralize this (e.g. `monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)`, ideally as an autouse fixture) — otherwise a real `.env` key present in the test process (`db/database.py` loads it at import time) turns nearly every UI smoke test that completes a build into a live, billed OpenRouter call, violating this project's "no live network calls in tests" rule. See `tests/test_ui_smoke.py`'s `_no_live_llm_calls` autouse fixture.
