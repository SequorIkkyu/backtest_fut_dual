"""Current-engine characterizations to replace when their remediation phase lands.

These tests deliberately prove defects in the current engine. When a later phase
fixes one, replace the corresponding assertion with its target-state acceptance
test in the same change; do not retain obsolete behaviour as a requirement.
"""

from __future__ import annotations

import inspect

import pandas as pd

from common import backtest as backtest_module
from common import grid, reporting
from common.backtest import Backtest, run_pair_session
from common.market import Market, PairMarket
from common.sessions import DAY_SESSION, NIGHT_SESSION
from common.strategy import Strategy
from common.tests.helpers import make_record


class _LifecycleTraceStrategy(Strategy):
    def __init__(self, market):
        super().__init__("trace", market, 10000, 0.005, None, 0.0)
        self.trace = []

    def reset(self, contract, trading_date=None):
        self.trace.append(("reset", contract))
        return super().reset(contract, trading_date)

    def stop(self):
        self.trace.append(("stop", self.contract))
        return super().stop()


def _pair_frame(rows):
    return pd.DataFrame(rows).set_index(pd.DatetimeIndex([row["datetime"] for row in rows], name="exchtime"))


def test_legacy_session_segments_reset_and_stop_independently():
    market = Market(mult=10000, tick=0.005)
    strategy = _LifecycleTraceStrategy(market)
    backtest = Backtest(market, [strategy], 10000, 0.005)
    frame = pd.DataFrame([make_record(contract="X")]).set_index("datetime")
    backtest.backtest(frame, ["X"])
    backtest.backtest(frame, ["X"])
    assert strategy.trace == [("reset", "X"), ("stop", "X"), ("reset", "X"), ("stop", "X")]


def test_legacy_fak_reports_limit_and_preserves_consumed_depth():
    market = Market(mult=10000, tick=0.005)
    record = make_record(askvol=10)
    market.curr = {"X": record}
    ask = record["askpx0"]
    submitted_limit = market.snap_price(ask + 0.005)
    direct = market.fak("X", submitted_limit, 3)
    assert direct["qty"] == 3
    assert direct["px"] == submitted_limit != ask
    assert market.curr["X"]["askvol0"] == 10

    market = Market(mult=10000, tick=0.005)
    record = make_record(askvol=10)
    market.curr = {"X": record}
    sweep = market.fak("X", record["askpx0"] + 0.005, 7, sweep=True)
    assert sweep["qty"] == 7
    assert market.curr["X"]["askvol0"] == market.curr["X"]["askvol1"] == 10


def test_legacy_pair_forward_fill_restores_consumed_depth():
    base = pd.Timestamp("2025-01-02 21:00:00")
    p_rows = [make_record("P", base + pd.Timedelta(seconds=index)) for index in (0, 1)]
    s_row = make_record("S", base, askvol=5)
    market = PairMarket(mult=10000, tick=0.005)
    market.load_pair(_pair_frame(p_rows), _pair_frame([s_row]))
    market.step_pair()
    ask = market.curr["S"]["askpx0"]
    first = market.place_order("S", ask, 3, aggressive=True, metadata={"participation": 1.0})
    assert market.match("S", first["order_id"])[0]["qty"] == 3
    assert market.curr["S"]["askvol0"] == 2
    assert market.step_pair()["updated"] == ["P"]
    assert market.curr["S"]["askvol0"] == 5
    second = market.place_order("S", ask, 3, aggressive=True, metadata={"participation": 1.0})
    assert market.match("S", second["order_id"])[0]["qty"] == 3


def test_legacy_pair_replay_uses_exchange_time_and_lacks_sequences():
    p_exchange = pd.Timestamp("2025-01-02 21:00:00")
    s_exchange = pd.Timestamp("2025-01-02 21:00:01")
    p_row = make_record("P", p_exchange)
    p_row["timestamp"] = pd.Timestamp("2025-01-02 21:00:02")
    s_row = make_record("S", s_exchange)
    s_row["timestamp"] = pd.Timestamp("2025-01-02 21:00:00")
    market = PairMarket(mult=10000, tick=0.005)
    market.load_pair(_pair_frame([p_row]), _pair_frame([s_row]))
    first, second = market.step_pair(), market.step_pair()
    assert first["updated"] == ["P"]
    assert second["updated"] == ["S"]
    assert not hasattr(market, "feed_seq") and not hasattr(market, "book_seq")


