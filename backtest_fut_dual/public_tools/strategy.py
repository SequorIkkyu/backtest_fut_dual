# Base strategy class for order management, fills, and PnL accounting.

import math
import os

import numpy as np

from public_tools.market import Market


class Strategy:
    def __init__(
        self,
        name: str,
        market: Market,
        mult: int,
        tick: float,
        fee: float,
        rebate: float,
        fee_lot: float = None,
        parallel: bool = True,
        verbose: bool = False,
        output_root: str | None = None,
        record_diagnostics: bool = False,
        underlying: str | None = None,
    ):
        self.name = "<Strategy - " + name + ">"
        self.market = market
        self.mult = mult
        self.tick = tick
        self.fee = fee
        self.rebate = rebate
        self.fee_lot = fee_lot  # fixed $ per lot per side; overrides rate-based fee when set
        # self.stop_thresh = stop_thresh
        self.parallel = parallel
        self.verbose = verbose
        self.output_root = os.path.abspath(output_root) if output_root is not None else None
        self.record_diagnostics = record_diagnostics
        self.underlying = underlying
        self.price_decimals = max(0, int(np.ceil(-np.log10(tick))))

        self.auto_unwind = False
        self.hedger = None
        self.curr_md = None
        self.trading_date = None
        self.fair_mid = None
        self.fair_ask = None
        self.fair_bid = None
        self.fair_value = None
        self.fair_upper = None
        self.fair_lower = None

    def snap_price(self, px: float):
        if self.market is not None:
            if hasattr(self.market, "snap_price"):
                return self.market.snap_price(px)
            if hasattr(self.market, "_snap_price"):
                return self.market._snap_price(px)

        return px

    def _resolve_book_level(self, book_side: dict[float, list[dict]], px: float) -> float | None:
        if self.market is not None and hasattr(self.market, "_resolve_book_level"):
            return self.market._resolve_book_level(book_side, px)

        normalized_px = self.snap_price(px)
        price_tol = getattr(self.market, "price_tol", self.tick / 10)
        if normalized_px in book_side:
            return normalized_px
        for level_px in book_side:
            if abs(level_px - normalized_px) <= price_tol:
                return level_px
        return None

    def _has_resting_level(self, book_side: dict[float, list[dict]], px: float) -> bool:
        return self._resolve_book_level(book_side, px) is not None

    def reset(self, contract: str, trading_date: str | None = None):
        self.contract = contract
        self.trading_date = str(trading_date) if trading_date is not None else None
        self.pos = 0
        self.cost = None
        self.gross_pnl = 0
        self.pnl = 0
        self.total_fees = 0
        self.num_orders = 0
        self.num_fills = 0
        self.filled_qty = 0
        self.num_cancels = 0
        self.submitted_order_count = 0
        self.cancel_message_count = 0
        self.opened_qty = 0
        self.filled_order_ids = set()
        self.curr_md = None
        self.session_step_count = 0
        self.session_total_steps = None

    def get_pnl_metrics(self) -> dict[str, float]:
        gross_pnl = float(self.gross_pnl)
        pnl_after_fees = float(self.pnl)
        fee_drag = float(self.total_fees)
        rebate_pnl = fee_drag * float(self.rebate)
        net_pnl = pnl_after_fees + rebate_pnl
        fee_drag_ratio = fee_drag / gross_pnl if gross_pnl > 0 else np.nan
        return {
            "gross_pnl": gross_pnl,
            "pnl_after_fees": pnl_after_fees,
            "fee_drag": fee_drag,
            "fee_drag_ratio": fee_drag_ratio,
            "rebate_pnl": rebate_pnl,
            "net_pnl": net_pnl,
        }

    def record_event(self, *args, **kwargs) -> None:
        return None

    def submit_order(
        self,
        px: float,
        qty: int,
        *,
        aggressive: bool = False,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        extra_metadata: dict | None = None,
        event_time_override=None,
    ) -> dict | None:
        metadata = {"strategy_name": self.name}
        if reason_code is not None:
            metadata["reason_code"] = reason_code
        if reason_detail is not None:
            metadata["reason_detail"] = reason_detail
        if extra_metadata:
            metadata.update(extra_metadata)

        normalized_px = self.snap_price(px)

        order = self.market.place_order(
            self.contract,
            normalized_px,
            qty,
            aggressive=aggressive,
            metadata=metadata,
            event_time=event_time_override,
        )
        self.num_orders += 1
        self.submitted_order_count += 1
        self.record_event(
            "order",
            normalized_px,
            qty,
            order=order,
            reason_code=reason_code,
            reason_detail=reason_detail,
            extra=extra_metadata,
            event_time_override=event_time_override,
        )
        return order

    def set_fair(self, fair_mid: float, fair_ask: float, fair_bid: float):
        self.fair_mid = fair_mid
        self.fair_ask = fair_ask
        self.fair_bid = fair_bid
        self.fair_value = fair_mid
        self.fair_upper = fair_ask
        self.fair_lower = fair_bid

        if self.verbose:
            print(
                f" - Fair mid set to {self.fair_mid}, ask: {self.fair_ask}, bid: {self.fair_bid}"
                f" for strategy {self.name} on contract {self.contract}"
            )

    def set_hedger(self, hedger: "Strategy"):
        self.hedger = hedger

    def step(self, md):
        if md["contract"] != self.contract:
            return

        self.curr_md = md
        self.session_step_count += 1

        filled = self.market.match(self.contract)
        if len(filled) > 0:
            self.num_fills += len(filled)
            self.filled_qty += sum(abs(order["qty"]) for order in filled)
            self.update_pos(filled)

            if self.auto_unwind:
                for order in filled:
                    self.unwind(order, 1)

        self.quote(md)

    def update_pos(self, filled):
        gross_pnl = 0
        pnl_after_fees = 0
        fees = 0
        opened_qty = 0

        for order in filled:
            px = order["px"]
            qty = order["qty"]
            order_id = order.get("order_id")

            if order_id is not None:
                self.filled_order_ids.add(order_id)

            self.record_event("fill", px, qty, order=order)

            if qty > 0:
                if self.pos > 0:
                    opened_qty += qty
                    value = self.pos * self.cost + px * qty
                    self.pos += qty
                    self.cost = value / self.pos
                elif self.pos == 0:
                    opened_qty += qty
                    self.pos = qty
                    self.cost = px
                elif self.pos < 0:
                    close = min(-self.pos, qty)
                    self.pos += close
                    gross_close = (self.cost - px) * close * self.mult
                    if self.fee_lot is not None:
                        fees += close * self.fee_lot
                        pnl_after_fees += gross_close - close * self.fee_lot
                    else:
                        fees += close * px * self.mult * self.fee
                        pnl_after_fees += gross_close - close * px * self.mult * self.fee
                    gross_pnl += gross_close
                    qty -= close

                    if qty > 0:
                        opened_qty += qty
                        self.pos = qty
                        self.cost = px
            elif qty < 0:
                if self.pos < 0:
                    opened_qty += -qty
                    value = self.pos * self.cost + px * qty
                    self.pos += qty
                    self.cost = value / self.pos
                elif self.pos == 0:
                    opened_qty += -qty
                    self.pos = qty
                    self.cost = px
                elif self.pos > 0:
                    close = min(self.pos, -qty)
                    self.pos -= close
                    gross_close = (px - self.cost) * close * self.mult
                    if self.fee_lot is not None:
                        fees += close * self.fee_lot
                        pnl_after_fees += gross_close - close * self.fee_lot
                    else:
                        fees += close * px * self.mult * self.fee
                        pnl_after_fees += gross_close - close * px * self.mult * self.fee
                    gross_pnl += gross_close
                    qty += close

                    if qty < 0:
                        opened_qty += -qty
                        self.pos = qty
                        self.cost = px

        self.gross_pnl += gross_pnl
        self.pnl += pnl_after_fees
        self.total_fees += fees
        self.opened_qty += opened_qty

        return pnl_after_fees

    def quote(self, md):
        pass

    def bid_and_cancel(
        self, px_list, min_count=0, cancel_reason_code: str | None = None, order_reason_code: str | None = None
    ):
        # Pre-compute rounded prices for fast lookup
        px_list_rounded_set = {self.snap_price(px) for px in px_list}
        existing_bids = self.market.bids.get(self.contract, {})

        # Find prices to cancel using set lookup (O(1) instead of O(n))
        to_cancel = [px for px in existing_bids.keys() if self.snap_price(px) not in px_list_rounded_set]

        # # Debug: log what we're trying to cancel
        # if self.verbose or (len(existing_bids) > 0 and len(to_cancel) == 0 and len(px_list) > 0):
        #     for px, orders in existing_bids.items():
        #         rounded_px = round(px, self.price_decimals)
        #         for order in orders:
        #             print(f"  [DEBUG] Existing bid @ {px} (rounded: {rounded_px}): qty={order['qty']}, count={order['count']}, min_count={min_count}, in_px_list={rounded_px in px_list_rounded_set}")

        total = 0
        for px in to_cancel:
            for order in existing_bids.get(px, []):
                self.log_cancel(px, order["qty"], order=order, reason_code=cancel_reason_code)

            cancelled_qty = self.cancel_bid_level(px, min_count=min_count)
            total += cancelled_qty

        level_qty = math.ceil(total / len(px_list)) if len(px_list) > 0 else 0
        for px in px_list:
            if total > 0:
                order_qty = min(level_qty, total)
                total -= order_qty
                if order_qty > 0:
                    if self.verbose:
                        print(f" - Placing bid @ {px} for strategy {self.name}")

                    # Round price to tick size
                    rounded_px = self.snap_price(px)
                    self.submit_order(rounded_px, order_qty, reason_code=order_reason_code)

    def ask_and_cancel(
        self, px_list, min_count=0, cancel_reason_code: str | None = None, order_reason_code: str | None = None
    ):
        # Pre-compute rounded prices for fast lookup
        px_list_rounded_set = {self.snap_price(px) for px in px_list}
        existing_asks = self.market.asks.get(self.contract, {})

        # Find prices to cancel using set lookup (O(1) instead of O(n))
        to_cancel = [px for px in existing_asks.keys() if self.snap_price(px) not in px_list_rounded_set]

        # # Debug: log what we're trying to cancel
        # if self.verbose or (len(existing_asks) > 0 and len(to_cancel) == 0 and len(px_list) > 0):
        #     for px, orders in existing_asks.items():
        #         rounded_px = round(px, self.price_decimals)
        #         for order in orders:
        #             print(f"  [DEBUG] Existing ask @ {px} (rounded: {rounded_px}): qty={order['qty']}, count={order['count']}, min_count={min_count}, in_px_list={rounded_px in px_list_rounded_set}")

        total = 0
        for px in to_cancel:
            for order in existing_asks.get(px, []):
                self.log_cancel(px, order["qty"], order=order, reason_code=cancel_reason_code)

            cancelled_qty = self.cancel_ask_level(px, min_count=min_count)
            total += cancelled_qty

        level_qty = math.floor(total / len(px_list)) if len(px_list) > 0 else 0
        for px in px_list:
            if total < 0:
                order_qty = max(level_qty, total)
                total -= order_qty
                if order_qty < 0:
                    if self.verbose:
                        print(f" - Placing ask @ {px} for strategy {self.name}")

                    # Round price to tick size
                    rounded_px = self.snap_price(px)
                    self.submit_order(rounded_px, order_qty, reason_code=order_reason_code)

    def unwind(self, trade, offset=1):
        px = trade["px"]
        qty = trade["qty"]

        if qty < 0:  # and self.pos < 0:
            target = self.snap_price(px - offset * self.tick)

            if not self._has_resting_level(self.market.bids.get(self.contract, {}), target):
                if self.verbose:
                    print(f" - Placing bid to unwind @ {target}")

                self.submit_order(target, -qty, reason_code="auto_unwind")
        elif qty > 0:  # and self.pos > 0:
            target = self.snap_price(px + offset * self.tick)

            if not self._has_resting_level(self.market.asks.get(self.contract, {}), target):
                if self.verbose:
                    print(f" - Placing ask to unwind @ {target}")

                self.submit_order(target, -qty, reason_code="auto_unwind")

    def stop_loss(self):
        return False

    def log_cancel(
        self,
        px: float,
        qty: int,
        *,
        order: dict | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
    ):
        """Log a cancel event to event_log (non-parallel mode only)."""
        self.record_event("cancel", px, qty, order=order, reason_code=reason_code, reason_detail=reason_detail)

    def log_cancel_book_side(self, book_side: dict, reason_code: str | None = None, reason_detail: str | None = None):
        """Log all resting orders in a book side (bids or asks) as cancel events."""
        for px, orders in book_side.items():
            for order in orders:
                self.log_cancel(px, order["qty"], order=order, reason_code=reason_code, reason_detail=reason_detail)

    def _count_cancellable_orders(self, book_side: dict, px: float, min_count: int = 0) -> int:
        level_px = self._resolve_book_level(book_side, px)
        if level_px is None:
            return 0
        return sum(1 for order in book_side[level_px] if order["count"] >= min_count)

    def _count_resting_orders(self, book_side: dict) -> int:
        return sum(len(orders) for orders in book_side.values())

    def cancel_bid_level(self, px: float, min_count: int = 0, *, include_cancel_stat: bool = True) -> int:
        bid_book = self.market.bids.get(self.contract, {})
        level_px = self._resolve_book_level(bid_book, px)
        if level_px is None:
            return 0
        cancelled_order_count = self._count_cancellable_orders(bid_book, level_px, min_count=min_count)
        cancelled_qty = self.market.cancel_bids(self.contract, level_px, min_count)
        if cancelled_qty != 0:
            if include_cancel_stat:
                self.num_cancels += 1
            self.cancel_message_count += cancelled_order_count
        return cancelled_qty

    def cancel_ask_level(self, px: float, min_count: int = 0, *, include_cancel_stat: bool = True) -> int:
        ask_book = self.market.asks.get(self.contract, {})
        level_px = self._resolve_book_level(ask_book, px)
        if level_px is None:
            return 0
        cancelled_order_count = self._count_cancellable_orders(ask_book, level_px, min_count=min_count)
        cancelled_qty = self.market.cancel_asks(self.contract, level_px, min_count)
        if cancelled_qty != 0:
            if include_cancel_stat:
                self.num_cancels += 1
            self.cancel_message_count += cancelled_order_count
        return cancelled_qty

    def cancel_all_resting_bids(self, *, include_cancel_stat: bool = True) -> int:
        bid_book = self.market.bids.get(self.contract, {})
        resting_order_count = self._count_resting_orders(bid_book)
        cancelled_levels = self.market.cancel_all_bids(self.contract)
        if cancelled_levels:
            if include_cancel_stat:
                self.num_cancels += cancelled_levels
            self.cancel_message_count += resting_order_count
        return cancelled_levels

    def cancel_all_resting_asks(self, *, include_cancel_stat: bool = True) -> int:
        ask_book = self.market.asks.get(self.contract, {})
        resting_order_count = self._count_resting_orders(ask_book)
        cancelled_levels = self.market.cancel_all_asks(self.contract)
        if cancelled_levels:
            if include_cancel_stat:
                self.num_cancels += cancelled_levels
            self.cancel_message_count += resting_order_count
        return cancelled_levels

    def stop(self):
        bid_book = self.market.bids.get(self.contract, {})
        ask_book = self.market.asks.get(self.contract, {})
        self.log_cancel_book_side(bid_book, reason_code="session_flatten")
        self.log_cancel_book_side(ask_book, reason_code="session_flatten")
        self.cancel_all_resting_bids(include_cancel_stat=False)
        self.cancel_all_resting_asks(include_cancel_stat=False)

        filled = None
        if self.pos > 0:
            filled = {"px": self.curr_md["bidpx0"], "qty": -self.pos}
        elif self.pos < 0:
            filled = {"px": self.curr_md["askpx0"], "qty": -self.pos}

        if filled is not None:
            self.update_pos([filled])
            self.num_orders += 1
            self.num_fills += 1
            self.filled_qty += abs(filled["qty"])

    def hedge(
        self, size, offset, reason_code: str | None = None, event_metadata: dict | None = None, event_time_override=None
    ):
        if self.curr_md is None:
            return

        if size > 0:
            px = self.curr_md["askpx0"] + offset * self.tick
            self.submit_order(
                px,
                size,
                aggressive=True,
                reason_code=reason_code or "hedge_submit",
                extra_metadata=event_metadata,
                event_time_override=event_time_override,
            )
        elif size < 0:
            px = self.curr_md["bidpx0"] - offset * self.tick
            self.submit_order(
                px,
                size,
                aggressive=True,
                reason_code=reason_code or "hedge_submit",
                extra_metadata=event_metadata,
                event_time_override=event_time_override,
            )

    def summarize(self):
        pnl_metrics = self.get_pnl_metrics()
        return {
            "gross_pnl": pnl_metrics["gross_pnl"],
            "pnl": self.pnl,
            "fees": self.total_fees,
            "fee_drag": pnl_metrics["fee_drag"],
            "fee_drag_ratio": pnl_metrics["fee_drag_ratio"],
            "rebate_pnl": pnl_metrics["rebate_pnl"],
            "net_pnl": pnl_metrics["net_pnl"],
            "num_orders": self.num_orders,
            "num_fills": self.num_fills,
            "filled_qty": self.filled_qty,
            "num_cancels": self.num_cancels,
            "submitted_order_count": self.submitted_order_count,
            "cancel_message_count": self.cancel_message_count,
            "traded_order_count": len(self.filled_order_ids),
            "opened_qty": self.opened_qty,
        }

    def plot_record(self):
        if self.verbose:
            print(f"No public strategy plot implementation for {getattr(self, 'contract', None)}.")

    def save_record(self):
        if self.verbose:
            print(f"No public strategy record implementation for {getattr(self, 'contract', None)}.")

    def get_extra(self):
        bid_book = self.market.bids.get(getattr(self, "contract", None), {}) if self.market is not None else {}
        ask_book = self.market.asks.get(getattr(self, "contract", None), {}) if self.market is not None else {}

        best_resting_bid_px = max(bid_book) if bid_book else np.nan
        best_resting_ask_px = min(ask_book) if ask_book else np.nan
        best_resting_bid_qty = sum(order["qty"] for order in bid_book.get(best_resting_bid_px, [])) if bid_book else 0
        best_resting_ask_qty = -sum(order["qty"] for order in ask_book.get(best_resting_ask_px, [])) if ask_book else 0

        return {
            "best_resting_bid_px": best_resting_bid_px,
            "best_resting_ask_px": best_resting_ask_px,
            "best_resting_bid_qty": best_resting_bid_qty,
            "best_resting_ask_qty": best_resting_ask_qty,
            "resting_bid_levels": len(bid_book),
            "resting_ask_levels": len(ask_book),
            "trading_date": getattr(self, "trading_date", None),
        }
