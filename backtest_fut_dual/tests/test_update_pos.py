"""PnL-engine tests for common/strategy.py::update_pos — long/short averaging,
position flips, opened_qty, and the round-trip-at-close fee convention.

Fee convention (see CLAUDE.md): FEE / FEE_LOT are the **full round-trip** cost and
are charged **once, on the closing trade**; opens add no fee.
"""

from __future__ import annotations

from common.market import Market
from common.strategy import Strategy

MULT = 10000
TICK = 0.005


def _strat(fee=None, fee_lot=3.0, rebate=0.1, mult=MULT, tick=TICK):
    s = Strategy("T", Market(mult=mult, tick=tick), mult, tick, fee, rebate, fee_lot=fee_lot)
    s.reset("X")
    return s


def fill(px, qty, oid=1):
    return {"px": px, "qty": qty, "order_id": oid}


# ── opening / averaging ────────────────────────────────────────────────────────
def test_open_long_sets_cost_no_fee():
    s = _strat()
    s.update_pos([fill(100.0, 2)])
    assert s.pos == 2 and s.cost == 100.0
    assert s.gross_pnl == 0 and s.pnl == 0 and s.total_fees == 0   # no fee on open


def test_open_short_sets_cost():
    s = _strat()
    s.update_pos([fill(100.0, -2)])
    assert s.pos == -2 and s.cost == 100.0 and s.total_fees == 0


def test_long_weighted_average_cost():
    s = _strat()
    s.update_pos([fill(100.0, 1)])
    s.update_pos([fill(102.0, 1)])
    assert s.pos == 2 and abs(s.cost - 101.0) < 1e-9


def test_short_weighted_average_cost():
    s = _strat()
    s.update_pos([fill(100.0, -1)])
    s.update_pos([fill(98.0, -3)])
    assert s.pos == -4 and abs(s.cost - 98.5) < 1e-9


# ── closing realizes PnL; fee charged once at close ────────────────────────────
def test_close_long_realizes_gross_and_fee_at_close():
    s = _strat(fee_lot=3.0)
    s.update_pos([fill(100.0, 1)])         # open (no fee)
    s.update_pos([fill(101.0, -1)])        # close 1 lot
    assert s.pos == 0
    assert s.gross_pnl == (101.0 - 100.0) * 1 * MULT       # 10_000
    assert s.total_fees == 3.0                              # round-trip fee, once
    assert s.pnl == s.gross_pnl - 3.0


def test_close_short_realizes_gross():
    s = _strat(fee_lot=3.0)
    s.update_pos([fill(100.0, -1)])        # open short
    s.update_pos([fill(99.0, 1)])          # buy to close
    assert s.pos == 0
    assert s.gross_pnl == (100.0 - 99.0) * 1 * MULT
    assert s.total_fees == 3.0


def test_round_trip_charges_fee_only_once():
    s = _strat(fee_lot=3.0)
    s.update_pos([fill(100.0, 2)])         # open 2 -> no fee
    assert s.total_fees == 0
    s.update_pos([fill(100.0, -2)])        # close 2 -> 2 lots * 3, once
    assert s.total_fees == 6.0


def test_rate_based_fee_at_close():
    s = _strat(fee=1e-4, fee_lot=None)
    s.update_pos([fill(100.0, 1)])
    s.update_pos([fill(101.0, -1)])        # fee = close * px * mult * fee = 1*101*10000*1e-4
    assert abs(s.total_fees - 101.0) < 1e-9


# ── position flips ─────────────────────────────────────────────────────────────
def test_flip_long_to_short_resets_cost_to_fill():
    s = _strat()
    s.update_pos([fill(100.0, 1)])         # long 1
    s.update_pos([fill(101.0, -3)])        # sell 3: close 1 long, open short 2
    assert s.pos == -2 and s.cost == 101.0
    assert s.gross_pnl == (101.0 - 100.0) * 1 * MULT       # only the closed lot realizes


def test_flip_short_to_long_resets_cost_to_fill():
    s = _strat()
    s.update_pos([fill(100.0, -1)])        # short 1
    s.update_pos([fill(98.0, 3)])          # buy 3: close 1 short, open long 2
    assert s.pos == 2 and s.cost == 98.0
    assert s.gross_pnl == (100.0 - 98.0) * 1 * MULT


# ── opened_qty tracks opens only ───────────────────────────────────────────────
def test_opened_qty_counts_opens_not_closes():
    s = _strat()
    s.update_pos([fill(100.0, 2)])         # open 2
    s.update_pos([fill(101.0, -2)])        # close 2 (no new open)
    assert s.opened_qty == 2


def test_opened_qty_includes_flip_overflow():
    s = _strat()
    s.update_pos([fill(100.0, 1)])         # open 1
    s.update_pos([fill(101.0, -3)])        # close 1 + open 2 short
    assert s.opened_qty == 1 + 2


# ── multi-fill in a single update_pos call ─────────────────────────────────────
def test_multiple_fills_in_one_call():
    s = _strat(fee_lot=2.0)
    s.update_pos([fill(100.0, 1), fill(100.0, 1), fill(102.0, -2)])
    assert s.pos == 0
    assert s.gross_pnl == (102.0 - 100.0) * 2 * MULT        # both lots closed at 102 vs avg 100
    assert s.total_fees == 2 * 2.0                          # 2 lots closed * fee_lot
