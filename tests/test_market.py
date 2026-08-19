"""Matching-engine tests: queue init/decay, passive & cross fills, FAK, partial
fills, and order-message counting in common/market.py."""

from __future__ import annotations

import pandas as pd
from common.market import PairMarket
from common.order_limit import OrderLimitTracker
from common.tests.helpers import TICK, feed, make_record, new_market, snap


# ── queue construction (FIFO behind resting level volume) ──────────────────────
def test_place_order_queue_includes_level_volume():
    m = new_market(make_record(bid0=100.0, bidvol=50))
    bid0 = m.curr["X"]["bidpx0"]
    order = m.place_order("X", bid0, 1)
    assert order["queue"] == 51            # 1 ours + 50 resting ahead


def test_place_order_fifo_second_sits_behind_first():
    m = new_market(make_record(bid0=100.0, bidvol=50))
    bid0 = m.curr["X"]["bidpx0"]
    o1 = m.place_order("X", bid0, 1)
    o2 = m.place_order("X", bid0, 1)
    assert o1["queue"] == 51 and o2["queue"] == 52


def test_off_book_same_price_orders_are_fifo_stacked():
    m = new_market(make_record(bid0=100.0, bidvol=50, askvol=40))
    bid0 = m.curr["X"]["bidpx0"]
    ask0 = m.curr["X"]["askpx0"]
    deep_bid = snap(bid0 - 6 * TICK)
    deep_ask = snap(ask0 + 6 * TICK)

    b1 = m.place_order("X", deep_bid, 1)
    b2 = m.place_order("X", deep_bid, 1)
    a1 = m.place_order("X", deep_ask, -1)
    a2 = m.place_order("X", deep_ask, -1)

    assert b1["queue"] == 1 and b2["queue"] == 2
    assert a1["queue"] == 1 and a2["queue"] == 2


def test_sell_order_queue_uses_ask_volume():
    m = new_market(make_record(bid0=100.0, askvol=40))
    ask0 = m.curr["X"]["askpx0"]
    order = m.place_order("X", ask0, -2)
    assert order["queue"] == 42            # 2 ours + 40 resting ahead


# ── passive fills via queue decay (step → match) ───────────────────────────────
def test_passive_bid_fills_when_queue_decays_to_zero():
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 1)                                  # queue = 1
    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=bid0, traded_v1=1.0))
    filled = m.match("X")
    assert len(filled) == 1
    assert filled[0]["qty"] == 1 and filled[0]["px"] == bid0


def test_partial_fill_when_queue_below_qty():
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 3)                                  # queue = 3
    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=bid0, traded_v1=1.0))   # queue -> 2
    filled = m.match("X")
    assert len(filled) == 1 and filled[0]["qty"] == 1           # 3 - 2 = 1 filled
    remaining = sum(o["qty"] for o in m.bids["X"][snap(bid0)])
    assert remaining == 2


def test_same_price_child_orders_fill_fifo_not_all_at_once():
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    deep_bid = snap(bid0 - 6 * TICK)
    m.place_order("X", deep_bid, 1)
    m.place_order("X", deep_bid, 1)
    m.place_order("X", deep_bid, 1)

    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=deep_bid, traded_v1=1.0))
    filled = m.match("X")

    assert len(filled) == 1 and filled[0]["qty"] == 1
    assert sum(o["qty"] for o in m.bids["X"][deep_bid]) == 2
    assert [o["queue"] for o in m.bids["X"][deep_bid]] == [1, 2]


def test_no_fill_while_queue_remains():
    m = new_market(make_record(bid0=100.0, bidvol=10))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 1)                                  # queue = 11
    feed(m, make_record(bid0=100.0, bidvol=10, traded_px=bid0, traded_v1=3.0))  # queue -> 8
    assert m.match("X") == []                                    # still behind queue


def test_step_trade_through_bid_clears_full_queue():
    # A trade strictly below our bid clears our level first (price priority).
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 5)                                  # queue = 5
    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=bid0 - 2 * TICK, traded_v1=10.0))
    filled = m.match("X")
    assert len(filled) == 1 and filled[0]["qty"] == 5 and filled[0]["px"] == bid0


