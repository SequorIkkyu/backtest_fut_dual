"""Phase-1 product-calendar and continuous trading-day acceptance tests."""

from __future__ import annotations

from datetime import date, time
from unittest.mock import patch

import pandas as pd

from common.backtest import Backtest, run_date
from common.foundation_contracts import InstrumentSpec, SessionCalendar, SessionWindow
from common.market import Market
from common.order_limit import OrderLimitTracker
from common.strategy import Strategy
from common.tests.helpers import make_record


def _calendar(*, early_closes=None, holidays=frozenset(), missing_data_disposition="reject"):
    return SessionCalendar(
        "phase1-zn-v1",
        "Asia/Shanghai",
        windows=(
            SessionWindow("night", time(21, 0), time(1, 0)),
            SessionWindow("day", time(9, 0), time(15, 15)),
        ),
        trading_day_rollover=time(18, 0),
        eod_time=time(15, 15),
        early_closes=early_closes or {},
        holidays=holidays,
        missing_data_disposition=missing_data_disposition,
    )


def _frame(rows):
    return pd.DataFrame(rows).set_index("datetime")


class _LifecycleTraceStrategy(Strategy):
    def __init__(self, market):
        super().__init__("phase1-trace", market, 1, 1.0, None, 0.0)
        self.trace = []

    def reset(self, contract, trading_date=None):
        self.trace.append(("reset", contract, str(trading_date)))
        return super().reset(contract, trading_date)

    def step(self, md):
        if md["contract"] != self.contract:
            return
        self.curr_md = md
        self.session_step_count += 1
        self.trace.append(("step", pd.Timestamp(md["datetime"]).strftime("%H:%M")))
        self.pos = 2

    def on_session_break(self, event):
        self.trace.append(("break", event.window_name, event.scheduled_at.strftime("%H:%M"), self.pos))
        return super().on_session_break(event)

    def on_eod(self, event):
        self.trace.append(("eod", event.scheduled_at.strftime("%H:%M"), self.pos))
        return super().on_eod(event)

    def stop(self):
        self.trace.append(("stop", self.pos))
        return super().stop()


def test_trading_day_preserves_state_across_break_and_emits_eod_without_touch_fill():
    calendar = _calendar()
    spec = InstrumentSpec("ZN", 0.05, 5.0, calendar, "fees-v1", "roll-v1")
    rows = [
        make_record("ZN", "2025-01-01 21:00:00", tick=0.05),
        make_record("ZN", "2025-01-02 00:59:00", tick=0.05),
        make_record("ZN", "2025-01-02 09:00:00", tick=0.05),
        make_record("ZN", "2025-01-02 15:15:00", tick=0.05),
    ]
    market = Market(10000, 0.005)
    strategy = _LifecycleTraceStrategy(market)
    backtest = Backtest(market, [strategy], 10000, 0.005, instrument_specs={"ZN": spec})

    backtest.backtest_trading_day(_frame(rows), trading_day=date(2025, 1, 2))

    assert strategy.trace[0] == ("reset", "ZN", "2025-01-02")
    assert [item for item in strategy.trace if item[0] == "reset"] == [("reset", "ZN", "2025-01-02")]
    assert ("break", "night", "01:00", 2) in strategy.trace
    assert ("step", "15:15") in strategy.trace
    assert ("eod", "15:15", 2) in strategy.trace
    assert not [item for item in strategy.trace if item[0] == "stop"]
    assert strategy.pos == 2
    assert strategy.session_calendar_id == "phase1-zn-v1"
    assert strategy.trading_date == "2025-01-02"
    assert strategy.session_window == "day"
    assert backtest.last_eod_outcomes[0]["status"] == "pending_execution_service"


def test_two_instrument_specs_bind_independent_ticks_and_multipliers():
    calendar = _calendar()
    quoted = InstrumentSpec("P", 0.05, 5.0, calendar, "fees-p", "roll-p")
    hedge = InstrumentSpec("H", 0.01, 20.0, calendar, "fees-h", "roll-h")
    rows = [
        make_record("P", "2025-01-02 09:00:00", tick=0.05),
        make_record("H", "2025-01-02 09:00:01", tick=0.01),
    ]
    market = Market(10000, 0.005)
    p_strategy = _LifecycleTraceStrategy(market)
    h_strategy = _LifecycleTraceStrategy(market)
    backtest = Backtest(market, [p_strategy, h_strategy], 10000, 0.005)

    backtest.backtest_trading_day(
        _frame(rows),
        {"P": quoted, "H": hedge},
        trading_day=date(2025, 1, 2),
        strategy_products=("P", "H"),
    )

    assert (p_strategy.tick, p_strategy.mult) == (0.05, 5.0)
    assert (h_strategy.tick, h_strategy.mult) == (0.01, 20.0)
    assert market.tick_for("P") == 0.05
    assert market.tick_for("H") == 0.01
    assert market.price_tol_for("P") == 0.005
    assert market.price_tol_for("H") == 0.001
    assert p_strategy._normalize_price(100.054) == 100.05
    assert h_strategy._normalize_price(100.054) == 100.054


