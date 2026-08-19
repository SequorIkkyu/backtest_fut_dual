"""Unified reporting / diagnostics for all strategies.

Generalizes the reporting that lived in `strategies/taker/utils.py` so any
strategy (single-leg taker/ladder, dual-leg pairs, arb) can use it, driven by the
unified cycle schema in `common.cycles`. Reporting is **core + opt-in**:

* CORE (always): per-day summary (with net_pnl / net_cum / session), totals +
  reconciliation, normalized cycles CSV, bucket PnL (by §5.0 `route` or
  `direction`), and a cumulative-PnL plot.
* OPT-IN (via a `diagnostics` set): `regime_split` (peak-drawdown regime
  diagnostics), `monthly_pnl`, `tod_buckets` (time-of-day PnL/qty),
  `microstructure` (entry-side depth).

`run_reports(...)` is the non-interactive entry point the grid harness calls.
Taker-specific cycle fields (`unwind_step`, `signal`, `opposite_volume`,
`volume_ratio`) are used only when present.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common.cycles import bucket_pnl, normalize_cycles_df
from common.output import (
    cycles_path,
    diagnostics_path,
    ensure_output_dirs,
    order_limit_path,
    plot_path,
    summary_log_path,
    summary_path,
)
from common.sessions import classify_session as _classify_session


def build_summary_frame(raw, rebate: float, all_cycles=None) -> pd.DataFrame:
    """Per-day summary DataFrame (date index) with rebate / net_pnl / net_cum and
    a session label inferred from cycle open times."""
    summary = pd.DataFrame(list(raw)).copy()
    # 'order_limit_rows' is a per-day list-of-dicts for the order-limit report; keep
    # it out of the scalar per-day summary frame / CSV.
    summary = summary.drop(columns=["order_limit_rows"], errors="ignore")
    summary["date"] = pd.to_datetime(summary["date"])
    summary = summary.set_index("date").sort_index()
    summary["rebate"] = summary["fees"] * rebate
    summary["net_pnl"] = summary["pnl"] + summary["rebate"]
    summary["net_cum"] = summary["net_pnl"].cumsum()

    summary["session"] = "unknown"
    cdf = pd.DataFrame(list(all_cycles)) if all_cycles else pd.DataFrame()
    if not cdf.empty and "open_time" in cdf.columns:
        cdf["open_time"] = pd.to_datetime(cdf["open_time"])
        cdf["trade_date"] = cdf["open_time"].dt.normalize()
        cdf["session"] = cdf["open_time"].dt.hour.apply(_classify_session)
        date_sessions = {}
        for trade_date, grp in cdf.groupby("trade_date"):
            sess = set(grp["session"].unique())
            sess.discard("other")
            date_sessions[trade_date] = (
                "unknown" if not sess else (next(iter(sess)) if len(sess) == 1 else "both")
            )
        summary["session"] = summary.index.map(
            lambda x: date_sessions.get(pd.Timestamp(x).normalize(), "unknown")
        )
    return summary


def _reconcile(summary: pd.DataFrame, cyc: pd.DataFrame, log) -> None:
    summary_net = summary["net_pnl"].sum()
    log(f"Total net PnL: {summary_net / 1e3:,.1f}K   fees: {summary['fees'].sum() / 1e3:,.1f}K")
    if cyc.empty:
        return
    cyc_net = cyc["net_pnl"].sum()
    diff_pct = (summary_net - cyc_net) / abs(cyc_net) * 100 if cyc_net != 0 else 0.0
    ok = "OK" if abs(diff_pct) < 1.0 else "*** WARN"
    log(f"  [recon {ok}] summary net {summary_net / 1e3:.1f}K | cycle net {cyc_net / 1e3:.1f}K | diff {diff_pct:+.2f}%")


def _bucket_report(cyc: pd.DataFrame, log) -> None:
    key = "route" if "route" in cyc.columns else "direction"
    table = bucket_pnl(cyc, key)
    if table.empty:
        return
    log(f"\n--- PnL by {key} ---")
    for _, row in table.iterrows():
        log(f"  {str(row[key]):<16} n={int(row['n']):<6} net_pnl={row['net_pnl']:>14,.1f}")


def _pairs_edge_report(cyc: pd.DataFrame, summary: pd.DataFrame, mult: int, tick, log) -> None:
    """Pairs edge decomposition (core; no-op unless the pair-specific cycle columns
    are present). Answers the design's Research Questions from the cycle telemetry:
    exit-reason mix / convergence-exit fill rate (RQ3), entry divergence captured &
    touch-join slippage (RQ1/RQ4), hedge-cross taker cost (RQ1), and inventory depth
    / cap-bind pressure (tuning hard_position_limit & inv_skew)."""
    if cyc.empty or "exit_reason" not in cyc.columns:
        return
    n = len(cyc)
    log("\n--- Pairs edge decomposition ---")

    # Exit-reason mix (RQ3: how positions actually close).
    er = (cyc.groupby("exit_reason")
          .agg(n=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
               avg_dur_s=("duration_seconds", "mean"))
          .sort_values("net_pnl", ascending=False))
    er["share"] = (er["n"] / n)
    log("  exit reason:")
    for reason, row in er.iterrows():
        log(f"    {str(reason):<14} n={int(row['n']):<6} share={row['share']:>6.1%} "
            f"net_pnl={row['net_pnl']:>14,.1f}  avg_dur={row['avg_dur_s']:>7.1f}s")
    # RQ3: of the cycles that became a hedged basis book, the fraction that reverted to the
    # band center and exited (vs trailing away to an EOD flush). Failed-hedge cycles are NOT
    # basis books (a thin touch left a naked leg) — excluded here, flagged separately below.
    revert_n = int(er.loc[er.index.isin(["exit_aggressive", "exit_passive"]), "n"].sum())
    eod_n = int(er.loc["eod_flatten", "n"]) if "eod_flatten" in er.index else 0
    if revert_n + eod_n:
        log(f"  convergence-exit fill rate: {revert_n / (revert_n + eod_n):.1%}  "
            f"({revert_n} reverted / {eod_n} EOD-flushed)")
    fh_n = int(er.loc["failed_hedge", "n"]) if "failed_hedge" in er.index else 0
    if fh_n:
        log(f"  [!] failed-hedge cycles (NAKED single leg, a thin touch left the hedge "
            f"incomplete): {fh_n} ({fh_n / n:.1%})")

    # Entry divergence captured & touch-join slippage (RQ1 / RQ4), in ticks.
    if tick and {"open_basis", "open_band_center"}.issubset(cyc.columns):
        div = (cyc["open_basis"] - cyc["open_band_center"]).abs() / tick
        log(f"  entry divergence vs band center: mean={div.mean():.2f}tk "
            f"median={div.median():.2f}tk  (slippage = realised - band edge)")
        if {"close_basis", "close_band_center"}.issubset(cyc.columns):
            rev = (cyc["close_basis"] - cyc["close_band_center"]).abs() / tick
            log(f"  exit basis vs band center:      mean={rev.mean():.2f}tk "
                f"median={rev.median():.2f}tk")

    # Hedge-cross taker cost (RQ1): does the cross eat the captured divergence?
    if "taker_cost" in cyc.columns:
        tc, gross = cyc["taker_cost"].sum(), cyc["gross_pnl"].sum()
        lots = cyc["taker_lots"].sum() if "taker_lots" in cyc.columns else 0
        free = gross + tc            # gross you'd keep if you crossed at mid (paid no spread)
        eaten = f"{tc / free:.0%}" if free else "n/a"
        per_lot = f"{tc / lots:,.1f}" if lots else "n/a"
        log(f"  taker (hedge-cross) cost: total={tc:,.0f}  per-lot={per_lot}  "
            f"~{eaten} of the would-be-free gross  ({int(lots)} lots crossed)")
        # RQ1 verdict: cost vs the EX-ANTE divergence the fade was sized to capture,
        # over HEDGE_AGGRESSIVE cycles only, aggregated total/total (mean-of-ratios
        # blows up on small/negative denominators). High => widen band_width.
        if "intended_divergence" in cyc.columns and "route" in cyc.columns:
            hedge = cyc[cyc["route"] == "HEDGE_AGGRESSIVE"]
            idiv, htc = hedge["intended_divergence"].sum(), hedge["taker_cost"].sum()
            if idiv > 0:
                log(f"  hedge-cross / intended divergence (RQ1): {htc / idiv:.1%}  "
                    f"(taker {htc:,.0f} / divergence {idiv:,.0f}, {len(hedge)} hedge cycles)")

    # Route labels are cycle-level summaries; pairs cycles may mix hedged entries,
    # HOLD adds, and residual HOLD exits. Make that composition explicit.
    comp_cols = [c for c in ("hold_lots", "hedge_lots", "passive_pair_lots", "taker_lots") if c in cyc.columns]
    if comp_cols:
        comp = {c: int(cyc[c].sum()) for c in comp_cols}
        log("  route composition lots: " + "  ".join(f"{k}={v:,}" for k, v in comp.items()))
        if {"hold_lots", "hedge_lots"}.issubset(cyc.columns):
            mixed = cyc[(cyc["hold_lots"] > 0) & (cyc["hedge_lots"] > 0)]
            if len(mixed):
                log(f"  mixed HOLD+HEDGE cycles: {len(mixed)} / {len(cyc)} "
                    f"net_pnl={mixed['net_pnl'].sum():,.1f}")

    # Inventory depth & cap-bind pressure (tune hard_position_limit / inv_skew).
    if "peak_inventory" in cyc.columns:
        pk = cyc["peak_inventory"]
        log(f"  peak inventory/cycle: mean={pk.mean():.2f} max={int(pk.max())}")
    if "manage_steps" in summary.columns and summary["manage_steps"].sum() > 0:
        frac = summary["cap_bind_steps"].sum() / summary["manage_steps"].sum()
        log(f"  cap-bind fraction (ticks the cap blocked a wanted add): {frac:.2%}")

    # Per-leg PnL split.
    if {"pnl_P", "pnl_S"}.issubset(cyc.columns):
        log(f"  per-leg PnL: P={cyc['pnl_P'].sum():,.1f}  S={cyc['pnl_S'].sum():,.1f}")


def _monthly_pnl(cyc: pd.DataFrame, output: str, und: str, log) -> None:
    time_col = "close_time" if "close_time" in cyc.columns else (
        "unwind_time" if "unwind_time" in cyc.columns else "open_time"
    )
    if cyc.empty or time_col not in cyc.columns or "direction" not in cyc.columns:
        return
    m = cyc.copy()
    m[time_col] = pd.to_datetime(m[time_col], errors="coerce")
    m = m.dropna(subset=[time_col, "direction", "net_pnl"])
    if m.empty:
        return
    m["month"] = m[time_col].dt.to_period("M").astype(str)
    table = m.groupby(["month", "direction"])["net_pnl"].sum().unstack(fill_value=0)
    table["total"] = table.sum(axis=1)
    log(f"\n--- PnL by Month (on {time_col}) ---")
    log(table.round(1).to_string())


def _tod_buckets(cyc: pd.DataFrame, mult: int, bucket_minutes: int, log) -> None:
    if cyc.empty or "open_time" not in cyc.columns:
        return
    c = cyc.copy()
    c["open_time"] = pd.to_datetime(c["open_time"])
    mins = c["open_time"].dt.hour * 60 + c["open_time"].dt.minute
    c["bucket"] = (mins // bucket_minutes) * bucket_minutes
    c["per_qty_pnl"] = c["net_pnl"] / (c["qty"].replace(0, np.nan) * mult)
    stats = c.groupby("bucket").agg(
        avg_per_qty=("per_qty_pnl", "mean"),
        count=("per_qty_pnl", "size"),
        total_pnl=("net_pnl", "sum"),
    ).round(4)
    log(f"\n--- PnL/qty by time-of-day ({bucket_minutes}-min buckets) ---")
    log(stats.to_string())


def _microstructure(cyc: pd.DataFrame, log) -> None:
    if cyc.empty or "opposite_volume" not in cyc.columns:
        return
    ov = cyc["opposite_volume"].dropna()
    if ov.empty:
        return
    log("\n--- Entry-side opposite volume ---")
    log(f"  mean={ov.mean():.1f} median={ov.median():.1f} q25={ov.quantile(.25):.1f} q75={ov.quantile(.75):.1f}")


def _plot_net_cum(summary: pd.DataFrame, output: str, und: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax1 = plt.subplots()
        for col in ("orders", "fills", "cancels"):
            if col in summary.columns:
                summary[col].plot(ax=ax1, label=col.capitalize())
        ax2 = summary["net_cum"].plot(color="red", secondary_y=True, label="Net PnL")
        ax1.legend(loc="upper left")
        ax1.grid(True)
        plt.title(f"Backtest cumulative PnL - {und}")
        plt.tight_layout()
        plt.savefig(plot_path(output, und))
        plt.close(fig)
    except Exception as exc:  # pragma: no cover - plotting is best-effort
        print(f"  (plot skipped: {exc})")


def _order_limit_report(raw, output: str, und: str, log) -> None:
    """Per-(contract, trading-day) order-message breach report (monitor-only).

    No-op when the order-limit feature is off (no rows in the per-day results).
    Writes OUTPUT/diagnostics/{UND}_order_limit.csv and logs a summary.
    """
    rows = [r for day in raw for r in day.get("order_limit_rows", [])]
    if not rows:
        return
    df = pd.DataFrame(rows)
    limit = int(df["limit"].iloc[0])
    throttled = df[df["throttled"]]
    breached = df[df["breached"]]
    log(f"\n--- Order-message limit (limit={limit}, per contract per trading day) ---")
    log(f"  contract-days tracked: {len(df)}  throttled (>=limit): {len(throttled)}  "
        f"breached (>limit): {len(breached)}  max messages: {int(df['messages'].max())}")
    if len(throttled):
        top = throttled.sort_values("messages", ascending=False).head(10)
        log("  top throttled contract-days:")
        log(top[["trading_day", "contract", "messages", "throttled", "breached"]].to_string(index=False))
    df.to_csv(order_limit_path(output, und), index=False)
    log(f"  Order-limit CSV: {order_limit_path(output, und)}")


def run_reports(
    raw,
    all_cycles,
    output: str,
    und: str,
    mult: int,
    rebate: float,
    *,
    tick: float | None = None,
    tag: str = "",
    diagnostics: frozenset[str] | set[str] = frozenset(),
    tod_bucket_minutes: int = 30,
    diag_window: int = 20,
) -> pd.DataFrame:
    """Non-interactive core + opt-in reporting. Writes CSVs/plot/log under
    `output`, returns the per-day summary frame. `diagnostics` selects opt-in
    sections (see common.config.KNOWN_DIAGNOSTICS)."""
    ensure_output_dirs(output)
    log_file = open(summary_log_path(output, und), "w", encoding="utf-8")

    def log(*args, **kwargs):
        print(*args, **kwargs)
        print(*args, file=log_file, **kwargs)

    summary = build_summary_frame(raw, rebate, all_cycles)
    cyc = normalize_cycles_df(all_cycles)

    log(f"\n=== {und} {tag} summary ({len(summary)} days) ===")
    _reconcile(summary, cyc, log)
    log(f"orders={summary.get('orders', pd.Series(dtype=float)).sum():.0f} "
        f"fills={summary.get('fills', pd.Series(dtype=float)).sum():.0f} "
        f"cancels={summary.get('cancels', pd.Series(dtype=float)).sum():.0f} "
        f"cycles={len(cyc)}")

    _bucket_report(cyc, log)
    _pairs_edge_report(cyc, summary, mult, tick, log)   # core; no-op unless pair columns present
    _order_limit_report(raw, output, und, log)   # monitor-only; no-op when feature off

    if "monthly_pnl" in diagnostics:
        _monthly_pnl(cyc, output, und, log)
    if "tod_buckets" in diagnostics:
        _tod_buckets(cyc, mult, tod_bucket_minutes, log)
    if "microstructure" in diagnostics:
        _microstructure(cyc, log)
    if "regime_split" in diagnostics and len(summary) >= 3:
        display_regime_diagnostics(summary, cyc, output, und, mult, diag_window, log_file)

    # CORE artifacts
    summary.reset_index().to_csv(summary_path(output, und), index=False)
    if not cyc.empty:
        cyc.to_csv(cycles_path(output, und), index=False)
    _plot_net_cum(summary, output, und)

    log_file.close()
    return summary


def display_regime_diagnostics(summary, cycles_df, output, und, mult, window, log_file=None):
    """Peak-drawdown regime split + per-regime daily/cycle metric comparison.

    Generalized from taker.utils: taker-only cycle fields (`unwind_step`,
    `signal`, `opposite_volume`, `volume_ratio`) are included only when present.
    Writes OUTPUT/diagnostics/{UND}_regime_diagnostics.csv.
    """
    import warnings
    warnings.filterwarnings("ignore")

    def log(*args, **kwargs):
        print(*args, **kwargs)
        if log_file is not None:
            print(*args, file=log_file, **kwargs)

    if summary.empty or "net_cum" not in summary.columns:
        return

    log("\n" + "=" * 80)
    log("REGIME DIAGNOSTICS")
    log("=" * 80)

    diag = summary.copy()
    orders = diag.get("orders")
    if orders is not None:
        diag["fill_rate"] = diag["fills"] / orders.replace(0, np.nan)
        diag["cancel_rate"] = diag["cancels"] / orders.replace(0, np.nan)
    diag["pnl_per_fill"] = diag["net_pnl"] / diag.get("fills", pd.Series(np.nan, index=diag.index)).replace(0, np.nan) / mult
    if "filled_qty" in diag.columns:
        diag["filled_qty_per_fill"] = diag["filled_qty"] / diag["fills"].replace(0, np.nan)
    diag[f"roll_pnl_{window}d"] = diag["net_pnl"].rolling(window).mean()

    cum = diag["net_cum"]
    peak_idx, peak_val, final_val = cum.idxmax(), cum.max(), cum.iloc[-1]
    drawdown_pct = (peak_val - final_val) / abs(peak_val) * 100 if peak_val != 0 else 0
    use_peak_split = (drawdown_pct > 15) and (peak_idx != cum.index[-1])

    if use_peak_split:
        diag["regime"] = "declining"
        diag.loc[diag.index <= peak_idx, "regime"] = "profitable"
        regime_labels = ["profitable", "declining"]
        log(f"\nPeak-drawdown split: peak {pd.Timestamp(peak_idx).date()} "
            f"({peak_val:,.0f}) -> final {final_val:,.0f}  (dd {drawdown_pct:.1f}%)")
    else:
        n = len(diag)
        t1, t2 = n // 3, 2 * (n // 3)
        diag["regime"] = ["early"] * t1 + ["middle"] * (t2 - t1) + ["late"] * (n - t2)
        regime_labels = ["early", "middle", "late"]
        log(f"\nEqual-thirds split (dd from peak {drawdown_pct:.1f}%).")

    compare_cols = [c for c in ("net_pnl", "orders", "fills", "cancels", "filled_qty",
                                "fill_rate", "cancel_rate", "pnl_per_fill", "filled_qty_per_fill")
                    if c in diag.columns]
    regime_compare = diag.groupby("regime")[compare_cols].mean()
    regime_compare.insert(0, "days", diag.groupby("regime").size())
    regime_compare.insert(1, "total_pnl", diag.groupby("regime")["net_pnl"].sum())
    regime_compare = regime_compare.reindex(regime_labels).round(3)
    log("\n--- Daily metrics by regime ---")
    log(regime_compare.to_string())

    if cycles_df is not None and len(cycles_df) > 0 and "open_time" in cycles_df.columns:
        cyc = cycles_df.copy()
        cyc["open_time"] = pd.to_datetime(cyc["open_time"])
        cyc["open_date"] = cyc["open_time"].dt.normalize()
        if use_peak_split:
            cyc["regime"] = np.where(cyc["open_date"] <= pd.Timestamp(peak_idx), "profitable", "declining")
        else:
            cut1, cut2 = pd.Timestamp(diag.index[t1 - 1]), pd.Timestamp(diag.index[t2 - 1])
            cyc["regime"] = "late"
            cyc.loc[cyc["open_date"] <= cut1, "regime"] = "early"
            cyc.loc[(cyc["open_date"] > cut1) & (cyc["open_date"] <= cut2), "regime"] = "middle"

        agg_spec = dict(
            count=("net_pnl", "count"),
            win_rate=("net_pnl", lambda x: round((x > 0).mean(), 3)),
            total_pnl=("net_pnl", "sum"),
            avg_pnl=("net_pnl", "mean"),
            avg_gross_pnl=("gross_pnl", "mean"),
            avg_fees=("fees", "mean"),
            avg_duration_s=("duration_seconds", "mean"),
        )
        if "unwind_step" in cyc.columns:
            agg_spec["avg_unwind_step"] = ("unwind_step", "mean")
        if "signal" in cyc.columns:
            agg_spec["avg_signal_abs"] = ("signal", lambda x: x.abs().mean())
        if "opposite_volume" in cyc.columns:
            agg_spec["avg_opp_vol"] = ("opposite_volume", "mean")
        cycle_compare = cyc.groupby("regime").agg(**agg_spec).reindex(regime_labels).round(3)
        log("\n--- Per-cycle metrics by regime ---")
        log(cycle_compare.to_string())

        daily_cyc = cyc.groupby("open_date").agg(
            cyc_count=("net_pnl", "count"),
            cyc_win_rate=("net_pnl", lambda x: (x > 0).mean()),
            cyc_avg_pnl=("net_pnl", "mean"),
            cyc_avg_dur_s=("duration_seconds", "mean"),
        )
        daily_cyc.index = pd.DatetimeIndex(daily_cyc.index)
        diag = diag.join(daily_cyc, how="left")

    path = diagnostics_path(output, und)
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    diag.to_csv(path, index=True)
    log(f"\nDiagnostics CSV: {path}")
