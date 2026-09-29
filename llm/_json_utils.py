"""Shared, dependency-free helpers for defensively parsing an OpenRouter
`/chat/completions` body — used by `llm/concierge.py` and `llm/client.py`
(never `llm/advisory.py`, owned separately).

Two concerns live here, both pure functions (no network, no `time`):

1. `extract_message(body)` — pull `(content, finish_reason)` out of a response
   body. `finish_reason == "length"` means the model was cut off by
   `max_tokens` and `content` is (very likely) truncated JSON.
2. `repair_json(content, safe_string_keys=None)` — a deliberately CONSERVATIVE
   repair for a truncated/fenced JSON object. It only ever (a) strips a
   markdown fence, (b) extracts the outermost `{...}`, (c) closes ONE
   unterminated string VALUE, (d) drops a trailing comma, and (e) balances the
   open `{`/`[`. It refuses (returns `None`) rather than guess whenever the
   truncation cut through a key, a number, a partial literal, or a string
   whose key isn't in `safe_string_keys` — a silently-shortened id list or a
   half-typed number is far worse than falling back. Callers must still run
   the repaired dict through their pydantic schema and any zero-hallucination
   validator; a repaired payload that fails either is discarded.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Explicit, generous completion budget. The concierge's `load_build` payload
# (8-15 ids + explanation + reply) and the analysis payload are well under
# this, so hitting it means something is genuinely wrong, but it is high enough
# that an ordinary response is never truncated by the provider default.
DEFAULT_MAX_TOKENS = 4096

# Retry backoff for a malformed/truncated body: 2 retries, 1s then 2s. Kept as
# a module-level constant on every caller (monkeypatched to zeros in tests).
MALFORMED_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0)


class TruncatedResponseError(ValueError):
    """The body's `finish_reason` was `"length"` — treated as a retryable
    truncation signal."""


def extract_message(body: Any) -> tuple[str, str | None]:
    """Return `(content, finish_reason)` from a decoded response body. Raises
    KeyError/IndexError/TypeError on an unexpected shape, and TypeError when
    `content` isn't a string."""
    choice = body["choices"][0]
    content = choice["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("message content is not a string")
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    return content, finish_reason


def parse_strict(content: str) -> dict:
    """`json.loads` requiring a JSON object. Raises ValueError otherwise."""
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("response JSON is not an object")
    return parsed


_FENCE_START = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*")
_FENCE_END = re.compile(r"\s*```\s*$")


def _strip_fences(text: str) -> str:
    text = _FENCE_START.sub("", text, count=1)
    return _FENCE_END.sub("", text, count=1)


def repair_json(content: str, safe_string_keys: frozenset[str] | set[str] | None = None) -> dict | None:
    """Best-effort, conservative repair — see module docstring. Returns the
    parsed dict, or `None` when the text can't be repaired safely."""
    if not isinstance(content, str):
        return None
    text = _strip_fences(content.strip())
    start = text.find("{")
    if start == -1:
        return None
    text = text[start:]

    # Each stack frame: [bracket, current_key]; for an array the "key" is its
    # parent's key, so a string element inherits it.
    stack: list[list[Any]] = []
    in_str = False
    esc = False
    str_start = 0
    str_is_value = False
    str_owner_key: str | None = None
    last_sig = ""
    last_string_was_key = False
    last_token_start = 0
    end_index = len(text)

    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                raw = text[str_start + 1 : i]
                if not str_is_value and stack:
                    stack[-1][1] = raw
                last_string_was_key = not str_is_value
                last_sig = '"'
            continue
        if ch.isspace():
            continue
        if ch == '"':
            in_str = True
            str_start = i
            top = stack[-1] if stack else None
            str_is_value = last_sig == ":" or (top is not None and top[0] == "[" and last_sig in ("[", ","))
            str_owner_key = top[1] if top is not None else None
            continue
        if ch in "{[":
            parent_key = stack[-1][1] if stack else None
            stack.append([ch, parent_key])
            last_sig = ch
            continue
        if ch in "}]":
            if not stack:
                return None
            stack.pop()
            last_sig = ch
            if not stack:
                end_index = i + 1
                break
            continue
        if ch == ",":
            last_sig = ","
            continue
        if ch == ":":
            last_sig = ":"
            continue
        # number / literal character
        if last_sig not in ("lit",):
            last_token_start = i
        last_sig = "lit"

    candidate = text[:end_index]
    if not stack:
        # Balanced already (possibly with trailing garbage cut off above).
        try:
            return parse_strict(candidate)
        except ValueError:
            return None

    tail = candidate
    if in_str:
        if not str_is_value:
            return None
        if safe_string_keys is not None and str_owner_key not in safe_string_keys:
            return None
        # Drop a dangling single backslash so the closing quote isn't escaped.
        trailing_backslashes = len(tail) - len(tail.rstrip("\\"))
        if trailing_backslashes % 2 == 1:
            tail = tail[:-1]
        tail += '"'
    else:
        if last_sig == ":":
            return None
        if last_sig == '"' and last_string_was_key:
            return None
        if last_sig == "lit":
            token = candidate[last_token_start:].strip()
            if token not in ("true", "false", "null"):
                return None
        if last_sig == ",":
            tail = tail.rstrip()
            tail = tail[:-1]

    closers = "".join("}" if frame[0] == "{" else "]" for frame in reversed(stack))
    try:
        return parse_strict(tail + closers)
    except ValueError:
        return None
