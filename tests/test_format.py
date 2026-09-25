"""Pure-function unit tests for ui/format.py's multi-currency helpers
(spec.md §7.7). This module carries no Streamlit/runtime dependency by
design (ui/format.py's own docstring), so it's unit-testable the same way
engine/ is, unlike most of ui/ (which is smoke-tested only per ui/CLAUDE.md).
"""
from __future__ import annotations

import datetime as dt

import pytest

from ui.format import (
    CURRENCY_CODES,
    CURRENCY_RATES,
    CURRENCY_SYMBOLS,
    convert_price,
    convert_to_usd,
    currency_label,
    format_currency,
    time_ago,
)


def test_convert_price_usd_is_identity():
    assert convert_price(100.0, "USD") == 100.0


def test_convert_price_applies_real_rate():
    assert convert_price(100.0, "EUR") == 100.0 * CURRENCY_RATES["EUR"]
    assert convert_price(100.0, "NIS") == 100.0 * CURRENCY_RATES["NIS"]


def test_convert_price_unknown_currency_falls_back_to_usd_rate():
    assert convert_price(50.0, "GBP") == 50.0


def test_format_currency_usd_uses_dollar_sign_and_commas():
    assert format_currency(1250.0) == "$1,250.00"


def test_format_currency_eur_uses_euro_symbol_and_real_rate():
    expected = f"€{1250.0 * CURRENCY_RATES['EUR']:,.2f}"
    assert format_currency(1250.0, "EUR") == expected


def test_format_currency_nis_uses_shekel_symbol_and_real_rate():
    expected = f"₪{1250.0 * CURRENCY_RATES['NIS']:,.2f}"
    assert format_currency(1250.0, "NIS") == expected


def test_format_currency_rounds_to_two_decimal_places():
    assert format_currency(99.999, "USD") == "$100.00"


def test_currency_label_matches_selector_format():
    assert currency_label("USD") == "USD ($)"
    assert currency_label("EUR") == "EUR (€)"
    assert currency_label("NIS") == "NIS (₪)"


def test_currency_codes_cover_every_symbol():
    assert set(CURRENCY_CODES) == set(CURRENCY_SYMBOLS)


def test_convert_to_usd_is_inverse_of_convert_price():
    for currency in CURRENCY_CODES:
        usd_amount = 1234.56
        local = convert_price(usd_amount, currency)
        assert convert_to_usd(local, currency) == pytest.approx(usd_amount)


def test_convert_to_usd_usd_is_identity():
    assert convert_to_usd(100.0, "USD") == 100.0


def test_convert_to_usd_nis_matches_real_rate():
    assert convert_to_usd(7000.0, "NIS") == pytest.approx(7000.0 / CURRENCY_RATES["NIS"])


def test_convert_to_usd_unknown_currency_falls_back_to_usd_rate():
    assert convert_to_usd(50.0, "GBP") == 50.0


# ---------------------------------------------------------------------------
# time_ago (Reddit-style relative timestamps, spec.md §7.6)
# ---------------------------------------------------------------------------
def test_time_ago_just_now():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(seconds=30), now=now) == "just now"


def test_time_ago_minutes():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(minutes=5), now=now) == "5m ago"


def test_time_ago_hours():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(hours=3), now=now) == "3h ago"


def test_time_ago_days():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(days=2), now=now) == "2d ago"


def test_time_ago_weeks():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(days=14), now=now) == "2w ago"


def test_time_ago_months():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(days=90), now=now) == "3mo ago"


def test_time_ago_years():
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now - dt.timedelta(days=400), now=now) == "1y ago"


def test_time_ago_future_timestamp_clamped_to_just_now():
    """Clock skew (a timestamp slightly ahead of "now") must never show a
    negative duration."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert time_ago(now + dt.timedelta(seconds=5), now=now) == "just now"


def test_time_ago_defaults_now_to_current_utc_time():
    """Omitting `now` entirely must not raise and must produce a plausible
    label for a timestamp from a few seconds ago."""
    recent = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=10)
    assert time_ago(recent) == "just now"
