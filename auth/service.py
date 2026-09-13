"""Registration, authentication, and password hashing. Pure domain service — no
Streamlit import anywhere in this file (see auth/CLAUDE.md)."""
from __future__ import annotations

import re

import bcrypt

from auth.models import User
from db.repositories import users_repo

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 32
PASSWORD_MIN_LENGTH = 8
BCRYPT_ROUNDS = 12

# Simple, deliberately permissive email shape check — full RFC 5322 validation
# is out of scope; this just catches obviously-malformed input client-side
# before it reaches the unique-email check.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ValidationError(Exception):
    """Carries field -> message so the UI can red-highlight the offending
    inputs without parsing exception text."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{field}: {message}" for field, message in errors.items()))


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def is_username_taken(username: str) -> bool:
    return users_repo.get_by_username(username) is not None


def is_email_taken(email: str) -> bool:
    return users_repo.get_by_email(email) is not None


def validate_unique(username: str, email: str) -> dict[str, str]:
    """field -> error message; empty dict means no conflicts. Used for live
    validation as the user types, and re-checked before register() commits."""
    errors: dict[str, str] = {}
    if is_username_taken(username):
        errors["username"] = "This username is already taken."
    if is_email_taken(email):
        errors["email"] = "This email is already registered."
    return errors


def _validate_fields(username: str, password: str, email: str, full_name: str) -> dict[str, str]:
    errors: dict[str, str] = {}

    if not username or not (USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH):
        errors["username"] = f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} characters."

    if not email or not EMAIL_PATTERN.match(email):
        errors["email"] = "Enter a valid email address."

    if not full_name or not full_name.strip():
        errors["full_name"] = "Full name is required."

    if not password or len(password) < PASSWORD_MIN_LENGTH:
        errors["password"] = f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    elif not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
        errors["password"] = "Password must contain both letters and numbers."

    return errors


def _to_domain_user(row) -> User:
    return User(
        id=row.id,
        username=row.username,
        email=row.email,
        full_name=row.full_name,
        created_at=row.created_at,
    )


def register(username: str, password: str, email: str, full_name: str) -> User:
    """Raises ValidationError (field -> message) on any field-format problem
    or username/email conflict. Only hashes/persists once validation passes."""
    errors = _validate_fields(username, password, email, full_name)
    errors.update(validate_unique(username, email))
    if errors:
        raise ValidationError(errors)

    password_hash = _hash_password(password)
    row = users_repo.create_user(username=username, password_hash=password_hash, email=email, full_name=full_name)
    return _to_domain_user(row)


def authenticate(identifier: str, password: str) -> User | None:
    """`identifier` may be a username OR an email (spec.md §7.3 dual-identifier
    login). Returns None on any mismatch — never distinguishes "no such user"
    from "wrong password" to the caller, to avoid leaking which is which."""
    row = users_repo.get_by_username(identifier) or users_repo.get_by_email(identifier)
    if row is None:
        return None
    if not _verify_password(password, row.password_hash):
        return None
    return _to_domain_user(row)


def to_session_payload(user: User) -> dict:
    """Sanitized dict for st.session_state["auth_user"] — never includes the
    password hash, even indirectly."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
    }
