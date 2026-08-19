import datetime
import itertools
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from joblib import Parallel, delayed

from examples.legacy.taker.taker_ML import Taker

# from factors.config.config_rb import *
from public_tools.backtest import Backtest
from public_tools.market import Market

# from spreader import Spreader
# from config_ML_v5.config_T import *
HRS = None  # default: trade all hours; configs may override with a list/set of hours
FEE_LOT = None  # default: use FEE rate; set to fixed $ per lot per side in config to override
TIME_STOP = None  # default: no time stop; set to int (quote() calls) in config to enable
LOSS_STOP = None  # default: no loss stop; set to float (ticks) in config to enable
STOP_OFFSET = 0  # default: close at opposite bid/ask (0 extra offset) when stop fires
BEST_PERIOD = None  # ('2025-07-01', '2025-12-31')

_CONFIG_IMPORT_ERROR = None
try:
    from examples.legacy.taker.config_ML_v5.config_IH_MR_Liq_0_2 import *
except ModuleNotFoundError as exc:
    _CONFIG_IMPORT_ERROR = exc

###################################################################################################

try:
    from example_taker.utils import display_summary
except ModuleNotFoundError:

    def display_summary(*args, **kwargs):
        raise ModuleNotFoundError("example_taker.utils.display_summary is required to render the final taker summary.")


def load(fn, mult, tick):
    c = pd.read_csv(fn, parse_dates=True)
    c["timestamp"] = pd.to_datetime(c["timestamp"])
    c["exchtime"] = pd.to_datetime(c["exchtime"])
    c.set_index("exchtime", drop=True, inplace=True)
    c = c[(~c.index.duplicated(keep="last")) & (c["bidpx0"] > 0) & (c["totalvol"] > 0)]

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

    c["traded"] = np.where(c["totalvol"] > 0, c["totalvalue"] / c["totalvol"] / mult, np.nan)
    cols_to_ffill = [col for col in c.columns if col != "traded"]
    c[cols_to_ffill] = c[cols_to_ffill].ffill()

    tick_precision = len(str(tick).split(".")[-1]) if "." in str(tick) else 0
    c["traded_p1"] = np.round(np.floor(c["traded"] / tick) * tick, tick_precision)
    c["traded_p2"] = np.round(c["traded_p1"] + tick, tick_precision)
    c["traded_v2"] = ((c["traded"] - c["traded_p1"]) * c["totalvol"] / tick).round()
    c["traded_v1"] = (c["totalvol"] - c["traded_v2"]).round()

    return c.between_time("21:00", "3:00"), c.between_time("9:00", "15:00")


def load_signals(path, limit=None):
    signals = {}
    date_contracts = []

    for fn in [f for f in sorted(os.listdir(path)) if os.path.isfile(os.path.join(path, f))][:limit]:
        if fn.endswith(".csv"):
            symbol = fn.split(".")[0].split("_")[1]

            print("Loading signals for:", symbol, "from", fn)

            sig = pd.read_csv(os.path.join(path, fn), parse_dates=True, index_col=0)["pred"]
            sig.index = pd.to_datetime(sig.index)
            signals[symbol] = sig

            date_contracts.append(
                pd.Series(symbol, index=pd.Series((sig.index + datetime.timedelta(hours=6)).date).drop_duplicates())
            )

    date_contracts = pd.concat(date_contracts, axis=0)
    date_contracts_1 = date_contracts.groupby(date_contracts.index).nth(-2)
    date_contracts_1.name = "contract_1"
    date_contracts_2 = date_contracts.groupby(date_contracts.index).nth(-1)
    date_contracts_2.name = "contract_2"

    date_contracts = pd.concat([date_contracts_1, date_contracts_2], axis=1).sort_index()
    return signals, date_contracts


