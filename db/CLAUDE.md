# db/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §3 before editing here.

## Responsibility
Schema definition, connection/session management, catalog seeding, and parameterized data access via the repository pattern. This is the only package that speaks SQL.

## Files
- `ddl.sql` — canonical DDL for all tables in `spec.md` §3 (`users`, `components`, `workload_mappings`, `builds`, `build_components`, `community_posts`, `community_comments`, `llm_cache`). Must stay portable across SQLite and PostgreSQL — no SQLite-only pragmas or functions.
- `database.py` — SQLAlchemy engine/session factory. Reads `DATABASE_URL` from env (defaults to local `sqlite:///db/uncapped.db` if unset). Exposes a single `get_engine()` / `get_session()` used by every repository.
- `seed_data.py` — catalog + workload-mapping seed rows per `spec.md` §4, as typed Python literals (not parsed from a spreadsheet/CSV). Pure data, no DB/session code.
- `seed.py` — the runner: loads `seed_data.py` into the database via `db.database`. Idempotent (skips if the `components` table is already populated, unless `force=True`) — re-running it must not duplicate rows.
- `seed_demo.py` — mock users/builds/community content for demos and manual testing (`spec.md` §4.4). Independent of `seed.py` (calls it first to ensure the catalog exists), idempotent the same way (checks for the demo usernames, `force=True` to wipe and reseed). Computes build scores via `engine/compatibility.py` + `engine/scoring.py` directly — never calls `llm/client.py`, so seeding never depends on network availability.
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

# community_repo.py
def create_post(build_id: int, user_id: int, title: str, author_notes: str | None) -> PostRow: ...
def get_feed() -> list[PostRow]: ...
def add_comment(post_id: int, user_id: int, content: str) -> CommentRow: ...
```
Every function that accepts user-supplied text (username, email, build name, post title, comment content) must use bound parameters — never f-string/`.format()` SQL construction.
