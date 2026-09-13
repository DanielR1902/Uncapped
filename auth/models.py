"""Sanitized user representation — no password hash, ever (see auth/CLAUDE.md)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: int
    username: str
    email: str
    full_name: str
    created_at: dt.datetime
