# db/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §3 before editing here.

## Responsibility
Schema definition, connection/session management, catalog seeding, and parameterized data access via the repository pattern. This is the only package that speaks SQL.

## Files
- `ddl.sql` — canonical DDL for all tables in `spec.md` §3 (`users`, `components`, `workload_mappings`, `builds`, `build_components`, `community_posts`, `community_comments`, `llm_cache`). Must stay portable across SQLite and PostgreSQL — no SQLite-only pragmas or functions.
- `database.py` — SQLAlchemy engine/session factory. Reads `DATABASE_URL` from env (defaults to local `sqlite:///db/uncapped.db` if unset). Exposes a single `get_engine()` / `get_session()` used by every repository. `init_db()` calls `Base.metadata.create_all(...)` (creates missing TABLES only — never ALTERs an existing one to add a new column) followed by `_ensure_builds_workload_tier_column()`, a small additive-only SQLite migration (`ALTER TABLE builds ADD COLUMN workload_tier TEXT`, guarded by a `PRAGMA table_info` check so it's a no-op once the column exists or on a table that doesn't exist yet) — needed because `Build.workload_tier` was added after real `builds` rows already existed in some environments. This is this project's only precedent for a schema change to an existing table; a future one should follow the same additive-only, idempotent, never-drop pattern rather than reaching for `reset_db()`.
- `seed_data.py` — catalog + workload-mapping seed rows per `spec.md` §4, as typed Python literals (not parsed from a spreadsheet/CSV). Pure data, no DB/session code.
- `seed.py` — the runner: loads `seed_data.py` into the database via `db.database`. Idempotent (skips if the `components` table is already populated, unless `force=True`) — re-running it must not duplicate rows.
- `seed_demo.py` — mock users/builds/community content for demos and manual testing (`spec.md` §4.4). Independent of `seed.py` (calls it first to ensure the catalog exists), idempotent the same way (checks for the demo usernames, `force=True` to wipe and reseed). Computes build scores via `engine/compatibility.py` + `engine/scoring.py` directly — never calls `llm/client.py`, so seeding never depends on network availability.
- `seed_admin_builds.py` — 12 realistic builds (3 Budget, 7 Workload spanning all 5 real profiles, 2 hand-assembled Free-mode) attached to the standing `admin` user seeded by `seed_demo.py`'s `_ensure_admin_user()` — never creates that user itself, raises if it's missing rather than creating a second one. Same conventions as `seed_demo.py` (idempotent by build name scoped to admin, `force=True` deletes and recreates only its own 12, never touches admin's other builds or `seed_demo.py`'s persona data). Every build is validated `is_compatible=True`/`compatibility_score=100.0` via `evaluate_build` before being persisted — an incompatible plan is skipped with a printed warning, never silently written. The 2 Free-mode builds use real, hand-picked catalog component ids (verified compatible, including one using the RAM/Storage quantity feature — `quantities={"Storage": 2}`, checked against the chosen motherboard's real `m2_slots` via `engine.compatibility.resolve_quantity_limit` before being committed) rather than going through a solver.
- `seed_community_shares.py` — shares a fixed, deliberate spread of 6 of `seed_admin_builds.py`'s 12 builds (1 Budget, 3 Workload across different profiles, both Free-mode) to the community feed via the same `builds_repo.set_public(build_id, True)` + `community_repo.create_post(...)` pair `ui/views/my_builds.py`'s own "Share to Community" button uses — there is no separate "community_builds"/"shared_builds" table. Comments are attached from `seed_demo.py`'s existing 5 persona users (never a new set of invented usernames) and are generated from each build's REAL persisted component/score data (`build.components` → `BuildComponent.category`/`.component`, plus `engine.scoring.bottleneck_percentage_baseline`) rather than generic filler, so every comment references that specific build's actual parts. Idempotent (checks `community_repo.get_feed()` for an existing post per build before creating a duplicate; never re-adds comments to a post that already has them, even under `force=True`) and depends on (calls first, never duplicates) both `seed_demo.py` and `seed_admin_builds.py`'s own idempotent entry points to ensure its prerequisites exist.
- `backfill_workload_tier.py` — one-time, idempotent backfill for `Build.workload_tier` on rows persisted before that column existed, using each build's real, already-known tier (hardcoded `(username, build_name) -> tier` pairs read directly from `seed_admin_builds.py`'s/`seed_demo.py`'s own `BuildPlan.tier` values — never a guess). Only ever updates a row where `workload_tier IS NULL`, so re-running it is always safe and never overwrites a real value. Goes entirely through `db.repositories.builds_repo`, never raw SQL.
- `repositories/` — one file per aggregate (`users_repo.py`, `components_repo.py`, `builds_repo.py`, `community_repo.py`). Each exposes plain functions (not a generic ORM passthrough) matching exactly what `engine/`, `auth/`, and `ui/` need — no leaking `Session`/`Connection` objects out of this package.

## Allowed imports
- `sqlalchemy` (Core, not the full ORM unless a repository genuinely benefits from it).
- Standard library (`json`, `hashlib` for `llm_cache` keys is fine here too since it's just storage).

## Forbidden
- No business logic (no compatibility checks, no scoring, no solver logic) — those belong in `engine/`.
- No `streamlit` import anywhere in this package.
- No raw string-interpolated SQL — every query is parameterized.
- No imports from `auth/`, `engine/`, `ui/`, or `llm/` (this package sits at the bottom of the dependency graph, per root `CLAUDE.md`).

## Interface contract (repository functions callers rely on)
```python
# users_repo.py
def get_by_username(username: str) -> UserRow | None: ...
def get_by_email(email: str) -> UserRow | None: ...
def create_user(username: str, password_hash: str, email: str, full_name: str) -> UserRow: ...

# components_repo.py
def get_by_category(category: str) -> list[ComponentRow]: ...
def get_by_id(component_id: int) -> ComponentRow | None: ...

# builds_repo.py
def create_build(user_id: int, name: str, creation_mode: str, components: list[ComponentRow], **scores) -> BuildRow: ...
def get_builds_for_user(user_id: int, filter: BuildsFilter) -> list[BuildRow]: ...
def set_public(build_id: int, is_public: bool) -> None: ...
def set_workload_tier(build_id: int, workload_tier: str) -> None: ...
def delete_build(build_id: int) -> None: ...  # cascades to build_components + community_posts/comments; no-op if already gone

# community_repo.py
def create_post(build_id: int, user_id: int, title: str, author_notes: str | None) -> PostRow: ...
def get_feed() -> list[PostRow]: ...
def add_comment(post_id: int, user_id: int, content: str) -> CommentRow: ...
```
Every function that accepts user-supplied text (username, email, build name, post title, comment content) must use bound parameters — never f-string/`.format()` SQL construction.
