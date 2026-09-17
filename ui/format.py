"""Tiny shared display-formatting helpers. Pure string formatting, no business
logic — anything that touches engine/db/llm output shape belongs elsewhere."""
from __future__ import annotations

import re

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def humanize_profile(name: str) -> str:
    """"VideoEditing" -> "Video Editing"; "Gaming" -> "Gaming"."""
    return _CAMEL_BOUNDARY.sub(" ", name)


def sanitize_markdown(text: str) -> str:
    """Defensively strip any literal `$` from LLM-authored or free-text
    user-authored text before it reaches st.markdown/st.write. A matching
    "$...$" pair triggers Streamlit's KaTeX/math-mode rendering and garbles
    plain text (e.g. advisory prose, or a user-typed build name combined
    with an appended price) — this is a last-resort belt-and-suspenders
    guard in case the source text carries a stray `$` anyway."""
    return text.replace("$", "USD ")
