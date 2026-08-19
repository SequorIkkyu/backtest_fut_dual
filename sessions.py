"""Trading-session compatibility helpers and the default product calendar.

New foundation callers receive an injected ``SessionCalendar`` through their
``InstrumentSpec``.  The constants and small functions below remain only for
legacy callers; they model the historic 21:00-03:00 / 09:00-15:00 schedule and
must not be used by new product-aware scheduling code.
"""

from __future__ import annotations

import datetime

import pandas as pd

from common.foundation_contracts import SessionCalendar, SessionWindow


# Legacy clock-time constants retained for old callers.  ``Backtest.run_trading_day``
# and ``backtest.run_date`` use injected calendars instead.
NIGHT_SESSION = ("21:00", "03:00")
DAY_SESSION = ("09:00", "15:00")

DEFAULT_SESSION_CALENDAR = SessionCalendar(
    "legacy-shanghai-futures-v1",
    "Asia/Shanghai",
    windows=(
        SessionWindow("night", datetime.time(21, 0), datetime.time(3, 0)),
        SessionWindow("day", datetime.time(9, 0), datetime.time(15, 0)),
    ),
    trading_day_rollover=datetime.time(18, 0),
    eod_time=datetime.time(15, 0),
)

# Historical public value retained for order-limit/report compatibility.
TRADING_DAY_SHIFT = datetime.timedelta(hours=6)


def trading_day_of(ts) -> datetime.date:
    """Legacy trading-day mapping using the default calendar."""
    return DEFAULT_SESSION_CALENDAR.trading_day_of(pd.Timestamp(ts).to_pydatetime())


def classify_session(hour: int) -> str:
    """Legacy hour classifier with the historical exclusive close boundary."""
    if 9 <= hour < 15:
        return "day"
    if hour >= 21 or hour < 3:
        return "night"
    return "other"