def test_bid_on_upper_tick_counts_aggressive_lower_print():
    # Our bid sits on the upper traded tick (px == traded_p2); the more aggressive
    # sell at the lower tick (traded_v1) printed below us and must also deplete us.
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 5)                                  # queue = 5
    # traded_px = bid0 - TICK  ->  p1 = bid0 - TICK, p2 = bid0 (our level)
    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=bid0 - TICK, traded_v1=10.0, traded_v2=2.0))
    filled = m.match("X")
    assert len(filled) == 1 and filled[0]["qty"] == 5 and filled[0]["px"] == bid0


def test_ask_on_lower_tick_counts_aggressive_upper_print():
    # Mirror: our ask on the lower traded tick (px == traded_p1); the more aggressive
    # buy at the upper tick (traded_v2) printed above us and must also deplete us.
    m = new_market(make_record(bid0=100.0, askvol=0))
    ask0 = m.curr["X"]["askpx0"]
    m.place_order("X", ask0, -5)                                 # queue = 5
    # traded_px = ask0  ->  p1 = ask0 (our level), p2 = ask0 + TICK
    feed(m, make_record(bid0=100.0, askvol=0, traded_px=ask0, traded_v1=2.0, traded_v2=10.0))
    filled = m.match("X")
    assert len(filled) == 1 and filled[0]["qty"] == -5 and filled[0]["px"] == ask0


def test_bid_ladder_cascade_does_not_overfill():
    # Two resting bid levels share a finite traded volume (6 lots). Price priority:
    # the best level fills fully (5), only the remainder (1) reaches the next level.
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    lvl2 = snap(bid0 - TICK)
    m.place_order("X", bid0, 5)                                  # queue = 5
    m.place_order("X", lvl2, 5)                                  # queue = 5
    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=bid0 - TICK, traded_v1=6.0, traded_v2=0.0))
    filled = m.match("X")
    by_px = {f["px"]: f["qty"] for f in filled}
    assert by_px[bid0] == 5                                      # best level fully filled
    assert by_px[lvl2] == 1                                      # only the remainder cascades
    assert sum(f["qty"] for f in filled) == 6                    # never exceeds traded volume
    assert sum(o["qty"] for o in m.bids["X"][lvl2]) == 4         # 4 still resting at worse level


def test_ask_ladder_cascade_does_not_overfill():
    # Mirror of the bid ladder: buys consume best ask (lowest px) first.
    m = new_market(make_record(bid0=100.0, askvol=0))
    ask0 = m.curr["X"]["askpx0"]
    lvl2 = snap(ask0 + TICK)
    m.place_order("X", ask0, -5)                                 # queue = 5
    m.place_order("X", lvl2, -5)                                 # queue = 5
    feed(m, make_record(bid0=100.0, askvol=0, traded_px=ask0, traded_v1=0.0, traded_v2=6.0))
    filled = m.match("X")
    by_px = {f["px"]: f["qty"] for f in filled}
    assert by_px[ask0] == -5                                     # best level fully filled
    assert by_px[lvl2] == -1                                     # only the remainder cascades
    assert sum(f["qty"] for f in filled) == -6                   # never exceeds traded volume
    assert sum(o["qty"] for o in m.asks["X"][lvl2]) == -4        # 4 still resting at worse level


def test_no_decay_when_prints_out_of_reach():
    # Trading entirely above our bid must not touch its queue.
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 5)                                  # queue = 5
    feed(m, make_record(bid0=100.0, bidvol=0, traded_px=bid0 + TICK, traded_v1=50.0, traded_v2=50.0))
    assert m.match("X") == []
    assert sum(o["queue"] for o in m.bids["X"][bid0]) == 5       # queue untouched


# ── aggressive / crossing fills ────────────────────────────────────────────────
def test_aggressive_buy_fills_at_best_ask():
    m = new_market(make_record(bid0=100.0))
    ask0 = m.curr["X"]["askpx0"]
    order = m.place_order("X", ask0, 1, aggressive=True)
    filled = m.match("X", order_id=order["order_id"])
    assert len(filled) == 1 and filled[0]["qty"] == 1 and filled[0]["px"] == ask0


def test_aggressive_sell_fills_at_best_bid():
    m = new_market(make_record(bid0=100.0))
    bid0 = m.curr["X"]["bidpx0"]
    order = m.place_order("X", bid0, -1, aggressive=True)
    filled = m.match("X", order_id=order["order_id"])
    assert len(filled) == 1 and filled[0]["qty"] == -1 and filled[0]["px"] == bid0


