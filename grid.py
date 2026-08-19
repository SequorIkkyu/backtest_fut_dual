"""Unified grid-search harness for all strategy drivers.

Factors the scaffolding that taker, ladder, arb, and pairs each re-implemented:
build the date list from MD_PATH, expand the parameter grid, run each day in
parallel, rank combos, write the standard outputs (grid_search.csv, summary.csv,
cycles.csv, best_config.py), and invoke `common.reporting.run_reports`.

Each family keeps its own run loop by supplying two callbacks:

    make_runner(combo, signals) -> runner
        Builds a fresh market + strategy/coordinator for one parameter combo.
    run_day(runner, date, contracts, md_path) -> dict
        Runs one trading day; returns
        {date, pnl, fees, orders, fills, filled_qty, cancels, cycles}.

Dataset preparation (signals + per-date contracts + date list) is the `prepare`
callback; the default is the signal-driven loader used by taker/ladder/pairs.
Arb supplies its own (contract auto-detection).
"""

from __future__ import annotations

import datetime as _dt
import itertools
import json
import os
import subprocess

import pandas as pd
from joblib import Parallel, delayed

from common.backtest import load_signals
from common.config import resolve_diagnostics, validate_config
from common.order_limit import attach_to_market
from common.output import (
    best_config_path,
    cycles_path,
    ensure_output_dirs,
    grid_search_path,
    run_manifest_path,
    summary_path,
)
from common.reporting import run_reports


def normalise_grid(names, namespace) -> dict:
    """Each named param present in `namespace` becomes a list of candidates
    (scalars wrapped as a one-element list)."""
    grid = {}
    for name in names:
        if name not in namespace:
            continue
        value = namespace[name]
        grid[name] = value if isinstance(value, list) else [value]
    return grid


def build_date_list(md_path: str, cutoff, valid_dates) -> pd.Series:
    """Sorted trading dates present under MD_PATH/<year>/, on/after `cutoff` and
    restricted to `valid_dates` (the dates with contract metadata)."""
    parts = []
    for year in os.listdir(md_path):
        parts.append(pd.to_datetime(pd.Series(os.listdir(os.path.join(md_path, year)))))
    date_list = pd.concat(parts, axis=0).drop_duplicates().sort_values()
    if cutoff is not None:
        date_list = date_list.loc[date_list >= cutoff]
    return date_list[date_list.dt.date.isin(valid_dates)]


def signal_prepare(ns: dict):
    """Default dataset prep: load per-contract signals + date->contracts map and
    the date list (taker / ladder / pairs)."""
    if "CONTRACT_UNIVERSE" not in ns:
        raise ValueError("signal_prepare requires CONTRACT_UNIVERSE; contract inference is not supported")
    signals, date_contracts = load_signals(
        ns["SIGNAL"],
        declared_contract_universe=ns.get("CONTRACT_UNIVERSE"),
        active_contracts_by_date=ns.get("ACTIVE_CONTRACTS_BY_DATE"),
        missing_data_disposition=ns.get("SIGNAL_MISSING_DATA_DISPOSITION", "reject"),
    )
    date_list = build_date_list(ns["MD_PATH"], ns["CUTOFF"], date_contracts.index)
    return signals, date_contracts, date_list


def write_best_config(path: str, und, mult, tick, signal, output, best_combo, tag) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# Best config from {tag} grid search\n")
        fh.write(f"UND = {und!r}\nMULT = {mult!r}\nTICK = {tick!r}\n")
        if signal is not None:
            fh.write(f"SIGNAL = {signal!r}\n")
        fh.write(f"OUTPUT = {output!r}\n\n")
        for name, value in best_combo.items():
            fh.write(f"{name} = {value!r}\n")


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (pd.Timestamp, _dt.date, _dt.datetime)):
        return value.isoformat()
    return repr(value)


def _git_info():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = [os.getcwd(), os.path.join(os.getcwd(), "strategies"), os.path.join(base, "strategies")]
    for path in candidates:
        if not os.path.isdir(path):
            continue
        rev = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True)
        if rev.returncode != 0:
            continue
        status = subprocess.run(["git", "-C", path, "status", "--short"],
                                capture_output=True, text=True)
        return {
            "root": path,
            "commit": rev.stdout.strip(),
            "dirty_files": status.stdout.splitlines() if status.returncode == 0 else [],
        }
    return None


