import datetime
import os
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from common.market import Market
from common.foundation_contracts import InstrumentSpec
from common.sessions import DAY_SESSION, NIGHT_SESSION, DEFAULT_SESSION_CALENDAR


@dataclass(frozen=True)
class TradingDayEvent:
    """Calendar lifecycle event emitted by the Phase-1 trading-day runner."""

    kind: str
    product: str
    trading_day: datetime.date
    scheduled_at: datetime.datetime
    window_name: str | None = None


def load(fn, mult, tick, calendar=None):
    """Load one product file.

    ``calendar=None`` preserves the legacy two-frame return for external callers.
    Product-aware callers pass an injected calendar and receive one continuous
    trading-day frame, including each calendar's declared EOD timestamp.
    """
    c = pd.read_csv(fn, parse_dates=True)
    c["timestamp"] = pd.to_datetime(c["timestamp"])
    c["exchtime"] = pd.to_datetime(c["exchtime"])
    c.set_index("exchtime", drop=True, inplace=True)
    # c.drop('timestamp', axis = 1, inplace = True)
    c = c[(~c.index.duplicated(keep="last")) & (c["bidpx0"] > 0) & (c["totalvol"] > 0)]
    # c = c.between_time('9:00', '8:00')

    c.loc[c["bidvol0"] <= 0, "bidpx0"] = c.loc[c["bidvol0"] <= 0, "askpx0"]
    c.loc[c["bidvol1"] <= 0, "bidpx1"] = c.loc[c["bidvol1"] <= 0, "bidpx0"]
    c.loc[c["bidvol2"] <= 0, "bidpx2"] = c.loc[c["bidvol2"] <= 0, "bidpx1"]
    c.loc[c["bidvol3"] <= 0, "bidpx3"] = c.loc[c["bidvol3"] <= 0, "bidpx2"]
    c.loc[c["bidvol4"] <= 0, "bidpx4"] = c.loc[c["bidvol4"] <= 0, "bidpx3"]

    c.loc[c["askvol0"] <= 0, "askpx0"] = c.loc[c["askvol0"] <= 0, "bidpx0"]
    c.loc[c["askvol1"] <= 0, "askpx1"] = c.loc[c["askvol1"] <= 0, "askpx0"]
    c.loc[c["askvol2"] <= 0, "askpx2"] = c.loc[c["askvol2"] <= 0, "askpx1"]
    c.loc[c["askvol3"] <= 0, "askpx3"] = c.loc[c["askvol3"] <= 0, "askpx2"]
    c.loc[c["askvol4"] <= 0, "askpx4"] = c.loc[c["askvol4"] <= 0, "askpx3"]

    volume = c["totalvol"].diff()
    volume.iloc[0] = c["totalvol"].iloc[0]
    c["totalvol"] = volume

    totalvalue = c["totalvalue"].diff()
    totalvalue.iloc[0] = c["totalvalue"].iloc[0]
    c["totalvalue"] = totalvalue

    # Calculate traded price, handling zero volume cases
    # When totalvol is 0, there's no trading, so 'traded' should remain NaN
    c["traded"] = np.where(c["totalvol"] > 0, c["totalvalue"] / c["totalvol"] / mult, np.nan)

    # Forward fill other columns (bid/ask prices, volumes) but NOT 'traded'
    # 'traded' should only have values when actual trading occurred
    cols_to_ffill = [col for col in c.columns if col != "traded"]
    c[cols_to_ffill] = c[cols_to_ffill].ffill()

    # Determine precision based on tick size
    tick_precision = len(str(tick).split(".")[-1]) if "." in str(tick) else 0

    c["traded_p1"] = np.round(np.floor(c["traded"] / tick) * tick, tick_precision)  # Lower bound
    c["traded_p2"] = np.round(c["traded_p1"] + tick, tick_precision)  # Upper bound

    # Linear interpolation: if traded is closer to p1, more volume at p1
    # Formula: v1 = totalvol * (p2 - traded) / (p2 - p1)
    #          v2 = totalvol * (traded - p1) / (p2 - p1)
    c["traded_v2"] = ((c["traded"] - c["traded_p1"]) * c["totalvol"] / tick).round()
    c["traded_v1"] = (c["totalvol"] - c["traded_v2"]).round()

    # c.to_csv('output/processed_MD.csv', index=True)
    # input('\nSaved processed MD')

    # if len(c) < 5000:
    #     print('LENGTH NOT SUFFICIENT:', len(c), '-', fn)

    #     return None, None

    if calendar is not None:
        keep = []
        for timestamp in c.index:
            allowed = calendar.is_trading_time(timestamp.to_pydatetime())
            if not allowed and calendar.missing_data_disposition == "reject":
                raise ValueError(f"timestamp outside declared calendar: {timestamp}")
            keep.append(allowed)
        return c.loc[keep]

    # Deprecated compatibility path. New scheduling must use the injected
    # calendar route above rather than these global windows.
    c1 = c.between_time(*NIGHT_SESSION)
    c2 = c.between_time(*DAY_SESSION)

    return c1, c2


