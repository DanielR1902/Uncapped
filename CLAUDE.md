# Uncapped — Project Guardrails

This is a Spec-Driven Development (SDD) project. Read `spec.md` (and `intent.txt` for original intent) before touching code. `spec.md` is the source of truth — if an instruction conflicts with it, update `spec.md` first, then implement.

## Tech stack (do not substitute without updating spec.md)
- Python 3.11+, type hints on public functions.
- Streamlit for UI — session-state-driven router in `ui/router.py`, not native `st.Page` file-based navigation (the landing page must hard-gate on auth).
- SQLAlchemy Core over SQLite locally / PostgreSQL in cloud. No raw `sqlite3` module usage, no SQLite-only SQL syntax.
- `bcrypt` for password hashing (used directly, no `passlib` wrapper).
- OpenRouter for LLM calls, via `httpx`, isolated entirely inside `llm/`.
- `pytest` for tests, `ruff` + `black` for lint/format.

## Architecture — module boundaries are load-bearing

```
ui  →  auth, engine, db.repositories, llm
llm →  engine, db.repositories (cache table only)
engine → db.repositories (read-only)
auth → db.repositories (users_repo only)
db  →  nothing above it
```

- `engine/` is pure Python: no Streamlit imports, no network calls, no imports from `llm/` or `ui/`. It must remain unit-testable with no mocking of external services.
- `llm/` never talks to the database except the `llm_cache` table, and never contains compatibility logic — it receives deterministic pre-check results from `engine` as part of its request payload and only adds interpretive/advisory output.
- `ui/` contains no SQL and no compatibility/scoring math — it only calls into the other four packages.
- `db/` contains no business logic — only schema, connection management, seeding, and parameterized repository queries.
- Compatibility is never LLM-gated: deterministic rules in `engine/compatibility.py` are the only source of pass/fail. The LLM explains and contextualizes; it cannot override a failed check.

## SDD guardrails
- Every subdirectory (`auth/`, `engine/`, `db/`, `ui/`, `llm/`) has its own `CLAUDE.md` scoping its responsibilities and allowed imports. Read the scoped file before editing inside that directory.
- Don't add scope beyond `intent.txt` / `spec.md`. If a feature seems missing or ambiguous, flag it and propose a `spec.md` update rather than silently improvising.
- Any change to behavior (new compatibility rule, new prompt schema, new DB column, new page) requires updating `spec.md` in the same change — spec and code must never drift.
- No secrets committed. `.env` is gitignored; `.env.example` documents required variables (`DATABASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`) with no real values.
- No SQL string interpolation — parameterized queries only, enforced in `db/repositories/`.

## Testing expectations
- `engine/` and `db/` require unit tests for new logic (pure functions, no live DB needed for `engine/`; an in-memory SQLite DB for `db/` repository tests).
- `llm/` tests mock the HTTP layer — never call the live OpenRouter API in tests or CI.
- UI is verified manually via `streamlit run app.py` for behavior changes; it is not unit-tested beyond smoke-level import checks.
