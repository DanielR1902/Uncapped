"""Tiny shared display-formatting helpers. Pure string formatting, no business
logic — anything that touches engine/db/llm output shape belongs elsewhere."""
from __future__ import annotations

import re

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")

# Multi-currency display support (spec.md §7.7/§6.7). Every price stored in
# the database and computed by engine/ux logic (Component.price_usd,
# Build.total_cost, budget_ceiling, etc.) stays in USD everywhere it is
# PERSISTED or COMPARED — these rates/helpers are a pure DISPLAY-layer
# concern, applied only at the point a number is turned into a string for
# the user (or, for llm/concierge.py, into a pre-formatted string handed to
# the model to quote verbatim — never a number it converts itself; see
# llm/concierge.py's NO AGGREGATE TOTALS RULE, which this extends to cover
# currency-conversion arithmetic too). Rates are static, approximate
# constants (not a live FX feed) — deliberately out of scope; wiring a real
# exchange-rate API would add a network dependency and a staleness/failure
# mode nothing in this app currently needs.
CURRENCY_SYMBOLS: dict[str, str] = {"USD": "$", "EUR": "€", "NIS": "₪"}
CURRENCY_RATES: dict[str, float] = {"USD": 1.0, "EUR": 0.92, "NIS": 3.70}  # relative to 1 USD
CURRENCY_CODES: tuple[str, ...] = ("USD", "EUR", "NIS")  # display/selector order
DEFAULT_CURRENCY = "USD"


def convert_price(amount_usd: float, currency: str = DEFAULT_CURRENCY) -> float:
    """Convert a real USD amount to `currency` at the static rate above.
    An unrecognized currency code falls back to the USD rate (1.0) rather
    than raising — the same defensive-fallback precedent as
    `CURRENCY_SYMBOLS.get(currency, "$")` below."""
    return amount_usd * CURRENCY_RATES.get(currency, 1.0)


def convert_to_usd(amount_local: float, currency: str = DEFAULT_CURRENCY) -> float:
    """The inverse of `convert_price` — a `currency`-denominated amount back
    to real USD (e.g. `convert_to_usd(6000.0, "NIS") -> 1621.62...`). There
    is no UI widget that takes a currency-denominated NUMERIC INPUT today
    (the Budget ceiling number_input is deliberately USD-only by design,
    spec.md §7.10) — this exists for `llm/concierge.py`'s own budget-parsing
    intent (SYSTEM_PROMPT's BUDGET CURRENCY CONVERSION rule): when a user
    states a build budget in a non-USD currency in a chat message (e.g.
    "build me a PC for 7000 NIS"), the model performs this SAME division
    itself (it has no way to call real Python code mid-generation) using the
    `currency_rates` dict handed to it in the payload — this function is the
    canonical, testable definition of that one conversion, kept here so the
    prompt's arithmetic instruction and this module's own logic can never
    silently drift apart."""
    return amount_local / CURRENCY_RATES.get(currency, 1.0)


def format_currency(amount_usd: float, currency: str = DEFAULT_CURRENCY) -> str:
    """"1250.0", "USD" -> "$1,250.00"; "1250.0", "NIS" -> "₪4,625.00". Takes
    `currency` as an explicit parameter rather than reading
    `st.session_state["selected_currency"]` itself — this module stays free
    of any Streamlit/runtime dependency (pure stdlib, easily unit-tested),
    matching `sanitize_markdown`'s/`humanize_profile`'s existing convention;
    every caller already has (or can cheaply read) the active currency and
    passes it in explicitly."""
    return f"{CURRENCY_SYMBOLS.get(currency, '$')}{convert_price(amount_usd, currency):,.2f}"


def currency_label(currency: str) -> str:
    """"USD" -> "USD ($)"; "NIS" -> "NIS (₪)" — the sidebar selector's
    display label for a given currency code."""
    return f"{currency} ({CURRENCY_SYMBOLS.get(currency, '$')})"


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
