"""Shared builders for the common/ engine tests.

No market data files needed: construct a Market, set its ``curr`` snapshot, and
drive place_order / cancel / fak / step / match directly. Plain functions (not
pytest fixtures) so the suite runs under the bundled runner AND under pytest.
"""

from __future__ import annotations

import pandas as pd

from common.market import Market

TICK = 0.005
MULT = 10000


def snap(x, tick=TICK):
    return round(round(x / tick) * tick, 6)


def make_record(contract="X", dt="2025-01-02 09:30:00", bid0=100.0, bidvol=50, askvol=50,
                traded_px=None, traded_v1=0.0, traded_v2=0.0, tick=TICK):
    """One MD record in the schema Market consumes (5-level book + traded split).

    ``bid0`` is the best bid (snapped to the tick grid); best ask = bid0 + 1 tick.
    Keep it an exact tick multiple so the book levels land cleanly on the grid.
    """
    bid0 = snap(bid0, tick)
    ask0 = snap(bid0 + tick, tick)
    r = {"contract": contract, "datetime": pd.Timestamp(dt), "timestamp": pd.Timestamp(dt)}
    for i in range(5):
        r[f"bidpx{i}"] = snap(bid0 - i * tick, tick)
        r[f"askpx{i}"] = snap(ask0 + i * tick, tick)
        r[f"bidvol{i}"] = bidvol
        r[f"askvol{i}"] = askvol
    if traded_px is not None:
        p1 = snap(traded_px, tick)
        r.update(traded=traded_px, traded_p1=p1, traded_p2=snap(p1 + tick, tick),
                 traded_v1=traded_v1, traded_v2=traded_v2, totalvol=traded_v1 + traded_v2)
    else:
        nan = float("nan")
        r.update(traded=nan, traded_p1=nan, traded_p2=nan, traded_v1=nan, traded_v2=nan, totalvol=0.0)
    return r


def new_market(record=None):
    m = Market(mult=MULT, tick=TICK)
    m.curr = {}
    if record is not None:
        m.curr[record["contract"]] = record
    return m


def feed(market, record):
    """Inject one record and run Market.step() (queue decay + curr update)."""
    market.md = [record]            # any non-None so step() proceeds
    market.md_records = [record]
    market.md_iter = None
    market.md_len = 1
    market.itr = 0
    return market.step()
