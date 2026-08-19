# import numpy as np

import math
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

FAK_AVAIL = 0.5

class Market:
    def __init__(self, mult: int, tick: float, verbose: bool = False, instrument_specs=None):
        self.mult = mult
        self.tick = tick
        self.price_tol = tick / 10
        self.verbose = verbose
        self._tick_decimal = Decimal(str(tick))
        self.instrument_specs = {}
        self.set_instrument_specs(instrument_specs or {})

        self.md = None
        self.md_records = None
        self.md_iter = None
        self.md_len = 0
        self.itr = 0
        self.curr = None
        self.bids = {}
        self.asks = {}
        self.bid_qty = {}
        self.ask_qty = {}
        self.next_order_id = 1
        # Shared per-(contract, trading-day) order-message tracker (set via
        # set_order_limit). Survives load_md so it spans the night+day sessions.
        self.order_limit = None

    def set_instrument_specs(self, instrument_specs):
        """Register immutable per-product specs for foundation scheduling.

        Legacy callers may leave this empty and retain the constructor's
        ``mult``/``tick`` defaults.  The market only requires product, tick, and
        multiplier attributes, avoiding a dependency on the contract module.
        """
        if hasattr(instrument_specs, "items"):
            items = instrument_specs.items()
        else:
            items = ((spec.product, spec) for spec in instrument_specs)
        specs = {}
        for product, spec in items:
            if not isinstance(product, str) or not product:
                raise ValueError("instrument spec product keys must be non-empty strings")
            if getattr(spec, "product", None) != product:
                raise ValueError("instrument spec key must match spec.product")
            tick = getattr(spec, "tick", None)
            multiplier = getattr(spec, "multiplier", None)
            if tick is None or float(tick) <= 0 or multiplier is None or float(multiplier) <= 0:
                raise ValueError("instrument specs must have positive tick and multiplier")
            specs[product] = spec
        self.instrument_specs = specs

    def instrument_spec_for(self, contract: str):
        return self.instrument_specs.get(contract)

    def tick_for(self, contract: str | None = None) -> float:
        spec = self.instrument_spec_for(contract) if contract is not None else None
        return float(spec.tick) if spec is not None else float(self.tick)

    def mult_for(self, contract: str | None = None) -> float:
        spec = self.instrument_spec_for(contract) if contract is not None else None
        return float(spec.multiplier) if spec is not None else float(self.mult)

    def price_tol_for(self, contract: str | None = None) -> float:
        return self.tick_for(contract) / 10

    def set_order_limit(self, tracker):
        self.order_limit = tracker

    def _record_msg(self, contract, n: int = 1, event_time=None):
        """Record n exchange order messages (post / cancel / FAK) for contract.

        Most engine actions are issued against ``curr`` and therefore use its
        timestamp; callers with an explicit exchange event time may supply it.
        """
        if self.order_limit is None or n <= 0:
            return
        if event_time is not None:
            dt = pd.Timestamp(event_time)
        else:
            curr = self.curr.get(contract)
            if curr is None:
                return
            dt = curr.get("datetime")
        if dt is None or pd.isna(dt):   # don't mis-bucket under a NaT trading day
            return
        spec = self.instrument_spec_for(contract)
        calendar = getattr(spec, "calendar", None)
        self.order_limit.record(contract, dt, n, calendar=calendar)

    def _snap_price(self, px: float, contract: str | None = None):
        if pd.isna(px):
            return px

        tick_decimal = Decimal(str(self.tick_for(contract)))
        snapped = float(
            (Decimal(str(px)) / tick_decimal).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick_decimal
        )

        if math.isclose(px, snapped, abs_tol=self.price_tol_for(contract)):
            return snapped

        return px

    def snap_price(self, px: float, contract: str | None = None):
        return self._snap_price(px, contract)

    def _price_eq(self, left: float, right: float, contract: str | None = None):
        if pd.isna(left) or pd.isna(right):
            return False

        left = self._snap_price(left, contract)
        right = self._snap_price(right, contract)

        return math.isclose(left, right, abs_tol=self.price_tol_for(contract))

    def _price_ge(self, left: float, right: float, contract: str | None = None):
        if pd.isna(left) or pd.isna(right):
            return False

        left = self._snap_price(left, contract)
        right = self._snap_price(right, contract)

        return left > right or self._price_eq(left, right, contract)

    def _price_gt(self, left: float, right: float, contract: str | None = None):
        if pd.isna(left) or pd.isna(right):
            return False

        left = self._snap_price(left, contract)
        right = self._snap_price(right, contract)

        return left > right and not self._price_eq(left, right, contract)

    def _price_le(self, left: float, right: float, contract: str | None = None):
        return self._price_ge(right, left, contract)

    def _price_lt(self, left: float, right: float, contract: str | None = None):
        return self._price_gt(right, left, contract)

    def load_md(self, md: pd.DataFrame):
        self.md = md
        self.md_records = None
        self.md_iter = None
        self.md_len = 0
        self.itr = 0
        self.curr = {}
        self.bids = {}
        self.asks = {}
        self.bid_qty = {}
        self.ask_qty = {}
        self.next_order_id = 1

        if isinstance(md, pd.DataFrame):
            records = md.to_dict("records")
            for record, exchange_ts in zip(records, md.index.to_list()):
                # The historic dataframe index is exchange time.  Preserve it
                # as analysis data, but make the event/audit clock receive
                # time so callers that supplied receive-ordered data do not
                # silently revert to exchange-time decisions.
                recv_ts = record.get("recv_ts", record.get("timestamp", exchange_ts))
                if recv_ts is None or pd.isna(recv_ts):
                    recv_ts = exchange_ts
                record["exchange_ts"] = exchange_ts
                record["recv_ts"] = recv_ts
                record["datetime"] = recv_ts
            self.md_records = records
            self.md_len = len(records)
        elif hasattr(md, "iter_rows") and hasattr(md, "height"):
            self.md_iter = md.iter_rows(named=True)
            self.md_len = md.height
        else:
            self.md_records = list(md)
            self.md_len = len(self.md_records)

        if self.verbose:
            print("\nMD Loaded:")
            print(self.md)

    def _normalize_price(self, px: float, contract: str | None = None) -> float:
        return self._snap_price(px, contract)

    def _same_price(self, lhs: float, rhs: float, contract: str | None = None) -> bool:
        return self._price_eq(lhs, rhs, contract)

    def _resolve_book_level(self, book: dict[float, list[dict]], px: float, contract: str | None = None) -> float | None:
        normalized_px = self._normalize_price(px, contract)
        if normalized_px in book:
            return normalized_px
        for level_px in book:
            if self._same_price(level_px, normalized_px, contract):
                return level_px
        return None

    def _resting_abs_qty_at_level(self, book: dict[float, list[dict]], px: float, contract: str | None = None) -> int:
        level_px = self._resolve_book_level(book, px, contract)
        if level_px is None:
            return 0
        return sum(abs(order["qty"]) for order in book[level_px])

    def _visible_level_volume(self, curr: dict, side: str, px: float) -> int:
        """Return displayed whole-lot depth at one price of ``curr``."""
        i = 0
        while f"{side}px{i}" in curr:
            if self._price_eq(px, curr[f"{side}px{i}"], curr.get("contract")):
                value = curr.get(f"{side}vol{i}", 0)
                if value is None or pd.isna(value):
                    return 0
                return max(0, int(math.floor(float(value))))
            i += 1
        return 0

    def _rebase_level_queues(self, contract: str, side: str, px: float):
        """Rebuild one level's FIFO queues from external depth and resting orders."""
        book = self.bids if side == "bid" else self.asks
        levels = book.get(contract, {})
        level_px = self._resolve_book_level(levels, px, contract)
        if level_px is None:
            return
        ahead = self._visible_level_volume(self.curr[contract], side, level_px)
        for order in levels[level_px]:
            if order.get("aggressive", False):
                continue
            ahead += abs(order["qty"])
            order["queue"] = ahead

    def _decay_book_side(self, book, is_bid, tp1, tp2, tv1, tv2, has_l1, has_l2, contract: str):
        """Deplete resting queues from this tick's two traded sub-levels.

        Trading is consumed by price priority with a finite, shared budget: the best
        level absorbs up to its deepest requirement (book ahead + our size) before any
        surplus cascades to worse levels.

        - is_bid=True  -> sells deplete bids; best level = highest px; a sub-level at
          price P reaches our orders with px >= P (lower P is more aggressive).
        - is_bid=False -> buys deplete asks;  best level = lowest px;  a sub-level at
          price P reaches our orders with px <= P (higher P is more aggressive).

        Within a level, depletion is independent (queues are cumulative FIFO); across
        levels, the budget is shared so multiple resting levels cannot all fill on the
        same finite volume.
        """
        if not (has_l1 or has_l2):
            return

        b1 = float(tv1) if has_l1 else 0.0   # volume printed at tp1 (lower tick)
        b2 = float(tv2) if has_l2 else 0.0   # volume printed at tp2 (upper tick)

        for px in sorted(book.keys(), reverse=is_bid):      # best level first
            if b1 <= 0 and b2 <= 0:
                break

            if is_bid:                                       # sells reach bids px >= P
                use1 = b1 if (has_l1 and self._price_ge(px, tp1, contract)) else 0.0
                use2 = b2 if (has_l2 and self._price_ge(px, tp2, contract)) else 0.0
            else:                                            # buys reach asks px <= P
                use1 = b1 if (has_l1 and self._price_le(px, tp1, contract)) else 0.0
                use2 = b2 if (has_l2 and self._price_le(px, tp2, contract)) else 0.0

            avail = use1 + use2
            if avail <= 0:
                continue

            orders = book[px]
            level_req = max((order["queue"] for order in orders), default=0.0)  # deepest req, pre-decay
            for order in orders:
                if order["queue"] > 0:
                    order["queue"] -= avail                  # within-level: cumulative FIFO

            absorbed = min(avail, level_req if level_req > 0 else 0.0)   # volume that stopped here
            # Consume the budget whose reach expires first as we move to worse levels:
            #   bids descend -> b2 (reach >= p2) expires first; asks ascend -> b1 expires first.
            if is_bid:
                take2 = min(use2, absorbed)
                b2 -= take2
                absorbed -= take2
                take1 = min(use1, absorbed)
                b1 -= take1
            else:
                take1 = min(use1, absorbed)
                b1 -= take1
                absorbed -= take1
                take2 = min(use2, absorbed)
                b2 -= take2

    def _apply_tick(self, curr):
        """Apply ONE market-data row: record it as this contract's current snapshot, then age + decay this
        contract's resting books by the row's own traded sub-levels. Per-contract and self-contained, so a
        subclass (PairMarket) can call it once per contract that ticks at an exchtime. Output is identical to
        the inline block step() previously ran (pure extract-method; single-contract behaviour unchanged)."""
        # ``curr`` is interval-local state, never a mutable loader record.
        curr = dict(curr)
        contract = curr["contract"]
        self.curr[contract] = curr

        # This tick's two decomposed traded sub-levels (lower tick p1, upper tick p2).
        tp1, tp2 = curr.get("traded_p1"), curr.get("traded_p2")
        tv1, tv2 = curr.get("traded_v1"), curr.get("traded_v2")
        has_l1 = pd.notna(tp1) and pd.notna(tv1)
        has_l2 = pd.notna(tp2) and pd.notna(tv2)

        # Pass 1 — age every resting order (independent of fills, so the cascade's
        # early-out below never skips an order's count increment).
        if contract in self.bids:
            for orders in self.bids[contract].values():
                for order in orders:
                    order["count"] += 1

        if contract in self.asks:
            for orders in self.asks[contract].values():
                for order in orders:
                    order["count"] += 1

        # Pass 2 — deplete queues by this tick's traded volume using price-priority
        # with a finite, shared budget cascaded best-level-first.
        if contract in self.bids:
            self._decay_book_side(self.bids[contract], True, tp1, tp2, tv1, tv2, has_l1, has_l2, contract)

        if contract in self.asks:
            self._decay_book_side(self.asks[contract], False, tp1, tp2, tv1, tv2, has_l1, has_l2, contract)

    def step(self):
        if self.md is not None:
            if self.itr >= self.md_len:
                # print('\n - End of market data reached.')

                return None
            else:
                if self.md_iter is not None:
                    try:
                        curr = next(self.md_iter)
                    except StopIteration:
                        return None
                else:
                    curr = self.md_records[self.itr]

                self.itr += 1

                self._apply_tick(curr)

                return curr
        else:
            print("\n - No market data loaded.")

            return None

    def place_order(
        self,
        contract: str,
        px: float,
        qty: int,
        aggressive: bool = False,
        metadata: dict | None = None,
        event_time=None,
    ):
        px = self._snap_price(px, contract)

        if self.verbose:
            print(f" - Placed <{contract}> {'Aggressive' if aggressive else 'Limit'} Order: {qty} @ {px}")

        curr = self.curr[contract]
        px = self._normalize_price(px, contract)
        order_time = pd.Timestamp(event_time) if event_time is not None else curr.get("datetime")
        order = {
            "qty": qty,
            "px": px,
            "queue": 0,
            "count": 0,
            "aggressive": aggressive,
            "order_id": self.next_order_id,
            "created_at": order_time,
            "created_tick": self.itr,
            "initial_qty": qty,
            "remaining_qty": qty,
            "side": "buy" if qty > 0 else "sell",
        }
        if metadata:
            order.update(metadata)
        self.next_order_id += 1

        if qty > 0:
            q = qty + self._visible_level_volume(curr, "bid", px)
            q += self._resting_abs_qty_at_level(self.bids.get(contract, {}), px, contract)

            self.bid_qty[contract] = self.bid_qty.get(contract, 0) + qty
            order["queue"] = q

            if contract in self.bids:
                bids = self.bids[contract]
                level_px = self._resolve_book_level(bids, px, contract)

                if level_px is not None:
                    bids[level_px].append(order)
                else:
                    bids[px] = [order]
            else:
                self.bids[contract] = {px: [order]}
        elif qty < 0:
            q = -qty + self._visible_level_volume(curr, "ask", px)
            q += self._resting_abs_qty_at_level(self.asks.get(contract, {}), px, contract)

            self.ask_qty[contract] = self.ask_qty.get(contract, 0) - qty
            order["queue"] = q

            if contract in self.asks:
                asks = self.asks[contract]
                level_px = self._resolve_book_level(asks, px, contract)

                if level_px is not None:
                    asks[level_px].append(order)
                else:
                    asks[px] = [order]
            else:
                self.asks[contract] = {px: [order]}

        self._record_msg(contract, 1)   # one exchange order message (post)
        return order

    def _build_fill_event(self, order: dict, fill_price: float, fill_qty: int, queue_before: int) -> dict:
        fill_event = {
            "px": fill_price,
            "qty": fill_qty,
            "order_id": order.get("order_id"),
            "created_at": order.get("created_at"),
            "created_tick": order.get("created_tick"),
            "age_ticks": order.get("count", 0),
            "aggressive": order.get("aggressive", False),
            "side": order.get("side"),
            "initial_qty": order.get("initial_qty"),
            "remaining_qty": order.get("remaining_qty", order.get("qty")),
            "queue_before": queue_before,
            "queue_after": max(queue_before - abs(fill_qty), 0),
        }
        for key in (
            "strategy_name",
            "reason_code",
            "reason_detail",
            "hedge_episode_id",
            "hedge_action",
            "effective_offset",
            "requote_count",
            "hedge_price_mode",
            "hedge_allow_escalation",
            "hedge_aggressive",
            "hedge_passive_join_top",
            "quote_action",
            "close_type_candidate",
            "close_urgency",
            "submit_aggressive",
            "submit_time",
            "submit_mid_spread",
            "submit_fair_spread",
            "submit_mid_dev",
            "submit_distance_ticks",
            "submit_in_active_zone",
            "submit_is_far_order",
            "submit_expected_hedge_px",
            "submit_expected_locked_spread",
            "submit_expected_edge_ticks",
            "expected_close_pnl_ticks",
            "primary_holding_ms",
            "hedge_trigger_time",
            "hedge_pricing_md_time",
            "hedge_pricing_bid0",
            "hedge_pricing_ask0",
            "post_fill_route",
            "post_fill_trigger_id",
            "post_fill_route_reason",
            "post_fill_route_applied",
            "post_fill_leg1_favorable_ticks",
            "post_fill_leg1_adverse_ticks",
            "post_fill_leg2_adverse_ticks",
            "base_close_candidate",
            "prediction_close_candidate",
            "prediction_base_allow_close",
            "prediction_changed_allow_close",
            "prediction_additional_roundtrip_candidate",
            "prediction_close_trigger_id",
            "prediction_close_urgency",
            "prediction_close_mode",
            "prediction_hedge_extra_offset_ticks",
            "prediction_hedge_reason",
            "prediction_close_override",
            "prediction_close_reason",
            "hedge_route_mode",
            "hedge_route_decision",
            "hedge_route_reason",
            "fallback_hedge_mode",
            "fallback_hedge_aggressive",
        ):
            if key in order:
                fill_event[key] = order.get(key)
        return fill_event

    def fak(self, contract: str, px: float, qty: int, sweep: bool = False):
        px = self._snap_price(px, contract)

        if self.verbose:
            print(f" - Place <{contract}> Fill-and-kill order: {qty} @ {px}{' [sweep]' if sweep else ''}")

        curr = self.curr[contract]

        # Depth-sweeping FAK: walk the book from the touch up to the limit px, taking liquidity at
        # each level within reach. Opt-in (default off) so touch-priced callers (taker/ladder) are
        # byte-identical to before. Used by the pairs hedge cross to complete past a thin level-0.
        if sweep and qty != 0:
            return self._fak_sweep(curr, contract, px, qty)

        # Count a FAK as one exchange message only when it is a real marketable
        # order (a price-mismatch reject below is not a sent order).
        if qty > 0 and self._price_ge(px, curr["askpx0"], contract):
            self._record_msg(contract, 1)
            avail = round(curr["askvol0"] * FAK_AVAIL)
            return {"px": px, "qty": min(qty, avail)}
        elif qty < 0 and self._price_le(px, curr["bidpx0"], contract):
            self._record_msg(contract, 1)
            avail = round(curr["bidvol0"] * FAK_AVAIL)
            return {"px": px, "qty": max(qty, -avail)}
        else:
            print("\n - FAK order not filled due to price mismatch.")
            print(f" - Current Ask: {curr['askpx0']} | Current Bid: {curr['bidpx0']}")
            print(f" - Order Price: {px} | Order Qty: {qty}")

            return None

    def _fak_sweep(self, curr: dict, contract: str, px: float, qty: int):
        """Depth-sweeping FAK: consume book levels from the touch outward, taking
        ``round(vol_i * FAK_AVAIL)`` at each level whose price is within the limit ``px``,
        until ``qty`` is met or no reachable level remains. Returns ONE aggregated fill at the
        VWAP of the consumed levels (one exchange message), or None on a price mismatch / dry book.
        Backward-compatible by construction: a touch-priced sweep reaches only level 0."""
        side_buy = qty > 0
        pre = "ask" if side_buy else "bid"
        touch = curr[f"{pre}px0"]
        marketable = self._price_ge(px, touch, contract) if side_buy else self._price_le(px, touch, contract)
        if not marketable:
            print("\n - FAK order not filled due to price mismatch.")
            print(f" - Current Ask: {curr['askpx0']} | Current Bid: {curr['bidpx0']}")
            print(f" - Order Price: {px} | Order Qty: {qty}")
            return None

        self._record_msg(contract, 1)                       # one order, regardless of levels swept
        need, filled, notional, i = abs(qty), 0, 0.0, 0
        while f"{pre}px{i}" in curr:
            lpx, lvol = curr[f"{pre}px{i}"], curr[f"{pre}vol{i}"]
            i += 1
            if lpx is None or lpx != lpx or lpx <= 0:       # absent/NaN level price -> no valid depth beyond
                break
            within = self._price_ge(px, lpx, contract) if side_buy else self._price_le(px, lpx, contract)
            if not within:                                  # levels are monotone away from the touch
                break
            if lvol and lvol > 0:
                take = min(need - filled, round(lvol * FAK_AVAIL))
                if take > 0:
                    filled += take
                    notional += take * lpx
                    if filled >= need:
                        break
        if filled == 0:
            return None                                     # nothing takeable within the limit
        return {"px": notional / filled, "qty": filled if side_buy else -filled}

    def cancel_bids(self, contract: str, px: float, min_count=0, predicate=None):
        px = self._snap_price(px, contract)

        if self.verbose:
            print(f" - Canceled <{contract}> bids at {px}")

        if contract in self.bids:
            bids = self.bids[contract]
            level_px = self._resolve_book_level(bids, px, contract)

            if level_px is not None:
                qty = 0
                removed_count = 0
                removed_ahead = 0
                kept = []
                for order in bids[level_px]:
                    selected = order["count"] >= min_count and (predicate is None or predicate(order))
                    if selected:
                        qty += order["qty"]
                        removed_count += 1
                        removed_ahead += abs(order["qty"])
                    else:
                        # A cancellation removes only the cancelled order's
                        # remaining quantity from later FIFO positions.
                        order["queue"] = max(0, order["queue"] - removed_ahead)
                        kept.append(order)

                if kept:
                    bids[level_px] = kept
                else:
                    del bids[level_px]

                if qty != 0:
                    self.bid_qty[contract] = max(0, self.bid_qty.get(contract, 0) - qty)

                self._record_msg(contract, removed_count)  # one message per cancelled order
                return qty
            else:
                return 0
        else:
            return 0

    def cancel_all_bids(self, contract: str):
        if self.verbose:
            print(f" - Canceled all bids of <{contract}>")

        if contract in self.bids:
            num = len(self.bids[contract])
            self._record_msg(contract, sum(len(orders) for orders in self.bids[contract].values()))
            self.bids[contract] = {}
            self.bid_qty[contract] = 0

            return num
        else:
            return 0

    def cancel_asks(self, contract: str, px: float, min_count=0, predicate=None):
        px = self._snap_price(px, contract)

        if self.verbose:
            print(f" - Canceled <{contract}> asks at {px}")

        if contract in self.asks:
            asks = self.asks[contract]
            level_px = self._resolve_book_level(asks, px, contract)

            if level_px is not None:
                qty = 0
                removed_count = 0
                removed_ahead = 0
                kept = []
                for order in asks[level_px]:
                    selected = order["count"] >= min_count and (predicate is None or predicate(order))
                    if selected:
                        qty += order["qty"]
                        removed_count += 1
                        removed_ahead += abs(order["qty"])
                    else:
                        order["queue"] = max(0, order["queue"] - removed_ahead)
                        kept.append(order)

                if kept:
                    asks[level_px] = kept
                else:
                    del asks[level_px]

                if qty != 0:
                    self.ask_qty[contract] = max(0, self.ask_qty.get(contract, 0) + qty)

                self._record_msg(contract, removed_count)  # one message per cancelled order
                return qty
            else:
                return 0
        else:
            return 0

    def cancel_all_asks(self, contract: str):
        if self.verbose:
            print(f" - Canceled all asks of <{contract}>")

        if contract in self.asks:
            num = len(self.asks[contract])
            self._record_msg(contract, sum(len(orders) for orders in self.asks[contract].values()))
            self.asks[contract] = {}
            self.ask_qty[contract] = 0

            return num
        else:
            return 0

    def _sweep_current_depth(self, contract: str, px: float, qty: int, participation=FAK_AVAIL):
        """Consume executable opposite-side depth through a limit price.

        The caller owns the order lifecycle; this helper only represents the
        current supplied book.  A later aligned snapshot replaces that book.
        """
        curr = self.curr[contract]
        side = "ask" if qty > 0 else "bid"
        touch = curr.get(f"{side}px0")
        marketable = self._price_ge(px, touch, contract) if qty > 0 else self._price_le(px, touch, contract)
        if not marketable:
            return False, 0, None, []

        try:
            participation = max(0.0, float(participation))
        except (TypeError, ValueError):
            participation = FAK_AVAIL

        remaining, filled, notional, levels, i = abs(qty), 0, 0.0, [], 0
        while remaining and f"{side}px{i}" in curr:
            level_px = curr.get(f"{side}px{i}")
            level_vol = curr.get(f"{side}vol{i}")
            if level_px is None or pd.isna(level_px):
                break
            within_limit = self._price_ge(px, level_px, contract) if qty > 0 else self._price_le(px, level_px, contract)
            if not within_limit:
                break
            if level_vol is not None and pd.notna(level_vol):
                available = max(0, round(float(level_vol) * participation))
                take = min(remaining, available)
                if take:
                    signed_take = take if qty > 0 else -take
                    levels.append({"px": level_px, "qty": signed_take})
                    filled += take
                    remaining -= take
                    notional += take * level_px
                    curr[f"{side}vol{i}"] = max(0, level_vol - take)
            i += 1

        signed_filled = filled if qty > 0 else -filled
        return True, signed_filled, (notional / filled if filled else None), levels

    def _remove_book_order(self, contract: str, side: str, px: float, order: dict):
        book = self.bids if side == "bid" else self.asks
        levels = book.get(contract, {})
        level_px = self._resolve_book_level(levels, px, contract)
        if level_px is None:
            return
        levels[level_px].remove(order)
        if not levels[level_px]:
            del levels[level_px]

    def _execute_depth_order(self, contract: str, side: str, px: float, order: dict, execution_mode: str):
        """Fill one existing order from current depth, preserving any residual."""
        original_qty = order["qty"]
        queue_before = order["queue"]
        participation = order.get("participation", FAK_AVAIL)
        _marketable, fill_qty, fill_px, levels = self._sweep_current_depth(
            contract, px, original_qty, participation
        )

        event = None
        if fill_qty:
            order["qty"] -= fill_qty
            order["remaining_qty"] = order["qty"]
            if original_qty > 0:
                self.bid_qty[contract] = max(0, self.bid_qty.get(contract, 0) - fill_qty)
            else:
                self.ask_qty[contract] = max(0, self.ask_qty.get(contract, 0) - abs(fill_qty))
            event = self._build_fill_event(order, fill_px, fill_qty, queue_before)
            event.update(
                execution_mode=execution_mode,
                execution_levels=levels,
                limit_px=px,
                participation=participation,
            )

        if order["qty"] == 0:
            self._remove_book_order(contract, side, px, order)
        else:
            if execution_mode == "aggressive_sweep":
                order["aggressive"] = False
            self._rebase_level_queues(contract, side, px)
        return event

    def _match_order_id(self, contract: str, order_id: int):
        """Immediately execute one aggressive order, without touching any peer."""
        for side, book in (("bid", self.bids), ("ask", self.asks)):
            for px, orders in list(book.get(contract, {}).items()):
                for order in list(orders):
                    if order.get("order_id") == order_id:
                        if not order.get("aggressive", False):
                            return []
                        event = self._execute_depth_order(contract, side, px, order, "aggressive_sweep")
                        return [event] if event is not None else []
        return []

    def match(self, contract: str, order_id: int | None = None):
        """Match resting orders, or immediately sweep one aggressive order ID.

        ``order_id`` is deliberately narrow: it is the strategy-driven path
        for an immediate hedge and never processes unrelated resting orders.
        """
        if order_id is not None:
            return self._match_order_id(contract, order_id)

        bids = self.bids.get(contract, {})
        asks = self.asks.get(contract, {})
        curr = self.curr[contract]
        best_ask = self._snap_price(curr["askpx0"], contract)
        best_bid = self._snap_price(curr["bidpx0"], contract)
        filled = []

        if self.verbose:
            print(f"\nMatching orders for <{contract}>:")
            print("Bids:", bids)
            print("Asks:", asks)

        for side, book, best_opposite in (("bid", bids, best_ask), ("ask", asks, best_bid)):
            for px, orders in list(book.items()):
                for order in list(orders):
                    if order.get("aggressive", False) or order["created_tick"] >= self.itr:
                        continue
                    qty, queue_before = order["qty"], order["queue"]
                    marketable = self._price_ge(px, best_opposite, contract) if qty > 0 else self._price_le(px, best_opposite, contract)
                    if marketable:
                        event = self._execute_depth_order(contract, side, px, order, "crossing_limit")
                        if event is not None:
                            filled.append(event)
                        continue

                    if qty > 0 and queue_before < qty:
                        fill_qty = max(0, min(qty, qty - queue_before))
                    elif qty < 0 and queue_before < -qty:
                        fill_qty = -max(0, min(-qty, -qty - queue_before))
                    else:
                        fill_qty = 0
                    if not fill_qty:
                        continue

                    order["qty"] -= fill_qty
                    order["remaining_qty"] = order["qty"]
                    if fill_qty > 0:
                        self.bid_qty[contract] = max(0, self.bid_qty.get(contract, 0) - fill_qty)
                    else:
                        self.ask_qty[contract] = max(0, self.ask_qty.get(contract, 0) - abs(fill_qty))
                    event = self._build_fill_event(order, px, fill_qty, queue_before)
                    event["execution_mode"] = "passive_queue"
                    filled.append(event)
                    if order["qty"] == 0:
                        self._remove_book_order(contract, side, px, order)

        return filled


