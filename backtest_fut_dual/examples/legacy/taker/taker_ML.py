import numpy as np

from public_tools.market import Market
from public_tools.strategy import Strategy


class Taker(Strategy):
    def __init__(
        self,
        name: str,
        market: Market,
        mult: int,
        tick: float,
        fee: float,
        rebate: float,
        fee_lot: float = None,  # fixed fee $ per lot per side; overrides fee rate when set
        # stop_thresh: float,
        # quote_start: int = 1,
        # quote_steps: int = 1,
        # quote_size: int = 1,
        taker_size: int = 1,
        min_l1_depth: int = 5,  # min volume required at best ask/bid level (L0)
        min_l1_perc: float = 0.0,  # min fraction of ask/bid L0 vol in first N levels; N = max(2, floor(0.5 * unwind_steps[0]))
        unwind_mult: list = [1, 2, 3],
        min_count=float("-inf"),
        max_pos: int = 1,
        signals=None,
        # cons_thresh = 1.0,
        taker_thresh=2.0,
        taker_step=0.2,
        max_spread=1,
        unwind_offset=0,
        unwind_thresh=-999,
        hrs=None,
        time_stop: int = None,  # quote() calls in taker_mode before stop mode engages
        loss_stop: float = None,  # adverse ticks between open_px and opposite best before stop mode
        stop_offset: int = 0,  # ticks offset on opposite bid/ask for the stop unwind order
        max_filled_qty: int = None,  # stop all activity once filled_qty reaches this threshold
        parallel: bool = True,
        verbose: bool = False,
    ):
        super().__init__(name, market, mult, tick, fee, rebate, fee_lot=fee_lot, parallel=parallel, verbose=verbose)

        self.name = name
        # self.quote_start = quote_start
        # self.quote_steps = quote_steps
        # self.quote_size = quote_size
        self.taker_size = taker_size
        self.taker_size_total = self.taker_size * len(unwind_mult)
        self.min_l1_depth = min_l1_depth
        self.min_l1_perc = min_l1_perc
        self.unwind_mult = unwind_mult  # price-fraction multipliers; steps computed per day
        self.unwind_steps = []  # refreshed each day from first mid price
        self.n_lev = 0  # number of ticks for L1 concentration check; computed from first unwind_steps[0]
        self._steps_refreshed = False
        self.min_count = min_count
        self.max_pos = max_pos
        self.signals = signals
        # self.cons_thresh = cons_thresh
        self.taker_thresh = taker_thresh
        self.taker_step = taker_step
        self.max_spread = max_spread
        self.unwind_offset = unwind_offset
        self.unwind_thresh = unwind_thresh
        self.hrs = set(hrs) if hrs is not None else None
        self.time_stop = time_stop
        self.loss_stop = loss_stop
        self.stop_offset = stop_offset
        self.max_filled_qty = max_filled_qty
        self.parallel = parallel

        # Calculate decimal places for price rounding based on tick size
        self.price_decimals = max(0, int(np.ceil(-np.log10(tick))))

        self.curr_signal = 0.0
        self.taker_mode = False
        self.stop_mode = False  # True when a time or loss stop has been triggered
        self.taker_quote_count = 0  # quote() calls elapsed while in taker_mode
        self.auto_unwind = False

        # Track open-unwind cycles
        self.open_position = None  # Just track opening cost and basic info
        self.completed_cycles = []  # Track each unwind fill separately
        self.cycle_id = 0

    def reset(self, contract: str):
        self.taker_mode = False
        self.stop_mode = False
        self.taker_quote_count = 0
        self.open_position = None
        self.completed_cycles = []
        self.cycle_id = 0
        self.unwind_steps = []  # will be recomputed on first quote of new day
        self.n_lev = 0  # will be computed from first mid price
        self._steps_refreshed = False

        return super().reset(contract)

    def update_pos(self, filled):
        # Track unwind fills if we have an open position
        if self.open_position is not None:
            for fill in filled:
                fill_px = fill["px"]
                fill_qty = fill["qty"]

                # Check if this fill is an unwind (opposite direction to open position)
                is_unwind = False
                actual_fill_qty = 0

                if fill_qty > 0 and self.open_position["direction"] == "short":
                    # Buying back short position
                    is_unwind = True
                    actual_fill_qty = fill_qty
                elif fill_qty < 0 and self.open_position["direction"] == "long":
                    # Selling long position
                    is_unwind = True
                    actual_fill_qty = -fill_qty

                if is_unwind and actual_fill_qty > 0:
                    # Calculate cycle PnL for this unwind fill
                    if self.open_position["direction"] == "long":
                        cycle_pnl = (fill_px - self.open_position["open_px"]) * actual_fill_qty * self.mult
                    else:  # short
                        cycle_pnl = (self.open_position["open_px"] - fill_px) * actual_fill_qty * self.mult

                    # Account for fees (opening + closing) and rebate
                    if self.fee_lot is not None:
                        total_fees = actual_fill_qty * self.fee_lot  # open-side + close-side, fixed per lot
                    else:
                        total_fees = (
                            (self.open_position["open_px"] + fill_px) * actual_fill_qty * self.mult * self.fee / 2.0
                        )
                    rebate_amount = total_fees * self.rebate
                    net_pnl = cycle_pnl - total_fees + rebate_amount

                    # Calculate unwind step (how many ticks away from open price)
                    if self.open_position["direction"] == "long":
                        unwind_step = (fill_px - self.open_position["open_px"]) // self.tick
                    else:
                        unwind_step = (self.open_position["open_px"] - fill_px) // self.tick

                    # Record completed cycle for this fill
                    completed_cycle = {
                        "cycle_id": self.open_position["cycle_id"],
                        "direction": self.open_position["direction"],
                        "open_px": self.open_position["open_px"],
                        "unwind_px": fill_px,
                        "qty": actual_fill_qty,
                        "open_time": self.open_position["open_time"],
                        "unwind_time": self.curr_md["datetime"],
                        "signal": self.open_position["signal"],
                        "opposite_volume": self.open_position["opposite_volume"],
                        "volume_ratio": self.open_position["volume_ratio"],
                        "unwind_step": int(unwind_step),
                        "gross_pnl": cycle_pnl,
                        "fees": total_fees,
                        "rebate": rebate_amount,
                        "net_pnl": net_pnl,
                        "duration_seconds": (self.curr_md["datetime"] - self.open_position["open_time"]).total_seconds()
                        if hasattr((self.curr_md["datetime"] - self.open_position["open_time"]), "total_seconds")
                        else 0,
                    }

                    self.completed_cycles.append(completed_cycle)

        pnl = super().update_pos(filled)

        if self.pos == 0:
            self.taker_mode = False
            self.stop_mode = False
            self.taker_quote_count = 0
            # Clear open position when flat
            self.open_position = None

        return pnl

    def refresh_unwind_steps(self, mid_price: float):
        """Compute integer tick steps to use for static unwind orders.

        Dual-mode: if an element of unwind_mult is >= 1 it is treated as a
        direct integer tick count (server-compatible format, e.g. [1], [4]).
        If it is < 1 it is treated as a price-fraction multiplier and the tick
        step is derived as round(mult * mid_price / tick), floored at 1.
        Called once per day on the first available mid price.
        """
        self.unwind_steps = [int(m) if m >= 1 else max(1, round(m * mid_price / self.tick)) for m in self.unwind_mult]

    def _ask_depth(self, md, n: int) -> int:
        """Sum ask volumes within a price range of n ticks above askpx0.
        Checks all 5 LOB levels; levels outside the range or missing are counted as 0."""
        px0 = md["askpx0"]
        total = md["askvol0"]  # Start with L0 volume

        for i in range(1, 5):
            px = md.get(f"askpx{i}")
            if px is not None:
                if px <= px0 + (n - 1) * self.tick:
                    total += md.get(f"askvol{i}", 0)
                else:
                    break  # levels are sorted, so we can stop once we exceed the range

        return total

    def _bid_depth(self, md, n: int) -> int:
        """Sum bid volumes within a price range of n ticks below bidpx0.
        Checks all 5 LOB levels; levels outside the range or missing are counted as 0."""
        px0 = md["bidpx0"]
        total = md["bidvol0"]  # Start with L0 volume

        for i in range(1, 5):
            px = md.get(f"bidpx{i}")
            if px is not None:
                if px >= px0 - (n - 1) * self.tick:
                    total += md.get(f"bidvol{i}", 0)
                else:
                    break  # levels are sorted, so we can stop once we exceed the range

        return total

    def quote(self, md):
        # super().quote(md)

        # Max filled qty cap: cancel all orders and do nothing
        if self.max_filled_qty is not None and self.filled_qty >= self.max_filled_qty:
            self.num_cancels += self.market.cancel_all_bids(self.contract)
            self.num_cancels += self.market.cancel_all_asks(self.contract)
            return

        # Hour filter: skip quoting outside allowed hours
        if self.hrs is not None and md["datetime"].hour not in self.hrs:
            return

        # Refresh unwind steps from the first mid price of each new day/contract
        if not self._steps_refreshed:
            mid = (md["bidpx0"] + md["askpx0"]) / 2.0
            self.refresh_unwind_steps(mid)
            # Compute n_lev based on first unwind step
            if self.unwind_steps:
                self.n_lev = max(2, int(0.5 * self.unwind_steps[0]))
            self._steps_refreshed = True

        if self.verbose:
            print(f"\n{self.name} quoting:")

        signals = self.signals[self.contract]

        if md["datetime"] in signals.index:
            val = signals.loc[md["datetime"]]
            self.curr_signal = float(val.iloc[0]) if hasattr(val, "iloc") else float(val)
            # print(f" - Signal: {self.curr_signal} @ {md['datetime']}")
            # input()
        else:
            # print(f" - No signal for {md['datetime']}")
            # print(signals.index)
            # input()

            self.curr_signal = 0.0

        # if self.taker_mode:
        #     return

        if self.taker_mode:
            # ── Stop-mode checks ────────────────────────────────────────────
            if not self.stop_mode:
                self.taker_quote_count += 1

                # Time stop: too many quote() calls without full unwind
                if self.time_stop is not None and self.taker_quote_count > self.time_stop:
                    self.stop_mode = True

                # Loss stop: market opposite-side has moved adversely by loss_stop ticks
                if self.loss_stop is not None and self.open_position is not None:
                    if self.pos > 0:  # long: watch bid falling below open_px
                        adverse_ticks = (self.open_position["open_px"] - md["bidpx0"]) / self.tick
                    else:  # short: watch ask rising above open_px
                        adverse_ticks = (md["askpx0"] - self.open_position["open_px"]) / self.tick
                    if adverse_ticks >= self.loss_stop:
                        self.stop_mode = True

            # ── Unwind / stop order ─────────────────────────────────────────
            if self.pos < 0:  # and self.curr_signal > self.unwind_thresh:
                if self.stop_mode:
                    # Aggressive buy: at ask + stop_offset ticks (crosses spread when offset=0)
                    unwind_price = self.snap_price(md["askpx0"] + self.stop_offset * self.tick)
                else:
                    unwind_price = min(md["bidpx0"] + self.unwind_offset * self.tick, md["askpx0"] - self.tick)
                    # unwind_price = max(md['askpx0'] - self.unwind_offset * self.tick, md['bidpx0'])
                    unwind_price = self.snap_price(unwind_price)

                self.bid_and_cancel([unwind_price], self.min_count)
                # self.bid_and_cancel([md['askpx0'] - self.tick], self.quote_size)
            elif self.pos > 0:  # and self.curr_signal < -self.unwind_thresh:
                if self.stop_mode:
                    # Aggressive sell: at bid - stop_offset ticks (crosses spread when offset=0)
                    unwind_price = self.snap_price(md["bidpx0"] - self.stop_offset * self.tick)
                else:
                    unwind_price = max(md["askpx0"] - self.unwind_offset * self.tick, md["bidpx0"] + self.tick)
                    # unwind_price = min(md['bidpx0'] + self.unwind_offset * self.tick, md['askpx0'])
                    unwind_price = self.snap_price(unwind_price)

                self.ask_and_cancel([unwind_price], self.min_count)
                # self.ask_and_cancel([md['bidpx0'] + self.tick], self.quote_size)
        elif self.curr_signal > self.taker_thresh and self.pos == 0:
            steps = int((self.curr_signal - self.taker_thresh) // self.taker_step) + 1
            size = self.taker_size_total * steps
            unwind = self.taker_size * steps

            # Sum volumes across price range implied by self.n_lev (each level = 1 tick), treating missing levels as 0
            ask_total = self._ask_depth(md, self.n_lev)
            if (
                size > 0
                and (ask_total > 0 and md["askvol0"] / ask_total > self.min_l1_perc)
                and md["askvol0"] > self.min_l1_depth
                and round((md["askpx0"] - md["bidpx0"]) / self.tick) <= self.max_spread
            ):
                self.take_up(size, unwind)
        elif self.curr_signal < -self.taker_thresh and self.pos == 0:
            steps = int((-self.curr_signal - self.taker_thresh) // self.taker_step) + 1
            size = self.taker_size_total * steps
            unwind = self.taker_size * steps

            # Sum volumes across price range implied by self.n_lev (each level = 1 tick), treating missing levels as 0
            bid_total = self._bid_depth(md, self.n_lev)
            if (
                size > 0
                and (bid_total > 0 and md["bidvol0"] / bid_total > self.min_l1_perc)
                and md["bidvol0"] > self.min_l1_depth
                and round((md["askpx0"] - md["bidpx0"]) / self.tick) <= self.max_spread
            ):
                self.take_dn(size, unwind)

        ### DONE ###

        if self.verbose:
            print(" - Asks:", self.market.asks)
            print(" - Bids:", self.market.bids)

    def take_up(self, size, unwind):
        self.num_cancels += self.market.cancel_all_bids(self.contract)
        self.num_cancels += self.market.cancel_all_asks(self.contract)

        px = self.curr_md["askpx0"]

        if self.verbose:
            print(" - Placing Taker Buy @", px)

        filled = self.market.fak(self.contract, px, size)
        self.num_orders += 1

        if filled["qty"] != size:
            self.num_cancels += 1

        if filled["qty"] > 0:
            self.taker_mode = True
            self.num_fills += 1
            self.filled_qty += abs(filled["qty"])
            self.update_pos([filled])

            # Track the opened position (simplified - just opening info)
            self.cycle_id += 1
            self.open_position = {
                "cycle_id": self.cycle_id,
                "direction": "long",
                "open_px": px,
                "open_qty": filled["qty"],
                "open_time": self.curr_md["datetime"],
                "signal": self.curr_signal,
                "opposite_volume": self.curr_md["askvol0"],  # Volume on opposite side when taking up
                "volume_ratio": self.curr_md["bidvol0"] / self.curr_md["askvol0"],
            }

            # Place unwind orders as before
            remaining_qty = filled["qty"]
            for s in self.unwind_steps:
                unwind_size = min(remaining_qty, unwind)
                unwind_px = self.snap_price(px + s * self.tick)

                # if unwind_px not in self.market.asks.keys():
                self.market.place_order(self.contract, unwind_px, -unwind_size)
                self.num_orders += 1
                if not self.parallel:
                    self.event_log.append(
                        {"time": self.curr_md["datetime"], "event": "order", "price": unwind_px, "qty": -unwind_size}
                    )

                remaining_qty -= unwind_size
                if remaining_qty <= 0:
                    break

    def take_dn(self, size, unwind):
        self.num_cancels += self.market.cancel_all_bids(self.contract)
        self.num_cancels += self.market.cancel_all_asks(self.contract)

        px = self.curr_md["bidpx0"]

        if self.verbose:
            print(" - Placing Taker Sell @", px)

        filled = self.market.fak(self.contract, px, -size)
        self.num_orders += 1

        if filled["qty"] != -size:
            self.num_cancels += 1

        if filled["qty"] < 0:
            self.taker_mode = True
            self.num_fills += 1
            self.filled_qty += abs(filled["qty"])
            self.update_pos([filled])

            # Track the opened position (simplified - just opening info)
            self.cycle_id += 1
            self.open_position = {
                "cycle_id": self.cycle_id,
                "direction": "short",
                "open_px": px,
                "open_qty": -filled["qty"],  # Make positive for tracking
                "open_time": self.curr_md["datetime"],
                "signal": self.curr_signal,
                "opposite_volume": self.curr_md["bidvol0"],  # Volume on opposite side when taking down
                "volume_ratio": self.curr_md["askvol0"] / self.curr_md["bidvol0"],
            }

            # Place unwind orders as before
            remaining_qty = -filled["qty"]
            for s in self.unwind_steps:
                unwind_size = min(remaining_qty, unwind)
                unwind_px = self.snap_price(px - s * self.tick)

                # if unwind_px not in self.market.bids.keys():
                self.market.place_order(self.contract, unwind_px, unwind_size)
                self.num_orders += 1
                if not self.parallel:
                    self.event_log.append(
                        {"time": self.curr_md["datetime"], "event": "order", "price": unwind_px, "qty": unwind_size}
                    )

                remaining_qty -= unwind_size
                if remaining_qty <= 0:
                    break

    def get_extra(self):
        return {
            "signal": self.curr_signal,
            "has_open_position": self.open_position is not None,
            "completed_cycles": len(self.completed_cycles),
        }

    def get_cycle_stats(self):
        """Get statistics on completed open-unwind cycles"""
        if not self.completed_cycles:
            return {}

        import pandas as pd

        df = pd.DataFrame(self.completed_cycles)

        if len(df) == 0:
            return {}

        stats = {
            "total_cycles": len(df),
            "avg_net_pnl": df["net_pnl"].mean(),
            "std_net_pnl": df["net_pnl"].std(),
            "win_rate": (df["net_pnl"] > 0).mean(),
            "avg_gross_pnl": df["gross_pnl"].mean(),
            "avg_fees": df["fees"].mean(),
            "avg_duration_seconds": df["duration_seconds"].mean(),
            "total_net_pnl": df["net_pnl"].sum(),
            "best_trade": df["net_pnl"].max(),
            "worst_trade": df["net_pnl"].min(),
            "profit_factor": df[df["net_pnl"] > 0]["net_pnl"].sum() / abs(df[df["net_pnl"] < 0]["net_pnl"].sum())
            if (df["net_pnl"] < 0).any()
            else float("inf"),
        }

        # Add signal-based analysis if signals vary
        if df["signal"].std() > 0:
            try:
                stats["pnl_by_signal_quintile"] = (
                    df.groupby(
                        pd.qcut(df["signal"].abs(), 5, duplicates="drop", labels=["Q1", "Q2", "Q3", "Q4", "Q5"]),
                        observed=True,
                    )["net_pnl"]
                    .mean()
                    .to_dict()
                )
            except:
                stats["pnl_by_signal_quintile"] = {}

        # Add unwind step analysis
        stats["pnl_by_unwind_step"] = df.groupby("unwind_step")["net_pnl"].sum().to_dict()
        stats["count_by_unwind_step"] = df.groupby("unwind_step").size().to_dict()

        # Add opposite volume analysis with quantiles
        if "opposite_volume" in df.columns:
            opposite_vol = df["opposite_volume"]
            stats["opposite_volume_stats"] = {
                "mean": opposite_vol.mean(),
                "median": opposite_vol.median(),
                "std": opposite_vol.std(),
                # 'min': opposite_vol.min(),
                # 'max': opposite_vol.max(),
                "q10": opposite_vol.quantile(0.10),
                "q25": opposite_vol.quantile(0.25),
                "q50": opposite_vol.quantile(0.50),
                "q75": opposite_vol.quantile(0.75),
                "q90": opposite_vol.quantile(0.90),
            }

            # PnL by opposite volume quintiles
            try:
                opposite_vol_quintiles = pd.qcut(
                    opposite_vol, 5, duplicates="drop", labels=["Q1", "Q2", "Q3", "Q4", "Q5"]
                )
                stats["pnl_by_opposite_volume_quintile"] = (
                    df.groupby(opposite_vol_quintiles, observed=True)["net_pnl"].agg(["mean", "sum", "count"]).to_dict()
                )
            except:
                stats["pnl_by_opposite_volume_quintile"] = {}

        if "volume_ratio" in df.columns:
            vol_ratio = df["volume_ratio"]
            stats["volume_ratio_stats"] = {
                "mean": vol_ratio.mean(),
                "median": vol_ratio.median(),
                "std": vol_ratio.std(),
                # 'min': vol_ratio.min(),
                # 'max': vol_ratio.max(),
                "q10": vol_ratio.quantile(0.10),
                "q25": vol_ratio.quantile(0.25),
                "q50": vol_ratio.quantile(0.50),
                "q75": vol_ratio.quantile(0.75),
                "q90": vol_ratio.quantile(0.90),
            }

            # PnL by volume ratio quintiles
            try:
                vol_ratio_quintiles = pd.qcut(vol_ratio, 5, duplicates="drop", labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
                stats["pnl_by_volume_ratio_quintile"] = (
                    df.groupby(vol_ratio_quintiles, observed=True)["net_pnl"].agg(["mean", "sum", "count"]).to_dict()
                )
            except:
                stats["pnl_by_volume_ratio_quintile"] = {}

        return stats

    def get_completed_cycles_df(self):
        """Return DataFrame of completed cycles for further analysis"""
        import pandas as pd

        return pd.DataFrame(self.completed_cycles) if self.completed_cycles else pd.DataFrame()

    def summarize(self):
        """Override summarize to include cycle tracking"""
        summary = super().summarize()
        summary.update(
            {
                "cycle_stats": self.get_cycle_stats(),
                "has_open_position": self.open_position is not None,
                "total_completed_cycles": len(self.completed_cycles),
            }
        )
        return summary