def load_signals(
    path,
    limit=None,
    *,
    declared_contract_universe=None,
    active_contracts_by_date=None,
    missing_data_disposition="reject",
):
    """Load prediction series against an explicit dual-contract declaration.

    The previous positional ``groupby(...).nth(-2/-1)`` selection depended on
    filename ordering and silently returned an incomplete universe.  A supported
    caller must now declare its universe and, for a universe larger than two,
    provide an explicit date-to-active-pair schedule.  Historical callers that
    cannot do so must remain outside the foundation path rather than infer a
    pair accidentally.
    """
    universe = _normalise_contract_universe(declared_contract_universe)
    if missing_data_disposition not in {"reject", "drop"}:
        raise ValueError("missing_data_disposition must be 'reject' or 'drop'")
    schedule = _normalise_active_contract_schedule(active_contracts_by_date, universe)
    if schedule is None and len(universe) != 2:
        raise ValueError("active_contracts_by_date is required when declared_contract_universe has more than two contracts")
    signals = {}
    availability: dict[datetime.date, set[str]] = {}

    files = [
        fn for fn in sorted(os.listdir(path))
        if os.path.isfile(os.path.join(path, fn))
        and fn.lower().endswith((".csv", ".parquet", ".pq"))
    ]
    if limit is not None:
        files = files[:limit]
    if not files:
        raise FileNotFoundError(
            f"No CSV or Parquet prediction files found in signal path: {path!r}")

    for fn in files:
        stem, ext = os.path.splitext(fn)
        parts = stem.split("_")
        if len(parts) < 2:
            raise ValueError(f"Cannot infer contract from signal filename: {fn}")
        symbol = parts[1]
        file_path = os.path.join(path, fn)

        frame = (pd.read_parquet(file_path)
                 if ext.lower() in (".parquet", ".pq")
                 else pd.read_csv(file_path, parse_dates=True, index_col=0))
        if "pred" not in frame:
            raise KeyError(f"Signal file {file_path!r} has no 'pred' column.")

        sig = frame["pred"].copy()
        sig.index = pd.to_datetime(sig.index)
        if sig.index.isna().any():
            raise ValueError(f"Signal index contains invalid timestamps: {file_path}")
        if symbol not in universe:
            raise ValueError(f"Signal contract {symbol!r} is outside declared_contract_universe")
        if symbol in signals:
            raise ValueError(f"Duplicate signal files declare contract {symbol!r}")
        signals[symbol] = sig
        for day in (sig.index + datetime.timedelta(hours=6)).date:
            availability.setdefault(day, set()).add(symbol)

    requested_days = set(schedule) if schedule is not None else set(availability)
    active_pairs: dict[datetime.date, tuple[str, str]] = {}
    for day in sorted(requested_days):
        active = schedule[day] if schedule is not None else (universe[0], universe[1])
        missing = set(active) - availability.get(day, set())
        if missing:
            if missing_data_disposition == "reject":
                raise ValueError(f"Declared active contracts are missing signal coverage for {day}: {sorted(missing)}")
            continue
        active_pairs[day] = active
    if not active_pairs:
        raise ValueError("No trading date has complete declared active-contract signal coverage")
    date_contracts = pd.DataFrame.from_dict(active_pairs, orient="index", columns=("contract_1", "contract_2")).sort_index()
    return signals, date_contracts


