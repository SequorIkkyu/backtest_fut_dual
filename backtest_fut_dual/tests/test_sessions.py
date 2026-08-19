"""Session-classification and trading-day boundary tests for common/sessions.py.

Trading day = night session (21:00) + the following day session (09:00-15:00),
mapped with the repo's +6h convention so a 21:00 action and the next 09:00-15:00
action share one trading-day bucket. The +6h shift puts the day boundary at 18:00.
"""

from __future__ import annotations

import datetime

import pandas as pd

from common.sessions import classify_session, trading_day_of


# ── classify_session ───────────────────────────────────────────────────────────
def test_day_session_window():
    assert classify_session(9) == "day"
    assert classify_session(12) == "day"
    assert classify_session(14) == "day"
    assert classify_session(15) == "other"     # 15:00 close is excluded
    assert classify_session(8) == "other"


def test_night_session_wraps_midnight():
    assert classify_session(21) == "night"
    assert classify_session(23) == "night"
    assert classify_session(0) == "night"
    assert classify_session(2) == "night"
    assert classify_session(3) == "other"       # 03:00 close is excluded
    assert classify_session(20) == "other"


# ── trading_day_of (+6h convention) ────────────────────────────────────────────
def test_night_and_next_day_share_trading_day():
    night = pd.Timestamp("2025-01-01 21:00")
    nextday = pd.Timestamp("2025-01-02 09:00")
    close = pd.Timestamp("2025-01-02 14:59")
    assert trading_day_of(night) == datetime.date(2025, 1, 2)
    assert trading_day_of(nextday) == datetime.date(2025, 1, 2)
    assert trading_day_of(close) == datetime.date(2025, 1, 2)


def test_trading_day_boundary_is_18h():
    # +6h boundary: <18:00 stays on the calendar day; >=18:00 rolls to the next.
    assert trading_day_of(pd.Timestamp("2025-01-01 17:59")) == datetime.date(2025, 1, 1)
    assert trading_day_of(pd.Timestamp("2025-01-01 18:00")) == datetime.date(2025, 1, 2)


def test_day_session_maps_to_same_calendar_day():
    assert trading_day_of(pd.Timestamp("2025-03-10 09:30")) == datetime.date(2025, 3, 10)
    assert trading_day_of(pd.Timestamp("2025-03-10 14:00")) == datetime.date(2025, 3, 10)


def test_accepts_string_timestamp():
    assert trading_day_of("2025-01-01 21:30") == datetime.date(2025, 1, 2)