def test_single_book_market_preserves_exchange_time_but_audits_by_receive_time():
    late_received = make_record("X", "2025-01-02 09:00:00")
    late_received["timestamp"] = pd.Timestamp("2025-01-02 09:00:02")
    early_received = make_record("X", "2025-01-02 09:00:01")
    early_received["timestamp"] = pd.Timestamp("2025-01-02 09:00:00")
    market = Market(mult=10000, tick=0.005)
    market.load_md(pd.DataFrame([early_received, late_received], index=pd.DatetimeIndex([
        "2025-01-02 09:00:01", "2025-01-02 09:00:00"], name="exchtime"
    )))
    first, second = market.step(), market.step()
    assert first["timestamp"] < second["timestamp"]
    assert first["datetime"] < second["datetime"]
    assert first["exchange_ts"] > second["exchange_ts"]


def test_legacy_cancel_diagnostics_log_before_min_count_filtering():
    market = Market(mult=10000, tick=0.005)
    record = make_record("X")
    market.curr = {"X": record}
    strategy = Strategy(
        "cancel-diagnostic",
        market,
        10000,
        0.005,
        None,
        0.0,
        parallel=False,
        record_diagnostics=True,
    )
    strategy.reset("X")
    strategy.curr_md = record
    order = strategy.submit_order(record["bidpx0"], 1)

    strategy.bid_and_cancel([], min_count=1)

    cancel_events = [event for event in strategy.event_log if event["event_type"] == "cancel"]
    assert len(cancel_events) == 1 and cancel_events[0]["order_id"] == order["order_id"]
    assert market.bids["X"][record["bidpx0"]][0]["order_id"] == order["order_id"]


def test_legacy_pair_v2_is_explicitly_archived_outside_the_foundation_api():
    market_parameters = inspect.signature(Market).parameters
    pair_parameters = inspect.signature(run_pair_session).parameters
    assert "engine_version" not in market_parameters
    assert "trading_date" not in pair_parameters
    assert not hasattr(Market, "match_batch")


def test_legacy_telemetry_is_optional_strategy_state_and_reconciliation_is_advisory():
    market = Market(mult=10000, tick=0.005)
    strategy = Strategy("trace", market, 10000, 0.005, None, 0.0)
    strategy.reset("X")
    assert isinstance(strategy.event_log, list)
    assert isinstance(strategy.session_record, list)
    assert not hasattr(market, "telemetry")
    reconcile_source = inspect.getsource(reporting._reconcile)
    assert "*** WARN" in reconcile_source
    assert "raise " not in reconcile_source


def test_legacy_capacity_and_latency_surfaces_are_not_generic():
    assert NIGHT_SESSION == ("21:00", "03:00")
    assert DAY_SESSION == ("09:00", "15:00")
    for capability in ("reserve_capacity", "release_capacity", "ledger", "schedule_arrival"):
        assert not hasattr(Market, capability)
    assert "latency" not in inspect.getsource(Backtest).lower()


def test_legacy_base_classes_contain_policy_state_and_unbounded_records():
    market = Market(mult=10000, tick=0.005)
    strategy = Strategy("trace", market, 10000, 0.005, None, 0.0)
    strategy.reset("X")
    assert strategy.auto_unwind is False
    assert hasattr(strategy, "pred_leg1_move")
    assert hasattr(strategy, "pred_spread_same_sign_count")
    assert "offset" in inspect.getsource(Strategy.hedge)
    fill_source = inspect.getsource(Market._build_fill_event)
    assert "prediction_close_trigger_id" in fill_source
    assert ".append(event)" in inspect.getsource(Strategy.record_event)
    assert ".append(record)" in inspect.getsource(Strategy.step)


def test_legacy_grid_and_market_cleaning_remain_compatibility_only_after_foundation_loader_hardening():
    manifest_source = inspect.getsource(grid.write_run_manifest)
    assert '"git": _git_info()' in manifest_source
    assert "data_hash" not in manifest_source and "signal_hash" not in manifest_source
    signal_source = inspect.getsource(backtest_module.load_signals)
    assert "declared_contract_universe" in signal_source
    assert "nth(-2)" not in signal_source and "nth(-1)" not in signal_source
    loader_source = inspect.getsource(backtest_module.load)
    assert 'c.loc[c["bidvol0"] <= 0, "bidpx0"]' in loader_source
    fak_source = inspect.getsource(Market.fak)
    assert "FAK_AVAIL" in fak_source
    market = Market(mult=10000, tick=0.005)
    record = make_record(askvol=10)
    market.curr = {"X": record}
    assert set(market.fak("X", record["askpx0"], 1)) == {"px", "qty"}