def _normalise_contract_universe(value) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        raise ValueError("declared_contract_universe must declare at least two contracts")
    universe = tuple(value)
    if any(not isinstance(contract, str) or not contract.strip() for contract in universe):
        raise ValueError("declared_contract_universe must contain non-empty contract strings")
    if len(set(universe)) != len(universe):
        raise ValueError("declared_contract_universe must not contain duplicate contracts")
    return universe


def _normalise_active_contract_schedule(value, universe: tuple[str, ...]) -> dict[datetime.date, tuple[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("active_contracts_by_date must be a mapping of date to two declared contracts")
    schedule: dict[datetime.date, tuple[str, str]] = {}
    for raw_day, raw_contracts in value.items():
        try:
            day = pd.Timestamp(raw_day).date()
        except (TypeError, ValueError) as exc:
            raise ValueError("active_contracts_by_date keys must be valid dates") from exc
        if not isinstance(raw_contracts, (tuple, list)) or len(raw_contracts) != 2:
            raise ValueError("each active_contracts_by_date value must contain exactly two contracts")
        pair = tuple(raw_contracts)
        if pair[0] == pair[1] or any(contract not in universe for contract in pair):
            raise ValueError("active contracts must be distinct members of declared_contract_universe")
        schedule[day] = pair
    return schedule


def run_pair_session(market, coordinator, frame_p, frame_s, c1, c2):
    """Run ONE session of a dual-contract (pair) backtest over a ``PairMarket``.

    The two legs are advanced SIDE-BY-SIDE by exchange datetime (``market.step_pair``) and dispatched to the
    coordinator's per-exchtime aligned step, so both legs' books/snapshots are coherent at each exchtime
    BEFORE the strategy acts (the foundation for correct cross-leg interactions, e.g. a hedge cross right
    after a passive fill). Mirrors the pairs driver's single-stream session loop, but exchtime-aligned.
    The coordinator is duck-typed (reset / step_aligned / stop / summarize / completed_cycles).
    Returns ``(summary, completed_cycles)``."""
    market.load_pair(frame_p, frame_s)
    coordinator.reset(c1, c2)
    coordinator.session_total_steps = len(frame_p) + len(frame_s)
    while True:
        bundle = market.step_pair()
        if bundle is None:
            coordinator.stop()
            break
        coordinator.step_aligned(bundle)
    return coordinator.summarize(), list(coordinator.completed_cycles)


def run_date(bt, date, contracts, path):
    date = str(date)
    year = date[:4]

    if not bt.parallel:
        print("\nDate:", date, "- Contracts:", contracts)

    day_pnl = 0
    day_fees = 0
    day_orders = 0
    day_fills = 0
    day_filled_qty = 0
    day_cancels = 0
    day_cycles = []

    day_frames = []
    known_specs = dict(getattr(bt, "instrument_specs", {}))
    active_specs = {}

    for contract in contracts:
        if pd.notna(contract):
            fn = os.path.join(path, year, date, contract + ".csv")

            spec = known_specs.get(contract)
            if spec is None:
                spec = InstrumentSpec(
                    contract,
                    float(bt.tick),
                    float(bt.mult),
                    DEFAULT_SESSION_CALENDAR,
                    "legacy-fee-model",
                    "legacy-roll-mapping",
                )
            active_specs[contract] = spec
            frame = load(fn, spec.multiplier, spec.tick, calendar=spec.calendar)
            frame["contract"] = contract
            day_frames.append(frame)

    if day_frames:
        md_combined = pd.concat(day_frames, axis=0).sort_values("timestamp", kind="stable")
        if len(md_combined) > 0:
            pnl, fees, num_orders, num_fills, filled_qty, num_cancels, cycles = bt.backtest_trading_day(
                md_combined,
                active_specs,
                trading_day=pd.to_datetime(date).date(),
            )
            day_pnl += pnl
            day_fees += fees
            day_orders += num_orders
            day_fills += num_fills
            day_filled_qty += filled_qty
            day_cancels += num_cancels
            day_cycles.extend(cycles)

    result = {
        "date": date,
        "pnl": day_pnl,
        "fees": day_fees,
        "orders": day_orders,
        "fills": day_fills,
        "filled_qty": day_filled_qty,
        "cancels": day_cancels,
        "cycles": day_cycles,
    }
    from common.order_limit import day_message_summary
    result.update(day_message_summary(bt.market, date))
    return result


class Backtest:
    def __init__(
        self,
        market: Market,
        strategies: list,
        mult: int,
        tick: float,
        parallel: bool = True,
        instrument_specs=None,
    ):
        self.market = market
        self.strategies = strategies
        self.mult = mult
        self.tick = tick
        self.parallel = parallel
        self.instrument_specs = self._normalise_instrument_specs(instrument_specs or {})
        self.last_session_events = []
        self.last_eod_outcomes = []

        if self.instrument_specs:
            self.market.set_instrument_specs(self.instrument_specs)

        for s in strategies:
            s.market = self.market

    @staticmethod
    def _normalise_instrument_specs(instrument_specs):
        if hasattr(instrument_specs, "items"):
            items = instrument_specs.items()
        else:
            items = ((spec.product, spec) for spec in instrument_specs)
        specs = {}
        for product, spec in items:
            if not isinstance(spec, InstrumentSpec) or spec.product != product:
                raise ValueError("instrument_specs must map product names to InstrumentSpec values")
            specs[product] = spec
        return specs

    def _collect_summaries(self):
        pnl = fees = num_orders = num_fills = filled_qty = num_cancels = 0
        all_cycles = []
        for strategy in self.strategies:
            summary = strategy.summarize()
            pnl += summary["pnl"]
            fees += summary["fees"]
            num_orders += summary["num_orders"]
            num_fills += summary["num_fills"]
            filled_qty += summary["filled_qty"]
            num_cancels += summary["num_cancels"]
            if hasattr(strategy, "completed_cycles"):
                all_cycles.extend(strategy.completed_cycles)
        return pnl, fees, num_orders, num_fills, filled_qty, num_cancels, all_cycles

    def backtest_trading_day(self, md: pd.DataFrame, instrument_specs=None, *, trading_day=None, strategy_products=None):
        """Run one calendar trading day without resetting across intra-day breaks.

        This is the supported Phase-1 path. It initializes each strategy once,
        keeps the market/order/strategy state alive while windows are separated
        by breaks, and emits a declared EOD event rather than calling the legacy
        synthetic-touch ``Strategy.stop`` implementation.
        """
        if not isinstance(md, pd.DataFrame):
            raise TypeError("backtest_trading_day requires a pandas DataFrame")
        if "contract" not in md.columns:
            raise ValueError("trading-day data must contain a contract column")
        specs = self._normalise_instrument_specs(instrument_specs or self.instrument_specs)
        if not specs:
            raise ValueError("backtest_trading_day requires injected InstrumentSpec values")
        self.instrument_specs = specs
        self.market.set_instrument_specs(specs)

        if strategy_products is None:
            strategy_products = list(specs)
        if len(strategy_products) != len(self.strategies):
            raise ValueError("strategy_products must bind exactly one product to each strategy")
        if any(product not in specs for product in strategy_products):
            raise ValueError("strategy_products contains a product without an InstrumentSpec")

        frame = md.copy()
        timestamps = pd.to_datetime(frame.index)
        inferred_days = set()
        keep = []
        for index, (_, row) in enumerate(frame.iterrows()):
            product = row["contract"]
            if product not in specs:
                raise ValueError(f"market data product has no InstrumentSpec: {product}")
            calendar = specs[product].calendar
            timestamp = timestamps[index].to_pydatetime()
            inferred_days.add(calendar.trading_day_of(timestamp))
            allowed = calendar.is_trading_time(timestamp)
            if allowed:
                keep.append(True)
            elif calendar.missing_data_disposition == "drop":
                keep.append(False)
            else:
                raise ValueError(f"timestamp outside declared calendar for {product}: {timestamp}")
        if not inferred_days:
            raise ValueError("trading-day data is empty")
        if trading_day is None:
            if len(inferred_days) != 1:
                raise ValueError("trading-day data spans multiple calendar trading days")
            trading_day = inferred_days.pop()
        else:
            trading_day = pd.to_datetime(trading_day).date()
            if any(day != trading_day for day in inferred_days):
                raise ValueError("market-data timestamp does not belong to requested trading_day")
        frame = frame.loc[keep].sort_values("timestamp", kind="stable")

        self.last_session_events = []
        self.last_eod_outcomes = []
        strategies_by_product = {}
        for strategy, product in zip(self.strategies, strategy_products):
            strategy.bind_instrument_spec(specs[product])
            strategy.reset(product, trading_day)
            strategy.session_total_steps = int((frame["contract"] == product).sum())
            strategies_by_product.setdefault(product, []).append(strategy)

        self.market.load_md(frame)
        last_windows = {}
        while True:
            curr = self.market.step()
            if curr is None:
                break
            product = curr["contract"]
            calendar = specs[product].calendar
            timestamp = pd.Timestamp(curr["datetime"]).to_pydatetime()
            window = calendar.window_at(timestamp)
            previous = last_windows.get(product)
            if previous is not None and previous.name != window.name:
                event = TradingDayEvent(
                    "session_break",
                    product,
                    trading_day,
                    calendar.window_end_at(trading_day, previous),
                    previous.name,
                )
                self.last_session_events.append(event)
                for strategy in strategies_by_product.get(product, []):
                    strategy.on_session_break(event)
            last_windows[product] = window
            for strategy in strategies_by_product.get(product, []):
                strategy.session_window = window.name
                strategy.step(curr)

        for product, strategies in strategies_by_product.items():
            eod_at = specs[product].calendar.eod_at(trading_day)
            if eod_at is None:
                continue
            event = TradingDayEvent("eod", product, trading_day, eod_at)
            self.last_session_events.append(event)
            for strategy in strategies:
                self.last_eod_outcomes.append(strategy.on_eod(event))
        return self._collect_summaries()

    def backtest(self, md, contracts: list):
        self.market.load_md(md)
        session_total_steps = len(md)

        i = 0
        for strategy in self.strategies:
            if i < len(contracts):
                strategy.reset(contracts[i])
                strategy.session_total_steps = session_total_steps
            i += 1

        while True:
            md = self.market.step()

            if md is None:
                for s in self.strategies:
                    s.stop()

                break

            # sys.stdout.write('.')
            # sys.stdout.flush()

            for s in self.strategies:
                s.step(md)

        pnl = 0
        fees = 0
        num_orders = 0
        num_fills = 0
        filled_qty = 0
        num_cancels = 0

        for s in self.strategies:
            summary = s.summarize()

            if not self.parallel:
                print("\nSummary for", s.name, "<", s.contract, ">")
                print(" - PnL:", round(summary["pnl"], 1))
                print(" - Fees:", round(summary["fees"], 1))
                print(" - Orders:", round(summary["num_orders"], 1))
                print(" - Fills:", round(summary["num_fills"], 1))
                print(" - Filled Qty:", round(summary["filled_qty"], 1))
                print(" - Cancels:", round(summary["num_cancels"], 1))

                if "cycle_stats" in summary:
                    print(" - Cycle Stats:")

                    for key, value in summary["cycle_stats"].items():
                        print(f"   - {key}: {value}")

                # input("Press Enter to continue...")

            pnl += summary["pnl"]
            fees += summary["fees"]
            num_orders += summary["num_orders"]
            num_fills += summary["num_fills"]
            filled_qty += summary["filled_qty"]
            num_cancels += summary["num_cancels"]

            if not self.parallel:
                s.save_record()
                s.plot_record()
                # print("Record saved for", s.name)
                # input("Press Enter to continue...")

        # Collect completed cycles from all strategies
        all_cycles = []
        for s in self.strategies:
            if hasattr(s, "completed_cycles"):
                all_cycles.extend(s.completed_cycles)

        return pnl, fees, num_orders, num_fills, filled_qty, num_cancels, all_cycles
