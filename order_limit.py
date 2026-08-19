"""Per-contract, per-trading-day order-message limit tracking.

Exchanges charge extra fees once a contract's total order **messages**
(posts + cancels, including FAK/taker inserts) exceed a daily LIMIT (default
4000). This module provides one shared tracker that:

* counts messages per ``(contract, trading_day)`` — fed from the single Market
  chokepoint (`common/market.py`), so maker posts, cancels, and FAK takers are
  all captured uniformly;
* answers ``over_limit(...)`` so strategies can **suppress new entries** once a
  contract hits the limit (exits / hedges / cancels still proceed);
* records how many entries were suppressed and produces a per-day breach report.

Trading day = the **night session + the following day session**: a timestamp is
mapped with the repo's ``+6 h`` convention (matches ``common.backtest.load_signals``),
so a 21:00 action and the next 09:00–15:00 action share one trading-day bucket.

This is **monitor-only**: no message fee is added to PnL — the tracker counts,
throttles entries, and reports breaches.
"""

from __future__ import annotations

import pandas as pd

# The trading-day map lives in common.sessions (single source); re-export it here
# for the existing callers that import it from common.order_limit.
from common.sessions import TRADING_DAY_SHIFT, trading_day_of  # noqa: F401


class OrderLimitTracker:
    """Counts order messages per (contract, trading_day) and flags the limit.

    One instance is shared across a strategy's legs and across the night/day
    sessions of a trading day (it lives on the Market and is not cleared by
    ``Market.load_md``). Disabled trackers (``enabled=False``) count nothing and
    never report a breach, so the feature is a no-op when turned off.
    """

    def __init__(self, limit: int = 4000, enabled: bool = True):
        self.limit = int(limit)
        self.enabled = bool(enabled)
        self._messages: dict[tuple, int] = {}     # (contract, trading_day) -> message count

    # ── recording ────────────────────────────────────────────────────────────
    @staticmethod
    def _trading_day(ts, calendar=None):
        if calendar is not None:
            return calendar.trading_day_of(pd.Timestamp(ts).to_pydatetime())
        return trading_day_of(ts)

    def record(self, contract, ts, n: int = 1, calendar=None) -> None:
        """Add ``n`` order messages for ``contract`` at timestamp ``ts``."""
        if not self.enabled or n <= 0:
            return
        key = (contract, self._trading_day(ts, calendar))
        self._messages[key] = self._messages.get(key, 0) + int(n)

    # ── queries ──────────────────────────────────────────────────────────────
    def count(self, contract, ts, calendar=None) -> int:
        return self._messages.get((contract, self._trading_day(ts, calendar)), 0)

    def over_limit(self, contract, ts, calendar=None) -> bool:
        """True once the contract's trading-day message count has reached LIMIT.

        Throttle is "stop exactly at the limit": entries are suppressed when the
        count is at or beyond LIMIT (cancels of resting orders may still push it
        higher, which is reported as a breach).
        """
        return self.enabled and self.count(contract, ts, calendar) >= self.limit

    # ── reporting (monitor-only) ─────────────────────────────────────────────
    # ``throttled`` (messages >= limit) means new entries were suppressed from that
    # point on; ``breached`` (messages > limit) means cancels of resting orders
    # pushed the count past the limit after entries stopped. Both are derived from
    # the message count alone — there is no separate suppression counter to drift.
    def day_report(self, trading_day) -> dict[str, dict]:
        """Per-contract row for one trading day:
        {contract: {messages, limit, throttled, breached}}."""
        out: dict[str, dict] = {}
        for (contract, day), msgs in self._messages.items():
            if day == trading_day:
                out[contract] = {
                    "messages": msgs,
                    "limit": self.limit,
                    "throttled": msgs >= self.limit,
                    "breached": msgs > self.limit,
                }
        return out


def day_message_summary(market, date) -> dict:
    """Per-day order-message stats to merge into a run_date result dict.

    Returns {} when no tracker is attached (feature off). ``date`` is the file /
    trading-day date (file D maps to trading day D under the +6h convention).
    ``order_limit_rows`` feeds the per-(contract, day) report (kept out of the
    per-day summary frame by build_summary_frame); the scalars (msg_max /
    msg_breaches / msg_throttled) flow into the per-day summary frame.
    """
    tracker = getattr(market, "order_limit", None)
    if tracker is None or not getattr(tracker, "enabled", False):
        return {}
    trading_day = pd.to_datetime(str(date)).date()
    rep = tracker.day_report(trading_day)
    if not rep:
        return {}
    rows = [{"trading_day": trading_day, "contract": c, "messages": r["messages"],
             "limit": r["limit"], "throttled": r["throttled"], "breached": r["breached"]}
            for c, r in rep.items()]
    return {
        "msg_max": max((r["messages"] for r in rep.values()), default=0),
        "msg_breaches": sum(1 for r in rep.values() if r["breached"]),
        "msg_throttled": sum(1 for r in rep.values() if r["throttled"]),
        "order_limit_rows": rows,
    }


def attach_to_market(market, ns) -> None:
    """Attach an OrderLimitTracker to a Market from a config namespace (a dict,
    e.g. a driver's ``globals()``). For drivers that don't go through
    ``common.grid.run_grid_search`` (e.g. the taker recording/replay drivers)."""
    if market is None or not hasattr(market, "set_order_limit"):
        return
    market.set_order_limit(OrderLimitTracker(
        limit=ns.get("ORDER_MSG_LIMIT", 4000),
        enabled=ns.get("ORDER_LIMIT_ENABLED", True)))
