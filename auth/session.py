"""Thin st.session_state helpers for auth. The only file in auth/ allowed to
import streamlit (see auth/CLAUDE.md) — everything else in this package stays
a pure domain service."""
from __future__ import annotations

import streamlit as st

from auth.models import User
from auth.service import to_session_payload


def current_user() -> dict | None:
    return st.session_state.get("auth_user")


def is_authenticated() -> bool:
    return current_user() is not None


def log_in(user: User) -> None:
    st.session_state["auth_user"] = to_session_payload(user)
    st.session_state["auth_mode"] = None
    st.session_state["auth_error"] = {}


def log_out() -> None:
    st.session_state["auth_user"] = None
    st.session_state["auth_mode"] = None
    st.session_state["page"] = "landing"


def set_auth_mode(mode: str | None) -> None:
    st.session_state["auth_mode"] = mode
    st.session_state["auth_error"] = {}