def run_date(bt, date, contracts, path):
    date = str(date)
    year = date[:4]

    if bt.parallel:
        sys.stdout.write(".")
        sys.stdout.flush()
    else:
        print("\nDate:", date, "- Contracts:", contracts)

    day_pnl = 0
    day_fees = 0
    day_orders = 0
    day_fills = 0
    day_filled_qty = 0
    day_cancels = 0
    day_cycles = []

    md1_comb = []
    md2_comb = []

    for contract in contracts:
        if pd.notna(contract):
            fn = os.path.join(path, year, date, contract + ".csv")
            md1, md2 = load(fn, bt.mult, bt.tick)
            md1["contract"] = contract
            md2["contract"] = contract
            md1_comb.append(md1)
            md2_comb.append(md2)

    md1_comb = pd.concat(md1_comb, axis=0).sort_values("timestamp")
    md2_comb = pd.concat(md2_comb, axis=0).sort_values("timestamp")

    if len(md1_comb) > 0:
        pnl, fees, num_orders, num_fills, filled_qty, num_cancels, cycles = bt.backtest(md1_comb, contracts)
        day_pnl += pnl
        day_fees += fees
        day_orders += num_orders
        day_fills += num_fills
        day_filled_qty += filled_qty
        day_cancels += num_cancels
        day_cycles.extend(cycles)

    if len(md2_comb) > 0:
        pnl, fees, num_orders, num_fills, filled_qty, num_cancels, cycles = bt.backtest(md2_comb, contracts)
        day_pnl += pnl
        day_fees += fees
        day_orders += num_orders
        day_fills += num_fills
        day_filled_qty += filled_qty
        day_cancels += num_cancels
        day_cycles.extend(cycles)

    return {
        "date": date,
        "pnl": day_pnl,
        "fees": day_fees,
        "orders": day_orders,
        "fills": day_fills,
        "filled_qty": day_filled_qty,
        "cancels": day_cancels,
        "cycles": day_cycles,
    }

# MD_PATH = 'E:/FinData/HFT/ticks/SHFE/'
# MD_PATH = '/ext/fut_tick/SHFE/'
MD_PATH = "/ext/fut_tick/CFFEX/"
# CONTRACT = 'rb2511'

REBATE = 0.1
# CUTOFF = '2024-7-1'
CUTOFF = "2025-1-1"
# CUTOFF = '2024-11-28'

PARALLEL = True
VERBOSE = False

TOD_BUCKET_MINUTES = 30  # Time-of-day bucket size in minutes for cycle analysis
DIAG_WINDOW = 20  # Rolling window (days) for regime diagnostics

# ── Grid-search parameter names (Taker constructor args that may be lists in config) ──────────
# Each config variable listed here is normalised to a list of candidates;
# single scalar values are treated as a one-element grid.
# UNWIND: each candidate element is wrapped in [] to form unwind_mult (single-level list).
_GRID_PARAM_NAMES = [
    "THRESH",
    "TAKER_STEP",
    "MIN_L1_PERC",
    "MAX_SPREAD",
    "UNWIND",
    "UNWIND_OFFSET",
    "MIN_COUNT",
    "TIME_STOP",
    "LOSS_STOP",
    "STOP_OFFSET",
]


def _normalise_grid(names, namespace):
    """Return {name: [candidates]} for every grid param present in namespace."""
    grid = {}
    for n in names:
        if n not in namespace:
            continue
        v = namespace[n]
        grid[n] = v if isinstance(v, list) else [v]
    return grid


def _make_taker(name, market, signals, combo):
    """Instantiate a Taker from a flat combo dict."""
    unwind_val = combo["UNWIND"]
    unwind_mult_arg = unwind_val if isinstance(unwind_val, list) else [unwind_val]
    return Taker(
        name,
        market,
        mult=MULT,
        tick=TICK,
        fee=FEE,
        rebate=REBATE,
        fee_lot=FEE_LOT,
        taker_size=1,
        min_l1_depth=MIN_DEPTH,
        min_l1_perc=combo["MIN_L1_PERC"],
        unwind_mult=unwind_mult_arg,
        min_count=combo["MIN_COUNT"],
        max_pos=1,
        signals=signals,
        taker_thresh=combo["THRESH"],
        taker_step=combo["TAKER_STEP"],
        max_spread=combo["MAX_SPREAD"],
        unwind_offset=combo["UNWIND_OFFSET"],
        unwind_thresh=UNWIND_THRESH,
        hrs=HRS,
        time_stop=combo["TIME_STOP"],
        loss_stop=combo["LOSS_STOP"],
        stop_offset=combo["STOP_OFFSET"],
        max_filled_qty=MAX_OPEN * 2,
        parallel=PARALLEL,
        verbose=VERBOSE,
    )