def write_run_manifest(path: str, *, ns, grid, names, combos, date_list, tag, rank_metric, diagnostics) -> None:
    config = {k: _jsonable(v) for k, v in ns.items() if k.isupper()}
    dates = pd.to_datetime(date_list) if len(date_list) else pd.Series(dtype="datetime64[ns]")
    manifest = {
        "created_at_utc": _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "tag": tag,
        "und": ns.get("UND"),
        "output": ns.get("OUTPUT"),
        "rank_metric": rank_metric or "total_net_pnl",
        "grid_param_names": names,
        "grid": _jsonable(grid),
        "combo_count": len(combos),
        "diagnostics": sorted(diagnostics),
        "date_count": int(len(date_list)),
        "date_start": None if len(dates) == 0 else pd.Timestamp(dates.min()).date().isoformat(),
        "date_end": None if len(dates) == 0 else pd.Timestamp(dates.max()).date().isoformat(),
        "config": config,
        "git": _git_info(),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _combo_net_pnl(raw, rebate, *, period=None):
    df = pd.DataFrame(raw)
    if df.empty:
        return 0.0
    df = df.copy()
    df["net_pnl"] = df["pnl"] + df["fees"] * rebate
    if period is not None:
        mask = (df["date"] >= period[0]) & (df["date"] <= period[1])
        df = df[mask]
    return float(df["net_pnl"].sum())


def _robustness_nets(cycles, total_net):
    """Concentration guards: `total_net` minus the single best cycle, and minus the best calendar day
    (grouped on each cycle's close_time). A config whose edge is one dislocation collapses on these —
    e.g. zn round-5's 63.7K headline was a single April-7 cycle (76% of net), ex-best-day < 0. Ranking
    the grid on `net_ex_bestday` instead of `total_net_pnl` rejects that fragility. Cycle `net_pnl` is
    the canonical net (sums to `total_net`), so these are exact offsets of the headline."""
    if not cycles:
        return total_net, total_net
    nets = pd.Series([c.get("net_pnl", 0.0) for c in cycles], dtype=float).reset_index(drop=True)
    ex_top1 = total_net - float(nets.max())
    days = pd.to_datetime(pd.Series([c.get("close_time") for c in cycles]), errors="coerce").dt.date
    days = days.reset_index(drop=True)
    by_day = nets.groupby(days).sum()
    ex_bestday = total_net - float(by_day.max()) if not by_day.empty else total_net
    return float(ex_top1), float(ex_bestday)


def run_grid_search(*, make_runner, run_day, grid_param_names, ns,
                    prepare=signal_prepare, tag="strategy"):
    """Run a parameter grid and write the standard outputs. Returns the list of
    per-combo result dicts (with `_raw` and `_cycles` for the best combo)."""
    md_path = ns["MD_PATH"]; output = ns["OUTPUT"]; und = ns["UND"]
    mult = ns["MULT"]; tick = ns["TICK"]; rebate = ns["REBATE"]
    parallel = ns.get("PARALLEL", True)
    best_period = ns.get("BEST_PERIOD")
    per_unit = ns.get("BEST_PER_UNIT", False)
    # Optional concentration-robust ranking: select/sort the grid by a robustness column instead of the
    # route-blind headline, so the best-config/cycle/summary artifacts describe the ROBUST winner, not a
    # single-dislocation jackpot (zn round-5). Default None = rank by total_net_pnl (unchanged for T etc.).
    rank_metric = ns.get("RANK_METRIC")
    _RANK_OK = {None, "total_net_pnl", "net_ex_bestday", "net_ex_top1cyc"}
    if rank_metric not in _RANK_OK:
        print(f"  [config] RANK_METRIC={rank_metric!r} not in {sorted(m for m in _RANK_OK if m)}; "
              f"falling back to total_net_pnl")
        rank_metric = None
    diagnostics = resolve_diagnostics(ns)
    tod = ns.get("TOD_BUCKET_MINUTES", 30)
    diag_window = ns.get("DIAG_WINDOW", 20)

    for problem in validate_config(ns):
        print(f"  [config] {problem}")

    ensure_output_dirs(output)
    print("\nPreparing dataset...")
    signals, date_contracts, date_list = prepare(ns)

    grid = normalise_grid(grid_param_names, ns)
    names = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in names]))
    print(f"Grid search: {len(combos)} combination(s)")
    for name in names:
        if len(grid[name]) > 1:
            print(f"  {name}: {grid[name]}")
    manifest = run_manifest_path(output, und, tag)
    write_run_manifest(manifest, ns=ns, grid=grid, names=names, combos=combos,
                       date_list=date_list, tag=tag, rank_metric=rank_metric,
                       diagnostics=diagnostics)
    print(f"Run manifest: {manifest}")

    # Make the order-message throttle visible (it is ENABLED by default and
    # suppresses new entries once a contract hits the limit on a trading day).
    if ns.get("ORDER_LIMIT_ENABLED", True):
        print(f"Order-message limit: ENABLED at {ns.get('ORDER_MSG_LIMIT', 4000)} per contract "
              f"per trading day (suppresses new entries; monitor-only, no PnL fee).")
    else:
        print("Order-message limit: disabled.")

    combo_results = []
    for idx, combo_vals in enumerate(combos):
        combo = dict(zip(names, combo_vals))
        print(f"\n[{idx + 1}/{len(combos)}] {combo}")
        runner = make_runner(combo, signals)
        # Attach the per-(contract, trading-day) order-message tracker to the shared
        # Market (one chokepoint counts posts/cancels/FAK; strategies throttle entries).
        attach_to_market(getattr(runner, "market", None), ns)

        def _one(date):
            return run_day(runner, date.date(), date_contracts.loc[date.date()].values, md_path)

        if parallel:
            raw = Parallel(n_jobs=-1)(delayed(_one)(d) for d in date_list)
        else:
            raw = [_one(d) for d in date_list]

        cycles = [c for r in raw for c in r.get("cycles", [])]
        total_net = _combo_net_pnl(raw, rebate)
        rank_net = _combo_net_pnl(raw, rebate, period=best_period) if best_period else total_net
        if per_unit:
            rank_net = rank_net / max(len(cycles), 1)
        print(f"  -> net PnL {total_net:,.2f}  cycles {len(cycles)}")
        # Per-combo route decomposition (cycles tagged with 'route' by the pair policies; lets a grid
        # judge a route in isolation instead of by the route-blind total). Harmless 0s otherwise.
        route_net, route_n = {}, {}
        for c in cycles:
            rt = c.get("route")
            if rt is None:
                continue
            route_net[rt] = route_net.get(rt, 0.0) + c.get("net_pnl", 0.0)
            route_n[rt] = route_n.get(rt, 0) + 1
        ex_top1, ex_bestday = _robustness_nets(cycles, total_net)
        print(f"     robustness: ex-best-day {ex_bestday:,.0f}  ex-top1-cycle {ex_top1:,.0f}")
        if rank_metric == "net_ex_bestday":
            rank_net = ex_bestday          # overrides total/period/per_unit: pick the robust winner
        elif rank_metric == "net_ex_top1cyc":
            rank_net = ex_top1
        combo_results.append({**combo, "total_net_pnl": total_net, "rank_net": rank_net,
                              "net_ex_bestday": round(ex_bestday, 1),
                              "net_ex_top1cyc": round(ex_top1, 1),
                              "n_cycles": len(cycles),
                              "hold_n": route_n.get("HOLD_DIRECTIONAL", 0),
                              "hold_net": round(route_net.get("HOLD_DIRECTIONAL", 0.0), 1),
                              "hedge_net": round(route_net.get("HEDGE_AGGRESSIVE", 0.0), 1),
                              "_raw": raw, "_cycles": cycles})

    # Grid table
    has_routes = any(r["hold_n"] or r["hedge_net"] for r in combo_results)
    cols = (names + ["total_net_pnl", "net_ex_bestday", "net_ex_top1cyc", "n_cycles"]
            + (["hold_n", "hold_net", "hedge_net"] if has_routes else []))
    grid_df = pd.DataFrame([{k: r[k] for k in cols} for r in combo_results])
    sort_col = rank_metric if rank_metric in grid_df.columns else "total_net_pnl"
    grid_df = grid_df.sort_values(sort_col, ascending=False).reset_index(drop=True)
    print("\n" + "=" * 80 + "\nGRID SEARCH RESULTS\n" + "=" * 80)
    print(grid_df.to_string())
    grid_df.to_csv(grid_search_path(output, und, tag), index=True)

    # Best combo -> outputs + reports
    best = max(combo_results, key=lambda r: r["rank_net"])
    best_combo = {k: best[k] for k in names}
    rank_note = (f"  [ranked by {rank_metric}={best.get(rank_metric):,.0f}]"
                 if rank_metric and rank_metric != "total_net_pnl" else "")
    print("\nBEST COMBO:", best_combo, f"net {best['total_net_pnl']:,.2f}{rank_note}")
    if "best_config" in diagnostics:
        write_best_config(best_config_path(output, und, tag), und, mult, tick,
                          ns.get("SIGNAL"), output, best_combo, tag)
    run_reports(best["_raw"], best["_cycles"], output, und, mult, rebate,
                tick=tick, tag=tag, diagnostics=diagnostics,
                tod_bucket_minutes=tod, diag_window=diag_window)
    return combo_results
