"""Inline login/register modal — same page, no navigation (spec.md §7.3).

Streamlit commits a text_input's value (and reruns) on blur/Enter, not on
literal every keystroke, but that commit still reran the *entire* app —
sidebar, page title, everything — until each form was wrapped in its own
@st.fragment below. Now a field commit only reruns that fragment, so typing
into one field of the register form no longer reflows the whole page. Live
is_username_taken/is_email_taken checks still work exactly as before —
fragments rerun on every widget interaction inside them, same as the full app
would — they just don't drag the rest of the page along for the ride.

A successful submit calls a plain st.rerun() (default scope="app"), which
*is* a full-app rerun on purpose: login/register success needs to update the
sidebar and nav buttons outside this fragment's boundary.
"""
from __future__ import annotations

import streamlit as st

from auth import service
from auth.session import log_in, set_auth_mode
from ui import theme


@st.fragment
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


@st.fragment
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