def test_calendar_holiday_early_close_and_missing_data_disposition_are_explicit():
    early = _calendar(early_closes={date(2025, 1, 2): time(13, 0)})
    assert early.is_trading_time(pd.Timestamp("2025-01-02 13:00:00").to_pydatetime())
    assert not early.is_trading_time(pd.Timestamp("2025-01-02 13:01:00").to_pydatetime())
    assert early.eod_at(date(2025, 1, 2)).strftime("%H:%M") == "13:00"

    holiday = _calendar(holidays=frozenset({date(2025, 1, 2)}))
    assert not holiday.is_trading_time(pd.Timestamp("2025-01-02 09:00:00").to_pydatetime())

    spec = InstrumentSpec("ZN", 0.05, 5.0, early, "fees-v1", "roll-v1")
    frame = _frame([make_record("ZN", "2025-01-02 13:01:00", tick=0.05)])
    market = Market(10000, 0.005)
    backtest = Backtest(market, [_LifecycleTraceStrategy(market)], 10000, 0.005)
    try:
        backtest.backtest_trading_day(frame, {"ZN": spec}, trading_day=date(2025, 1, 2))
    except ValueError as exc:
        assert "outside declared calendar" in str(exc)
    else:
        raise AssertionError("Calendar reject disposition must fail outside-window data")

    dropping = _calendar(missing_data_disposition="drop")
    drop_spec = InstrumentSpec("DROP", 0.05, 5.0, dropping, "fees-v1", "roll-v1")
    drop_rows = _frame(
        [
            make_record("DROP", "2025-01-02 08:00:00", tick=0.05),
            make_record("DROP", "2025-01-02 09:00:00", tick=0.05),
        ]
    )
    drop_market = Market(10000, 0.005)
    drop_strategy = _LifecycleTraceStrategy(drop_market)
    drop_backtest = Backtest(drop_market, [drop_strategy], 10000, 0.005)
    drop_backtest.backtest_trading_day(drop_rows, {"DROP": drop_spec}, trading_day=date(2025, 1, 2))
    assert [item for item in drop_strategy.trace if item[0] == "step"] == [("step", "09:00")]


def test_run_date_uses_one_continuous_product_calendar_session():
    calendar = _calendar()
    spec = InstrumentSpec("ZN", 0.05, 5.0, calendar, "fees-v1", "roll-v1")
    market = Market(10000, 0.005)
    strategy = _LifecycleTraceStrategy(market)
    backtest = Backtest(market, [strategy], 10000, 0.005, instrument_specs={"ZN": spec})
    frame = _frame(
        [
            make_record("ZN", "2025-01-01 21:00:00", tick=0.05),
            make_record("ZN", "2025-01-02 09:00:00", tick=0.05),
        ]
    )

    with patch("common.backtest.load", return_value=frame) as load_market_data:
        result = run_date(backtest, date(2025, 1, 2), ["ZN"], "unused-path")

    assert load_market_data.call_count == 1
    assert load_market_data.call_args.kwargs["calendar"] == calendar
    assert [item for item in strategy.trace if item[0] == "reset"] == [("reset", "ZN", "2025-01-02")]
    assert ("break", "night", "01:00", 2) in strategy.trace
    assert not [item for item in strategy.trace if item[0] == "stop"]
    assert result["date"] == "2025-01-02"


def test_product_calendar_controls_order_message_trading_day_identity():
    calendar = SessionCalendar(
        "phase1-late-rollover-v1",
        "Asia/Shanghai",
        windows=(
            SessionWindow("day", time(9, 0), time(19, 30)),
            SessionWindow("night", time(20, 0), time(1, 0)),
        ),
        trading_day_rollover=time(20, 0),
        eod_time=time(19, 30),
    )
    spec = InstrumentSpec("LATE", 0.05, 5.0, calendar, "fees-v1", "roll-v1")
    market = Market(10000, 0.005, instrument_specs={"LATE": spec})
    tracker = OrderLimitTracker(limit=10)
    market.set_order_limit(tracker)
    market.curr = {"LATE": make_record("LATE", "2025-01-01 19:30:00", tick=0.05)}

    market._record_msg("LATE")

    assert tracker.day_report(date(2025, 1, 1))["LATE"]["messages"] == 1


def test_post_rollover_non_crossing_window_ends_on_previous_calendar_date():
    calendar = SessionCalendar(
        "phase1-post-rollover-v1",
        "Asia/Shanghai",
        windows=(SessionWindow("evening", time(19, 0), time(20, 0)),),
        trading_day_rollover=time(18, 0),
        eod_time=time(20, 0),
    )

    end = calendar.window_end_at(date(2025, 1, 2), calendar.windows[0])

    assert end.strftime("%Y-%m-%d %H:%M") == "2025-01-01 20:00"
