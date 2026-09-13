"""Inline login/register modal — same page, no navigation (spec.md §7.3).

Streamlit reruns the whole script on every widget interaction, so checking
is_username_taken/is_email_taken on each rerun (outside a st.form, so each
keystroke commits immediately) gives genuinely live duplicate flags, not just
an on-submit check.
"""
from __future__ import annotations

import streamlit as st

from auth import service
from auth.session import log_in, set_auth_mode
from ui import theme


def _register_form() -> None:
    st.subheader("Create an account")

    username = st.text_input("Username", key="register_username")
    if username and service.is_username_taken(username):
        st.markdown(theme.tag("This username is already taken.", "danger"), unsafe_allow_html=True)

    email = st.text_input("Email", key="register_email")
    if email and service.is_email_taken(email):
        st.markdown(theme.tag("This email is already registered.", "danger"), unsafe_allow_html=True)

    full_name = st.text_input("Full name", key="register_full_name")
    password = st.text_input("Password", type="password", key="register_password")

    if st.button("Register", key="register_submit"):
        try:
            user = service.register(username, password, email, full_name)
        except service.ValidationError as exc:
            for field, message in exc.errors.items():
                st.markdown(theme.tag(f"{field.replace('_', ' ').title()}: {message}", "danger"), unsafe_allow_html=True)
        else:
            log_in(user)
            st.rerun()


def _login_form() -> None:
    st.subheader("Log in")

    identifier = st.text_input("Username or email", key="login_identifier")
    password = st.text_input("Password", type="password", key="login_password")

    if st.button("Log in", key="login_submit"):
        user = service.authenticate(identifier, password)
        if user is None:
            st.markdown(theme.tag("Incorrect username/email or password.", "danger"), unsafe_allow_html=True)
        else:
            log_in(user)
            st.rerun()


def render_auth_modal() -> None:
    col_login, col_register = st.columns(2)
    with col_login:
        if st.button("Login", key="open_login", use_container_width=True):
            set_auth_mode("login")
            st.rerun()
    with col_register:
        if st.button("Register", key="open_register", use_container_width=True):
            set_auth_mode("register")
            st.rerun()

    mode = st.session_state.get("auth_mode")
    if mode == "login":
        _login_form()
    elif mode == "register":
        _register_form()