def test_targeted_aggressive_match_only_touches_its_order():
    m = new_market(make_record(bid0=100.0, askvol=10))
    ask0 = m.curr["X"]["askpx0"]
    ordinary = m.place_order("X", ask0, 1)
    hedge = m.place_order("X", ask0, 2, aggressive=True, metadata={"participation": 1.0})

    filled = m.match("X", order_id=hedge["order_id"])

    assert len(filled) == 1
    assert filled[0]["order_id"] == hedge["order_id"]
    assert filled[0]["qty"] == 2 and filled[0]["px"] == ask0
    assert filled[0]["execution_mode"] == "aggressive_sweep"
    assert ordinary in m.bids["X"][ask0]


def test_broad_match_does_not_execute_an_immediate_order():
    m = new_market(make_record(bid0=100.0))
    ask0 = m.curr["X"]["askpx0"]
    hedge = m.place_order("X", ask0, 1, aggressive=True)

    assert m.match("X") == []
    assert hedge["aggressive"] is True


def test_aggressive_partial_keeps_same_order_as_normal_limit():
    m = new_market(make_record(bid0=100.0, askvol=4))
    ask0 = m.curr["X"]["askpx0"]
    hedge = m.place_order("X", ask0, 5, aggressive=True)

    filled = m.match("X", order_id=hedge["order_id"])

    assert filled[0]["qty"] == 2 and filled[0]["px"] == ask0
    assert hedge["order_id"] == filled[0]["order_id"]
    assert hedge["qty"] == hedge["remaining_qty"] == 3
    assert hedge["aggressive"] is False
    assert m.curr["X"]["askvol0"] == 2


def test_later_marketable_normal_limit_is_depth_limited():
    m = new_market(make_record(bid0=100.0, askvol=4))
    ask0 = m.curr["X"]["askpx0"]
    m.place_order("X", ask0, 5)
    feed(m, make_record(bid0=100.0, askvol=4))

    filled = m.match("X")

    assert len(filled) == 1
    assert filled[0]["qty"] == 2 and filled[0]["px"] == ask0
    assert filled[0]["execution_mode"] == "crossing_limit"
    residual = m.bids["X"][ask0][0]
    assert residual["qty"] == 3 and residual["aggressive"] is False


def test_selective_cancel_repairs_later_fifo_queues():
    m = new_market(make_record(bid0=100.0, bidvol=0))
    bid0 = m.curr["X"]["bidpx0"]
    first = m.place_order("X", bid0, 1)
    second = m.place_order("X", bid0, 1)
    third = m.place_order("X", bid0, 1)

    cancelled = m.cancel_bids("X", bid0, predicate=lambda order: order["order_id"] == first["order_id"])

    assert cancelled == 1
    assert [order["queue"] for order in m.bids["X"][bid0]] == [1, 2]
    assert second["qty"] == third["qty"] == 1


# ── FAK (fill-and-kill) ────────────────────────────────────────────────────────
def test_fak_marketable_buy_partial_and_counts_one():
    m = new_market(make_record(bid0=100.0, askvol=50))
    m.set_order_limit(OrderLimitTracker(limit=1000))
    ask0 = m.curr["X"]["askpx0"]
    f = m.fak("X", ask0, 10)               # avail = round(50 * 0.5) = 25 -> fills 10
    assert f is not None and f["qty"] == 10 and f["px"] == ask0
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 1


def test_fak_marketable_capped_by_available():
    m = new_market(make_record(bid0=100.0, askvol=8))
    ask0 = m.curr["X"]["askpx0"]
    f = m.fak("X", ask0, 100)              # avail = round(8 * 0.5) = 4
    assert f["qty"] == 4


def test_fak_reject_returns_none_and_not_counted():
    m = new_market(make_record(bid0=100.0))
    m.set_order_limit(OrderLimitTracker(limit=1000))
    f = m.fak("X", 90.0, 1)               # far below the ask -> price mismatch
    assert f is None
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 0


