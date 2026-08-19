# Simple limit-order-book simulator used by public strategies.

import math
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

FAK_AVAIL = 0.5


class Market:
    def __init__(self, mult: int, tick: float, verbose: bool = False):
        self.mult = mult
        self.tick = tick
        self.price_tol = tick / 10
        self.verbose = verbose
        self._tick_decimal = Decimal(str(tick))

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

    def _snap_price(self, px: float):
        if pd.isna(px):
            return px

        snapped = float(
            (Decimal(str(px)) / self._tick_decimal).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * self._tick_decimal
        )

        if math.isclose(px, snapped, abs_tol=self.price_tol):
            return snapped

        return px

    def snap_price(self, px: float):
        return self._snap_price(px)

    def _price_eq(self, left: float, right: float):
        if pd.isna(left) or pd.isna(right):
            return False

        left = self._snap_price(left)
        right = self._snap_price(right)

        return math.isclose(left, right, abs_tol=self.price_tol)

    def _price_ge(self, left: float, right: float):
        if pd.isna(left) or pd.isna(right):
            return False

        left = self._snap_price(left)
        right = self._snap_price(right)

        return left > right or self._price_eq(left, right)

    def _price_gt(self, left: float, right: float):
        if pd.isna(left) or pd.isna(right):
            return False

        left = self._snap_price(left)
        right = self._snap_price(right)

        return left > right and not self._price_eq(left, right)

    def _price_le(self, left: float, right: float):
        return self._price_ge(right, left)

    def _price_lt(self, left: float, right: float):
        return self._price_gt(right, left)

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
            for record, dt in zip(records, md.index.to_list()):
                record["datetime"] = dt
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

    def _resolve_book_level(self, book: dict[float, list[dict]], px: float) -> float | None:
        normalized_px = self.snap_price(px)
        if normalized_px in book:
            return normalized_px
        for level_px in book:
            if self._price_eq(level_px, normalized_px):
                return level_px
        return None

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

                contract = curr["contract"]
                self.curr[contract] = curr

                # print(f"\n - Market step {self.itr} at {curr['datetime']} with {contract}:")
                # print(curr)

                if contract in self.bids:
                    bids = self.bids[contract]

                    for px, orders in bids.items():
                        for order in orders:
                            order["count"] += 1

                            if order["queue"] > 0:
                                if self._price_eq(px, curr["traded_p1"]):
                                    # Trading occurred at our exact bid price
                                    order["queue"] -= curr["traded_v1"]
                                elif self._price_eq(px, curr["traded_p2"]):
                                    # Trading occurred at our exact bid price (upper tick)
                                    order["queue"] -= curr["traded_v2"]
                                elif (pd.notna(curr.get("traded_p2")) and pd.notna(curr.get("traded_v1"))
                                      and pd.notna(curr.get("traded_v2"))
                                      and self._price_lt(curr["traded_p2"], px)):
                                    # All interval trading was strictly below our bid:
                                    # by price priority, sellers must have cleared our level first
                                    order["queue"] -= curr["traded_v1"] + curr["traded_v2"]

                            # if order['queue'] <= 0:
                            #     print(f"\n - Orders at {px} has been FULLY FILLED.")

                if contract in self.asks:
                    asks = self.asks[contract]

                    for px, orders in asks.items():
                        for order in orders:
                            order["count"] += 1

                            if order["queue"] > 0:
                                if self._price_eq(px, curr["traded_p1"]):
                                    # Trading occurred at our exact ask price (lower tick)
                                    order["queue"] -= curr["traded_v1"]
                                elif self._price_eq(px, curr["traded_p2"]):
                                    # Trading occurred at our exact ask price
                                    order["queue"] -= curr["traded_v2"]
                                elif (pd.notna(curr.get("traded_p1")) and pd.notna(curr.get("traded_v1"))
                                      and pd.notna(curr.get("traded_v2"))
                                      and self._price_gt(curr["traded_p1"], px)):
                                    # All interval trading was strictly above our ask:
                                    # by price priority, buyers must have cleared our level first
                                    order["queue"] -= curr["traded_v1"] + curr["traded_v2"]

                            # if order['queue'] <= 0:
                            #     print(f"\n - Orders at {px} has been FULLY FILLED.")

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
        px = self.snap_price(px)

        if self.verbose:
            print(f" - Placed <{contract}> {'Aggressive' if aggressive else 'Limit'} Order: {qty} @ {px}")

        curr = self.curr[contract]
        order_time = pd.Timestamp(event_time) if event_time is not None else curr.get("datetime")
        order = {
            "qty": qty,
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
            q = qty

            if self._price_eq(px, curr["bidpx0"]):
                q += curr["bidvol0"]
                curr["bidvol0"] += qty
            elif self._price_eq(px, curr["bidpx1"]):
                q += curr["bidvol1"]
                curr["bidvol1"] += qty
            elif self._price_eq(px, curr["bidpx2"]):
                q += curr["bidvol2"]
                curr["bidvol2"] += qty
            elif self._price_eq(px, curr["bidpx3"]):
                q += curr["bidvol3"]
                curr["bidvol3"] += qty
            elif self._price_eq(px, curr["bidpx4"]):
                q += curr["bidvol4"]
                curr["bidvol4"] += qty

            self.bid_qty[contract] = self.bid_qty.get(contract, 0) + qty
            order["queue"] = q

            if contract in self.bids:
                bids = self.bids[contract]
                level_px = self._resolve_book_level(bids, px)

                if level_px is not None:
                    bids[level_px].append(order)
                else:
                    bids[px] = [order]
            else:
                self.bids[contract] = {px: [order]}
        elif qty < 0:
            q = -qty

            if self._price_eq(px, curr["askpx0"]):
                q += curr["askvol0"]
                curr["askvol0"] -= qty
            elif self._price_eq(px, curr["askpx1"]):
                q += curr["askvol1"]
                curr["askvol1"] -= qty
            elif self._price_eq(px, curr["askpx2"]):
                q += curr["askvol2"]
                curr["askvol2"] -= qty
            elif self._price_eq(px, curr["askpx3"]):
                q += curr["askvol3"]
                curr["askvol3"] -= qty
            elif self._price_eq(px, curr["askpx4"]):
                q += curr["askvol4"]
                curr["askvol4"] -= qty

            self.ask_qty[contract] = self.ask_qty.get(contract, 0) - qty
            order["queue"] = q

            if contract in self.asks:
                asks = self.asks[contract]
                level_px = self._resolve_book_level(asks, px)

                if level_px is not None:
                    asks[level_px].append(order)
                else:
                    asks[px] = [order]
            else:
                self.asks[contract] = {px: [order]}

        return order

    def _build_fill_event(self, order: dict, fill_price: float, fill_qty: int, queue_before: int) -> dict:
        internal_order_fields = {"qty", "queue", "count"}
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
        for key, value in order.items():
            if key not in internal_order_fields and key not in fill_event:
                fill_event[key] = value
        return fill_event

    def fak(self, contract: str, px: float, qty: int):
        px = self._snap_price(px)

        if self.verbose:
            print(f" - Place <{contract}> Fill-and-kill order: {qty} @ {px}")

        curr = self.curr[contract]

        if qty > 0 and self._price_ge(px, curr["askpx0"]):
            avail = round(curr["askvol0"] * FAK_AVAIL)
            return {"px": px, "qty": min(qty, avail)}
        elif qty < 0 and self._price_le(px, curr["bidpx0"]):
            avail = round(curr["bidvol0"] * FAK_AVAIL)
            return {"px": px, "qty": max(qty, -avail)}
        else:
            print("\n - FAK order not filled due to price mismatch.")
            print(f" - Current Ask: {curr['askpx0']} | Current Bid: {curr['bidpx0']}")
            print(f" - Order Price: {px} | Order Qty: {qty}")

            return None

    def cancel_bids(self, contract: str, px: float, min_count=0):
        px = self._snap_price(px)

        if self.verbose:
            print(f" - Canceled <{contract}> bids at {px}")

        if contract in self.bids:
            bids = self.bids[contract]
            level_px = self._resolve_book_level(bids, px)

            if level_px is not None:
                to_remove = []
                qty = 0

                for order in bids[level_px]:
                    if order["count"] >= min_count:
                        qty += order["qty"]
                        to_remove.append(order)

                for order in to_remove:
                    bids[level_px].remove(order)

                if len(bids[level_px]) == 0:
                    del bids[level_px]

                if qty != 0:
                    self.bid_qty[contract] = max(0, self.bid_qty.get(contract, 0) - qty)

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
            self.bids[contract] = {}
            self.bid_qty[contract] = 0

            return num
        else:
            return 0

    def cancel_asks(self, contract: str, px: float, min_count=0):
        px = self._snap_price(px)

        if self.verbose:
            print(f" - Canceled <{contract}> asks at {px}")

        if contract in self.asks:
            asks = self.asks[contract]
            level_px = self._resolve_book_level(asks, px)

            if level_px is not None:
                to_remove = []
                qty = 0

                for order in asks[level_px]:
                    if order["count"] >= min_count:
                        qty += order["qty"]
                        to_remove.append(order)

                for order in to_remove:
                    asks[level_px].remove(order)

                if len(asks[level_px]) == 0:
                    del asks[level_px]

                if qty != 0:
                    self.ask_qty[contract] = max(0, self.ask_qty.get(contract, 0) + qty)

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
            self.asks[contract] = {}
            self.ask_qty[contract] = 0

            return num
        else:
            return 0

    def match(self, contract: str):
        bids = self.bids.get(contract, {})
        asks = self.asks.get(contract, {})
        curr = self.curr[contract]
        best_ask = self._snap_price(curr["askpx0"])
        best_bid = self._snap_price(curr["bidpx0"])
        traded_p1 = curr.get("traded_p1")
        traded_p2 = curr.get("traded_p2")
        traded_v1 = curr.get("traded_v1")
        traded_v2 = curr.get("traded_v2")
        _has_traded = pd.notna(traded_p1) and pd.notna(traded_p2) and pd.notna(traded_v1) and pd.notna(traded_v2)

        if self.verbose:
            print("\nMatching orders for <{contract}>:")
            print("Bids:", bids)
            print("Asks:", asks)
            # print(' - with')
            # print(self.curr)

        filled = []

        for px, orders in bids.items():
            to_remove = []

            for order in orders:
                qty = order["qty"]
                q = order["queue"]
                is_aggressive = order.get("aggressive", False)
                # count = order['count']

                # traded_cross: all interval volume was strictly below our bid, so by price
                # priority our level must have been cleared first. Only used for aggressive orders;
                # passive fills are handled entirely by queue decay in step().
                traded_cross = _has_traded and self._price_lt(traded_p2, px)

                if self._price_ge(px, best_ask) or (traded_cross and is_aggressive):
                    if is_aggressive:
                        # Market order: fill at the best available price.
                        # If the book still shows an ask ≤ px, use it (price improvement).
                        # If the market traded through us but has since bounced (best_ask > px),
                        # use traded_p2 — the upper bound of where crossing actually occurred.
                        if self._price_le(best_ask, px):
                            fill_price = best_ask
                        else:
                            fill_price = traded_p2 if (traded_cross and pd.notna(traded_p2)) else px
                    else:
                        fill_price = px
                    if self.verbose:
                        print(
                            f" - TRADED (cross): Bot {qty} @ {fill_price}"
                            + (f" (improved from {px})" if is_aggressive and fill_price < px else "")
                        )
                    order["remaining_qty"] = 0
                    filled.append(self._build_fill_event(order, fill_price, qty, q))
                    self.bid_qty[contract] = max(0, self.bid_qty.get(contract, 0) - qty)
                    to_remove.append(order)

                    if is_aggressive:
                        order["aggressive"] = False
                elif q < qty:
                    fq = max(0, min(qty, qty - q))

                    if self.verbose:
                        print(f" - TRADED: Bot {fq} @ {px}")

                    filled.append(self._build_fill_event(order, px, fq, q))
                    self.bid_qty[contract] = max(0, self.bid_qty.get(contract, 0) - fq)
                    order["qty"] -= fq
                    order["remaining_qty"] = order["qty"]

                    if order["qty"] <= 0:
                        to_remove.append(order)

            for order in to_remove:
                bids[px].remove(order)

        for px in list(bids.keys()):
            if len(bids[px]) == 0:
                del bids[px]

        for px, orders in asks.items():
            to_remove = []

            for order in orders:
                qty = order["qty"]
                q = order["queue"]
                is_aggressive = order.get("aggressive", False)
                # count = order['count']

                # traded_cross: all interval volume was strictly above our ask, so by price
                # priority our level must have been cleared first. Only used for aggressive orders;
                # passive fills are handled entirely by queue decay in step().
                traded_cross = _has_traded and self._price_gt(traded_p1, px)

                if self._price_le(px, best_bid) or (traded_cross and is_aggressive):
                    if is_aggressive:
                        # Market order: fill at the best available price.
                        # If the book still shows a bid ≥ px, use it (price improvement).
                        # If the market traded through us but has since bounced (best_bid < px),
                        # use traded_p1 — the lower bound of where crossing actually occurred.
                        if self._price_ge(best_bid, px):
                            fill_price = best_bid
                        else:
                            fill_price = traded_p1 if (traded_cross and pd.notna(traded_p1)) else px
                    else:
                        fill_price = px
                    if self.verbose:
                        print(
                            f" - TRADED (cross): Sold {-qty} @ {fill_price}"
                            + (f" (improved from {px})" if is_aggressive and fill_price > px else "")
                        )
                    order["remaining_qty"] = 0
                    filled.append(self._build_fill_event(order, fill_price, qty, q))
                    self.ask_qty[contract] = max(0, self.ask_qty.get(contract, 0) + qty)
                    to_remove.append(order)

                    if is_aggressive:
                        order["aggressive"] = False
                elif q < -qty:
                    fq = max(0, min(-qty, -qty - q))

                    if self.verbose:
                        print(f" - TRADED: Sold {fq} @ {px}")

                    filled.append(self._build_fill_event(order, px, -fq, q))
                    self.ask_qty[contract] = max(0, self.ask_qty.get(contract, 0) - fq)
                    order["qty"] += fq
                    order["remaining_qty"] = order["qty"]

                    if order["qty"] >= 0:
                        to_remove.append(order)

            for order in to_remove:
                asks[px].remove(order)

        for px in list(asks.keys()):
            if len(asks[px]) == 0:
                del asks[px]

        return filled
