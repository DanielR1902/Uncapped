"""Tiny shared display-formatting helpers. Pure string formatting, no business
logic — anything that touches engine/db/llm output shape belongs elsewhere."""
from __future__ import annotations

import re

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def humanize_profile(name: str) -> str:
    """"VideoEditing" -> "Video Editing"; "Gaming" -> "Gaming"."""
    return _CAMEL_BOUNDARY.sub(" ", name)