# ── FAK depth sweep (opt-in; walks levels up to the limit px) ───────────────────
def test_fak_default_off_is_unchanged():
    # Without sweep=, a touch-priced FAK takes level 0 only (round(8*0.5)=4) — identical to today.
    m = new_market(make_record(bid0=100.0, askvol=8))
    ask0 = m.curr["X"]["askpx0"]
    assert m.fak("X", ask0, 100)["qty"] == 4
    assert m.fak("X", ask0, 100, sweep=False)["qty"] == 4


def test_fak_sweep_walks_into_depth_and_vwaps():
    # level0 px=ask0 vol=8 (avail 4); level1 px=ask0+tick vol=20 (avail 10). A buy of 12 with a
    # 1-tick offset (limit=ask0+tick) sweeps 4@ask0 + 8@ask0+tick = 12, priced at their VWAP; one message.
    rec = make_record(bid0=100.0, askvol=8); rec["askvol1"] = 20
    m = new_market(rec); m.set_order_limit(OrderLimitTracker(limit=1000))
    ask0 = m.curr["X"]["askpx0"]; lvl1 = ask0 + TICK
    f = m.fak("X", lvl1, 12, sweep=True)
    assert f["qty"] == 12
    assert abs(f["px"] - (4 * ask0 + 8 * lvl1) / 12) < 1e-9     # VWAP across the two levels
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 1   # one order, two levels


def test_fak_sweep_at_touch_reaches_level0_only():
    # A touch-priced sweep is backward-compatible: it can't reach level 1.
    rec = make_record(bid0=100.0, askvol=8); rec["askvol1"] = 20
    m = new_market(rec)
    ask0 = m.curr["X"]["askpx0"]
    assert m.fak("X", ask0, 12, sweep=True)["qty"] == 4         # level 0 only (askpx1 > ask0)


def test_fak_sweep_caps_each_level_by_fak_avail():
    # FAK_AVAIL=0.5 still caps EACH swept level: 8 -> 4 takeable, not 8.
    m = new_market(make_record(bid0=100.0, askvol=8))
    ask0 = m.curr["X"]["askpx0"]
    assert m.fak("X", ask0, 100, sweep=True)["qty"] == 4


def test_fak_sweep_stops_at_limit_price():
    # Deep size at level 1, but the limit is the touch -> level 1 is out of reach.
    rec = make_record(bid0=100.0, askvol=4); rec["askvol1"] = 100
    m = new_market(rec)
    ask0 = m.curr["X"]["askpx0"]
    assert m.fak("X", ask0, 50, sweep=True)["qty"] == 2         # round(4*0.5), level 1 not swept


def test_fak_sweep_respects_a_gapped_book():
    # level 1 exists but its price is 3 ticks away; a 1-tick offset can't reach it.
    rec = make_record(bid0=100.0, askvol=10); rec["askvol1"] = 100
    rec["askpx1"] = snap(rec["askpx0"] + 3 * TICK)
    m = new_market(rec)
    ask0 = m.curr["X"]["askpx0"]
    assert m.fak("X", ask0 + TICK, 50, sweep=True)["qty"] == 5  # round(10*0.5); gap blocks level 1


def test_fak_sweep_sell_side():
    rec = make_record(bid0=100.0, bidvol=8); rec["bidvol1"] = 20
    m = new_market(rec)
    bid0 = m.curr["X"]["bidpx0"]; lvl1 = bid0 - TICK
    f = m.fak("X", lvl1, -12, sweep=True)
    assert f["qty"] == -12
    assert abs(f["px"] - (4 * bid0 + 8 * lvl1) / 12) < 1e-9


def test_fak_sweep_reject_returns_none():
    m = new_market(make_record(bid0=100.0))
    m.set_order_limit(OrderLimitTracker(limit=1000))
    assert m.fak("X", 90.0, 1, sweep=True) is None
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 0


def test_snapshot_ticket_api_is_removed():
    m = new_market(make_record(bid0=100.0))
    assert not hasattr(m, "submit_interval_limit")
    assert not hasattr(m, "settle_interval_limit")
    assert not hasattr(m, "cancel_interval_limit")


# ── order-message counting (post / cancel / missing datetime) ──────────────────
def test_place_counts_one_message():
    m = new_market(make_record(bid0=100.0))
    m.set_order_limit(OrderLimitTracker(limit=1000))
    m.place_order("X", m.curr["X"]["bidpx0"], 1)
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 1


