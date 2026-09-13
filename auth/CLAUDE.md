# auth/ — Scoped Guardrails

See root `CLAUDE.md` and `spec.md` §7.3 before editing here.

## Responsibility
Registration, authentication, and session-state exposure of the logged-in user. Nothing else — no build logic, no UI rendering beyond reading/writing `st.session_state` auth keys.

## Files
- `models.py` — `User` dataclass (id, username, email, full_name, created_at). No password hash exposed outside `service.py`.
- `service.py` — `register`, `authenticate`, `is_username_taken`/`is_email_taken` (live pre-check), `validate_unique`, `to_session_payload`. All password handling (hash/verify) lives here via the `bcrypt` package directly. Never log or return the raw password or hash.
- `session.py` — thin helpers over `st.session_state["auth_user"]`, `st.session_state["auth_mode"]`, `st.session_state["auth_error"]`: `current_user()`, `is_authenticated()`, `log_in(user)`, `log_out()`, `set_auth_mode(mode)`. This is the **only** file in `auth/` allowed to import `streamlit`.

## Allowed imports
- `db.repositories.users_repo` (only repository this package may touch).
- `bcrypt`.
- `streamlit` — only inside `session.py`.

## Forbidden
- No direct SQL or SQLAlchemy engine access — go through `users_repo`.
- No imports from `engine/`, `llm/`, or `ui/`.
- No plaintext password storage, logging, or comparison (always hash-compare via `bcrypt.checkpw`).

## Interface contract
```python
# service.py
class ValidationError(Exception):
    errors: dict[str, str]  # field -> message

def is_username_taken(username: str) -> bool: ...
def is_email_taken(email: str) -> bool: ...
def validate_unique(username: str, email: str) -> dict[str, str]: ...  # field -> error message, empty dict if OK
def register(username: str, password: str, email: str, full_name: str) -> User: ...  # raises ValidationError
def authenticate(identifier: str, password: str) -> User | None: ...  # identifier: username OR email
def to_session_payload(user: User) -> dict: ...  # sanitized dict for st.session_state — never includes password_hash
```
`validate_unique` (or the two `is_*_taken` pre-checks individually) must be called before `register` commits — per `spec.md` §7.3, duplicate username/email must be flagged per-field, in red, without a page navigation. `register` raises `ValidationError` (carrying the same field->message shape) rather than returning it, so callers use `try/except` and `ValidationError.errors` drives the per-field red-highlighting.