if __name__ == "__main__":
    if _CONFIG_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "examples.legacy.taker.config_ML_v5.config_IH_MR_Liq_0_2 is required to run the legacy taker backtest."
        ) from _CONFIG_IMPORT_ERROR

    os.makedirs(OUTPUT + "plots", exist_ok=True)

    print("\nLoading signals from", SIGNAL)
    signals, date_contracts = load_signals(SIGNAL)

    # ── Build date list once (shared across all combos) ──────────────────────
    date_list = []
    for year in os.listdir(MD_PATH):
        date_list.append(pd.to_datetime(pd.Series(os.listdir(MD_PATH + year))))
    date_list = pd.concat(date_list, axis=0).drop_duplicates().sort_values()
    date_list = date_list.loc[date_list >= CUTOFF]
    date_list = date_list[date_list.dt.date.isin(date_contracts.index)]

    # ── Build parameter grid ─────────────────────────────────────────────────
    grid = _normalise_grid(_GRID_PARAM_NAMES, globals())
    param_names = list(grid.keys())
    param_values = [grid[k] for k in param_names]
    combos = list(itertools.product(*param_values))
    n_combos = len(combos)

    print(f"\nGrid search: {n_combos} combination(s)")
    for pname in param_names:
        print(f"  {pname}: {len(grid[pname])} candidate(s) = {grid[pname]}")

    # ── Run every combination ────────────────────────────────────────────────
    combo_results = []  # list of dicts: combo params + aggregate stats + raw data

    for combo_idx, combo_vals in enumerate(combos):
        combo = dict(zip(param_names, combo_vals))

        print(f"\n[{combo_idx + 1}/{n_combos}] Running combo: {combo}")

        # Fresh market per combo to avoid residual order-book state
        market = Market(mult=MULT, tick=TICK, verbose=VERBOSE)

        s1 = _make_taker("Taker_1", market, signals, combo)
        s2 = _make_taker("Taker_2", market, signals, combo)
        bt = Backtest(market, [s1, s2], MULT, TICK, PARALLEL)

        if PARALLEL:
            raw = Parallel(n_jobs=-1)(
                delayed(run_date)(bt, date.date(), date_contracts.loc[date.date()].values, MD_PATH)
                for date in date_list
            )
        else:
            raw = []
            for date in date_list:
                raw.append(run_date(bt, date.date(), date_contracts.loc[date.date()].values, MD_PATH))

        # Collect cycles
        all_cycles = []
        for day_result in raw:
            if "cycles" in day_result and day_result["cycles"]:
                all_cycles.extend(day_result["cycles"])

        # Compute total net PnL for ranking
        summary_df = pd.DataFrame(raw)
        summary_df["rebate_amt"] = summary_df["fees"] * REBATE
        total_net_pnl = (summary_df["pnl"] + summary_df["rebate_amt"]).sum()
        total_fees = summary_df["fees"].sum()
        total_fills = summary_df["fills"].sum()
        total_orders = summary_df["orders"].sum()

        # Ranking metric: restrict to BEST_PERIOD if defined, otherwise use full period
        if BEST_PERIOD is not None:
            bp_mask = (summary_df["date"] >= BEST_PERIOD[0]) & (summary_df["date"] <= BEST_PERIOD[1])
            bp_df = summary_df[bp_mask]
            rank_net_pnl = (bp_df["pnl"] + bp_df["rebate_amt"]).sum()
            rank_n_cycles = sum(
                1 for c in all_cycles if BEST_PERIOD[0] <= str(c.get("open_time", ""))[:10] <= BEST_PERIOD[1]
            )
        else:
            rank_net_pnl = total_net_pnl
            rank_n_cycles = len(all_cycles)

        print(f"  → total net PnL = {total_net_pnl:,.2f}  fills={total_fills:.0f}  orders={total_orders:.0f}")

        combo_results.append(
            {
                **combo,
                "total_net_pnl": total_net_pnl,
                "total_fees": total_fees,
                "total_fills": total_fills,
                "total_orders": total_orders,
                "n_cycles": len(all_cycles),
                "rank_net_pnl": rank_net_pnl,
                "rank_n_cycles": rank_n_cycles,
                "_raw": raw,  # kept for display_summary on winner
                "_cycles": all_cycles,
            }
        )

    # ── Combined grid results table ──────────────────────────────────────────
    display_cols = param_names + ["total_net_pnl", "total_fees", "total_fills", "n_cycles"]
    grid_df = pd.DataFrame([{k: r[k] for k in display_cols} for r in combo_results])

    # Add per-cycle PnL metric
    grid_df["total_net_pnl_per_cycle"] = grid_df["total_net_pnl"] / grid_df["n_cycles"]

    grid_df = grid_df.sort_values("total_net_pnl", ascending=False).reset_index(drop=True)
    sort_metric = "total net PnL"

    print("\n" + "=" * 80)
    print(f"GRID SEARCH RESULTS (sorted by {sort_metric})")
    print("=" * 80)
    print(grid_df.to_string())

    # Save combined grid results
    os.makedirs(OUTPUT + "summary/", exist_ok=True)
    grid_df.to_csv(OUTPUT + "summary/" + UND + "_grid_search.csv", index=True)
    print(f"\nGrid results saved to: {OUTPUT}summary/{UND}_grid_search.csv")

    # ── Best combo ───────────────────────────────────────────────────────────
    best_result = max(combo_results, key=lambda r: r["rank_net_pnl"])
    best_combo = {k: best_result[k] for k in param_names}

    print("\n" + "=" * 80)
    print("BEST COMBO")
    print("=" * 80)
    for k, v in best_combo.items():
        print(f"  {k} = {v!r}")
    print(f"  total_net_pnl = {best_result['total_net_pnl']:,.2f}")

    # ── Save best config file ────────────────────────────────────────────────
    best_dir = os.path.join(OUTPUT, "best_config")
    os.makedirs(best_dir, exist_ok=True)
    best_cfg_path = os.path.join(best_dir, f"{UND}_best_config.py")

    with open(best_cfg_path, "w") as f:
        f.write(f"# Best config from grid search — total net PnL: {best_result['total_net_pnl']:,.2f}\n")
        f.write(f"# Grid params searched: {param_names}\n\n")
        # Static instrument / path params
        f.write(f"UND = {UND!r}\n")
        f.write(f"MULT = {MULT!r}\n")
        f.write(f"TICK = {TICK!r}\n\n")
        f.write(f"SIGNAL = {SIGNAL!r}\n")
        f.write(f"OUTPUT = {OUTPUT!r}\n\n")
        # Best grid params (scalars, not lists)
        for name, val in best_combo.items():
            if name == "UNWIND":
                # Restore as single-element list (the unwind_mult convention)
                out_val = val if isinstance(val, list) else [val]
                f.write(f"UNWIND = {out_val!r}\n")
            else:
                f.write(f"{name} = {val!r}\n")
        # Non-grid params
        f.write(f"\nUNWIND_THRESH = {UNWIND_THRESH!r}\n")
        f.write(f"FEE = {FEE!r}\n")
        f.write(f"FEE_LOT = {FEE_LOT!r}\n")
        f.write(f"HRS = {HRS!r}\n")

    print(f"\nBest config saved to: {best_cfg_path}")

    # ── Full display_summary for the best combo only ─────────────────────────
    print("\nRunning display_summary for the best configuration...")
    display_summary(
        pd.DataFrame(best_result["_raw"]),
        best_result["_cycles"],
        OUTPUT,
        UND,
        MULT,
        TOD_BUCKET_MINUTES,
        REBATE,
        DIAG_WINDOW,
    )