def test_cancel_counts_one_message_per_order():
    m = new_market(make_record(bid0=100.0))
    m.set_order_limit(OrderLimitTracker(limit=1000))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0 - TICK, 1)             # 1 msg
    m.place_order("X", bid0 - 2 * TICK, 1)         # 1 msg
    m.cancel_all_bids("X")                          # 2 msgs (one per resting order)
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 4


def test_cancel_bids_at_level_counts_orders_removed():
    m = new_market(make_record(bid0=100.0))
    m.set_order_limit(OrderLimitTracker(limit=1000))
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0 - TICK, 1)
    m.place_order("X", bid0 - TICK, 1)             # same level, 2 orders
    base = m.order_limit.count("X", m.curr["X"]["datetime"])
    m.cancel_bids("X", bid0 - TICK)                 # cancels 2 orders -> +2
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) - base == 2


def test_missing_datetime_is_not_counted():
    rec = make_record(bid0=100.0)
    rec["datetime"] = None                          # NaT-like trading day must not bucket
    m = new_market(rec)
    m.set_order_limit(OrderLimitTracker(limit=10))
    m.place_order("X", rec["bidpx0"], 1)
    assert sum(m.order_limit._messages.values()) == 0


def test_disabled_tracker_counts_nothing():
    m = new_market(make_record(bid0=100.0))
    m.set_order_limit(OrderLimitTracker(limit=10, enabled=False))
    m.place_order("X", m.curr["X"]["bidpx0"], 1)
    assert m.order_limit.count("X", m.curr["X"]["datetime"]) == 0


# ── PairMarket alignment (dual-contract infra) ────────────────────────────────
def _pair_frame(contract, times, *, base=100.0, recv_times=None):
    recv_times = recv_times if recv_times is not None else times
    rows = []
    for i, (dt, recv) in enumerate(zip(times, recv_times)):
        row = make_record(contract=contract, dt=dt, bid0=base + i * TICK)
        row["timestamp"] = pd.Timestamp(recv)
        rows.append(row)
    return pd.DataFrame(rows).set_index(pd.DatetimeIndex(times, name="exchtime"))


def test_pairmarket_steps_by_exchtime_not_receive_timestamp():
    times = pd.to_datetime(["2025-01-02 21:00:00", "2025-01-02 21:00:01", "2025-01-02 21:00:02"])
    reversed_recv = list(reversed(times))
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(
        _pair_frame("P", times, base=100.0, recv_times=reversed_recv),
        _pair_frame("S", times, base=200.0, recv_times=reversed_recv),
    )

    seen = []
    while True:
        bundle = m.step_pair()
        if bundle is None:
            break
        seen.append(bundle["datetime"])
        assert bundle["updated"] == ["P", "S"]
        assert m.curr["P"]["datetime"] == bundle["datetime"]
        assert m.curr["S"]["datetime"] == bundle["datetime"]
        assert m.itr == 2 * len(seen)

    assert seen == list(times)


def test_pairmarket_normalizes_unsorted_leg_input_by_exchange_time():
    base = pd.Timestamp("2025-01-02 21:00:00")
    times = pd.DatetimeIndex([base, base + pd.Timedelta(seconds=1)])
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(
        _pair_frame("P", list(reversed(times)), base=100.0),
        _pair_frame("S", times, base=200.0),
    )

    bundles = [m.step_pair(), m.step_pair()]
    assert [bundle["datetime"] for bundle in bundles] == list(times)
    assert [bundle["updated"] for bundle in bundles] == [["P", "S"], ["P", "S"]]
    assert m.step_pair() is None


def test_pairmarket_keeps_latest_receive_observation_for_duplicate_exchange_time():
    exchtime = pd.Timestamp("2025-01-02 21:00:00")
    p_frame = _pair_frame(
        "P",
        [exchtime, exchtime],
        base=100.0,
        recv_times=[exchtime + pd.Timedelta("100ns"), exchtime + pd.Timedelta("200ns")],
    )
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(p_frame, _pair_frame("S", [exchtime], base=200.0))

    bundle = m.step_pair()
    assert bundle["updated"] == ["S", "P"]
    assert bundle["rows"]["P"]["bidpx0"] == 100.0 + TICK
    assert m.step_pair() is None