class PairMarket(Market):
    """Legacy dual-contract compatibility replay advanced by EXCHANGE datetime.

    This class is intentionally not the Phase-2 supported causal ingress path;
    use ``common.ingress.CausalIngress`` for receive-time replay and decision
    reconstruction. The legacy two-leg implementation is retained unchanged
    for historic callers: the two legs are kept SIDE-BY-SIDE and advanced by EXCHANGE datetime, instead of
    being unioned into one receive-time-sorted stream. `step_pair()` does a two-pointer (k-way) merge by the
    `datetime` each record carries (= exchtime); at each exchtime it applies EVERY leg that ticks there via the
    inherited per-contract `_apply_tick`, so both legs' books/snapshots are coherent at that exchtime BEFORE the
    coordinator acts.  A missing leg is materialized from its last supplied row
    with zeroed trade flow, so its unchanged quote state remains executable.
    Books/matching/fak are otherwise inherited unchanged (already per-contract).
    The single-contract Market path is untouched."""

    @staticmethod
    def _prepare_pair_frame(frame: pd.DataFrame, leg_name: str) -> pd.DataFrame:
        """Return one leg's records in deterministic exchange-time order.

        ``step_pair`` is a two-head merge, so it must never receive a frame
        whose next row is later than a row still behind it.  Raw loaders normally
        deduplicate exchange times already, but this boundary is shared by
        diagnostics and synthetic callers too.  Sort by exchange timestamp,
        then receive timestamp, and retain the final receive-time observation at
        a duplicated exchange timestamp.  A missing receive timestamp falls
        back to the stable source-row order.

        This only normalizes supplied rows.  ``step_pair`` materializes a
        zero-trade forward fill when the other leg advances alone.
        """
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(f"{leg_name} pair frame must be a pandas DataFrame")

        out = frame.copy()
        try:
            exchange_time = pd.DatetimeIndex(pd.to_datetime(out.index, errors="raise"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{leg_name} pair frame index must contain valid exchange datetimes") from exc
        if exchange_time.isna().any():
            raise ValueError(f"{leg_name} pair frame index must not contain NaT exchange datetimes")

        # Keep a stable final key for rows whose receive timestamps are equal or
        # unavailable.  ``mergesort`` preserves source order for equal keys, so
        # ``keep='last'`` below deterministically chooses the final source row.
        out["_pair_exchtime"] = exchange_time
        if "timestamp" in out:
            receive_time = pd.to_datetime(out["timestamp"], errors="coerce")
            out["_pair_recvtime"] = receive_time.where(receive_time.notna(), exchange_time)
        else:
            out["_pair_recvtime"] = exchange_time
        out["_pair_source_order"] = range(len(out))

        out.sort_values(
            ["_pair_exchtime", "_pair_recvtime", "_pair_source_order"],
            kind="mergesort",
            inplace=True,
        )
        out = out.loc[~out["_pair_exchtime"].duplicated(keep="last")].copy()
        out.set_index("_pair_exchtime", inplace=True)
        out.index.name = "exchtime"
        out.drop(columns=["_pair_recvtime", "_pair_source_order"], inplace=True)

        if not out.index.is_monotonic_increasing or out.index.has_duplicates:
            raise ValueError(f"{leg_name} pair frame could not be normalized to unique increasing exchange times")
        return out

    def load_pair(self, frame_p, frame_s):
        """Load the two per-contract, exchtime-indexed frames (from common.backtest.load, each tagged with a
        'contract' column). Mirrors load_md's reset of the per-contract books (order_limit survives); the pair
        path uses step_pair, not the single-stream step()."""
        frame_p = self._prepare_pair_frame(frame_p, "P")
        frame_s = self._prepare_pair_frame(frame_s, "S")

        def _records(frame):
            recs = frame.to_dict("records")
            for rec, dt in zip(recs, frame.index.to_list()):
                rec["datetime"] = dt          # exchtime (the index), as load_md does
            return recs

        self._p_records = _records(frame_p)
        self._s_records = _records(frame_s)
        self._p_itr = 0
        self._s_itr = 0

        self.md = True                        # marker: data loaded (pair path uses step_pair, not step/md)
        self.md_records = None
        self.md_iter = None
        self.md_len = len(self._p_records) + len(self._s_records)
        self.itr = 0
        self.curr = {}
        self.bids = {}
        self.asks = {}
        self.bid_qty = {}
        self.ask_qty = {}
        self.next_order_id = 1

    @staticmethod
    def _forward_fill_pair_row(row, bundle_time):
        """Carry quote depth to ``bundle_time`` without replaying old trades."""
        forwarded = dict(row)
        forwarded["datetime"] = pd.Timestamp(bundle_time)
        price_fields = {"traded", "traded_p1", "traded_p2"}
        zero_fields = {
            "traded_v1", "traded_v2", "totalvol", "totalvalue", "volume", "turnover",
            "total_volume", "total_value", "trade_count", "tradecount", "num_trades",
            "totaltrades", "total_trade_count",
        }
        for key in forwarded:
            field = key.lower()
            if field in price_fields:
                forwarded[key] = float("nan")
            elif field in zero_fields or field.startswith("trade_count_"):
                forwarded[key] = 0
        return forwarded

    @staticmethod
    def _recv_ts(row):
        """Receive timestamp used to order a same-exchtime tie (nanosecond precision). Falls back to the
        exchtime (datetime) when the `timestamp` column is absent / NaT, so two simultaneous rows then keep a
        stable order."""
        ts = row.get("timestamp")
        return ts if ts is not None and ts == ts else row["datetime"]   # `ts == ts` is False for NaT

    def step_pair(self):
        """Advance to the next exchtime across the two legs and apply every leg that ticks at it.

        Returns an aligned bundle with source ``rows`` only for legs that
        actually updated. Both (or the one) legs at exchtime T are applied
        before return (books aged/decayed, curr updated). A same-
        exchtime TIE (both legs at T) is ordered by the finer RECEIVE timestamp (nanosecond) -- the order a
        live feed would deliver them in, so the coordinator reacts in that order; equal/missing timestamps
        fall back to a stable P-before-S. Returns None when both legs are exhausted. load() de-dups exchtime
        within a contract, so each leg contributes at most one row per exchtime."""
        ip, is_ = self._p_itr, self._s_itr
        dp = self._p_records[ip]["datetime"] if ip < len(self._p_records) else None
        ds = self._s_records[is_]["datetime"] if is_ < len(self._s_records) else None
        if dp is None and ds is None:
            return None

        if ds is None or (dp is not None and dp <= ds):
            t = dp
        else:
            t = ds

        # Collect the leg(s) AT this exchtime, then order a tie by the finer RECEIVE timestamp: both
        # snapshots are simultaneous at the exchange, so receive order is the order a live system would have
        # processed them. (Fills are per-contract / order-independent, and the basis + _manage run once after
        # both legs ingest; the order only matters for cross-leg side-effects of a fill reaction -- a hedge
        # cross or a cancel on the other leg.) The "p"/"s" tag breaks an exact-timestamp tie stably (P first).
        pending = []
        if dp is not None and dp == t:
            pending.append(("p", self._p_records[ip]))
        if ds is not None and ds == t:
            pending.append(("s", self._s_records[is_]))
        pending.sort(key=lambda pr: (self._recv_ts(pr[1]), pr[0]))

        updated, rows = [], {}
        for which, row in pending:
            if which == "p":
                self._p_itr += 1
            else:
                self._s_itr += 1
            self._apply_tick(row)
            # Count supplied raw records, rather than fabricated forward fills,
            # for order-age diagnostics.
            self.itr += 1
            updated.append(row["contract"])
            rows[row["contract"]] = row

        # A missing source row means an unchanged executable quote/book, not a
        # missing market.  Apply it at this bundle time so its orders age, while
        # cleared trade fields prevent the preceding interval's prints from
        # consuming the same FIFO queue twice.  The record lists hold the last
        # supplied input separately from mutable ``curr`` depth.
        updated_set = set(updated)
        for records, index in ((self._p_records, self._p_itr), (self._s_records, self._s_itr)):
            if index:
                source = records[index - 1]
                if source["contract"] not in updated_set:
                    self._apply_tick(self._forward_fill_pair_row(source, t))

        return {"datetime": t, "updated": updated, "rows": rows}