def test_pairmarket_sparse_legs_forward_fill_current_snapshot():
    base = pd.Timestamp("2025-01-02 21:00:00")
    p_times = pd.DatetimeIndex([base, base + pd.Timedelta(seconds=1), base + pd.Timedelta(seconds=3)])
    s_times = pd.DatetimeIndex([base, base + pd.Timedelta(seconds=2)])
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(_pair_frame("P", p_times), _pair_frame("S", s_times, base=200.0))

    first = m.step_pair()
    second = m.step_pair()

    assert first["updated"] == ["P", "S"]
    assert second["updated"] == ["P"]
    assert set(second["rows"]) == {"P"}
    assert m.curr["S"]["datetime"] == second["datetime"]

    third = m.step_pair()
    fourth = m.step_pair()

    assert third["updated"] == ["S"]
    assert fourth["updated"] == ["P"]
    assert m.step_pair() is None

    assert second["datetime"] == base + pd.Timedelta(seconds=1)
    assert third["datetime"] == base + pd.Timedelta(seconds=2)
    assert m.curr["P"]["datetime"] == base + pd.Timedelta(seconds=3)
    assert m.curr["S"]["datetime"] == base + pd.Timedelta(seconds=3)
    assert m.itr == 5


def test_pairmarket_same_exchtime_tie_breaks_by_receive_timestamp():
    exchtime = pd.Timestamp("2025-01-02 21:00:00")
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(
        _pair_frame("P", [exchtime], recv_times=[exchtime + pd.Timedelta("200ns")]),
        _pair_frame("S", [exchtime], recv_times=[exchtime + pd.Timedelta("100ns")]),
    )
    assert m.step_pair()["updated"] == ["S", "P"]

    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(_pair_frame("P", [exchtime]), _pair_frame("S", [exchtime]))
    assert m.step_pair()["updated"] == ["P", "S"]


def test_pairmarket_forward_fill_clears_trade_flow_without_touching_quote_depth():
    base = pd.Timestamp("2025-01-02 21:00:00")
    p_times = pd.DatetimeIndex([base, base + pd.Timedelta(seconds=1)])
    s_first = make_record("S", base, bid0=200.0, bidvol=0, traded_px=200.0, traded_v1=5.0)
    s_first["timestamp"] = base
    s_first["turnover"] = 123.0
    s_first["volume"] = 5.0
    s_frame = pd.DataFrame([s_first]).set_index(pd.DatetimeIndex([base], name="exchtime"))
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(_pair_frame("P", p_times), s_frame)

    m.step_pair()
    bid0 = m.curr["S"]["bidpx0"]
    order = m.place_order("S", bid0, 1)
    second = m.step_pair()

    carried = m.curr["S"]
    assert second["updated"] == ["P"]
    assert carried["datetime"] == second["datetime"]
    assert carried["bidpx0"] == bid0 and carried["bidvol0"] == 0
    assert pd.isna(carried["traded"]) and pd.isna(carried["traded_p1"]) and pd.isna(carried["traded_p2"])
    assert carried["traded_v1"] == carried["traded_v2"] == 0
    assert carried["totalvol"] == carried["turnover"] == carried["volume"] == 0
    assert order["queue"] == 1


def test_pairmarket_forward_fill_reloads_supplied_depth_after_immediate_sweep():
    base = pd.Timestamp("2025-01-02 21:00:00")
    p_times = pd.DatetimeIndex([base, base + pd.Timedelta(seconds=1)])
    s_first = make_record("S", base, bid0=200.0, askvol=5)
    s_first["timestamp"] = base
    s_frame = pd.DataFrame([s_first]).set_index(pd.DatetimeIndex([base], name="exchtime"))
    m = PairMarket(mult=10000, tick=TICK)
    m.load_pair(_pair_frame("P", p_times), s_frame)

    m.step_pair()
    ask0 = m.curr["S"]["askpx0"]
    hedge = m.place_order("S", ask0, 2, aggressive=True, metadata={"participation": 1.0})
    assert m.match("S", order_id=hedge["order_id"])[0]["qty"] == 2
    assert m.curr["S"]["askvol0"] == 3

    m.step_pair()

    assert m.curr["S"]["askvol0"] == 5
