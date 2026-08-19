"""Unified cycle schema for all strategies.

Every strategy records completed trades ("cycles") with different native keys —
the taker/ladder single-leg cycle (`open_px, unwind_px, qty, signal,
opposite_volume, unwind_step, ...`) vs. the pairs basis cycle (`open_time,
close_time, route, entry_dir, open_basis, close_basis, pnl`). This module defines
the **core** schema the shared reporting/diagnostics rely on and a normalizer that
fills missing core columns from whatever a strategy provided, leaving all extra
(strategy-specific) columns untouched.
"""

from __future__ import annotations

import pandas as pd

# Core fields the shared reporting layer can always rely on.
CORE_CYCLE_FIELDS = (
    "cycle_id", "open_time", "close_time", "direction", "qty",
    "gross_pnl", "fees", "rebate", "net_pnl", "duration_seconds",
)


def normalize_cycles_df(cycles) -> pd.DataFrame:
    """Return a DataFrame guaranteeing the core cycle fields.

    Accepts a list[dict] or a DataFrame. Missing core columns are derived where
    possible (net_pnl from pnl or gross−fees+rebate; duration from open/close
    times; direction from entry_dir; qty defaults to 1). Extra columns pass
    through so opt-in diagnostics can still use strategy-specific fields.
    """
    df = cycles.copy() if isinstance(cycles, pd.DataFrame) else pd.DataFrame(list(cycles or []))
    if df.empty:
        return df

    # direction: taker uses 'direction' ('long'/'short'); pairs uses 'entry_dir'
    # ('long_basis'/'short_basis'). Keep whichever exists.
    if "direction" not in df.columns and "entry_dir" in df.columns:
        df["direction"] = df["entry_dir"]
    if "direction" not in df.columns:
        df["direction"] = "na"

    # fees / rebate default to 0 when a strategy doesn't split them out.
    for col in ("fees", "rebate"):
        if col not in df.columns:
            df[col] = 0.0

    # timestamps: guarantee open_time / close_time (taker uses unwind_time).
    if "open_time" not in df.columns:
        df["open_time"] = pd.NaT
    if "close_time" not in df.columns:
        df["close_time"] = df["unwind_time"] if "unwind_time" in df.columns else df["open_time"]

    # net_pnl: explicit > 'pnl' alias > gross − fees + rebate.
    if "net_pnl" not in df.columns:
        if "pnl" in df.columns:
            df["net_pnl"] = df["pnl"]
        elif "gross_pnl" in df.columns:
            df["net_pnl"] = df["gross_pnl"] - df["fees"] + df["rebate"]
        else:
            df["net_pnl"] = 0.0

    # gross_pnl fallback: net + fees − rebate (so gross ≥ net under positive fees).
    if "gross_pnl" not in df.columns:
        df["gross_pnl"] = df["net_pnl"] + df["fees"] - df["rebate"]

    # duration_seconds from open/close timestamps.
    if "duration_seconds" not in df.columns:
        df["duration_seconds"] = (
            pd.to_datetime(df["close_time"]) - pd.to_datetime(df["open_time"])
        ).dt.total_seconds()

    if "qty" not in df.columns:
        df["qty"] = 1
    if "cycle_id" not in df.columns:
        df["cycle_id"] = range(len(df))

    return df


def bucket_pnl(cycles, key: str = "route") -> pd.DataFrame:
    """Per-bucket trade count and net PnL (e.g. by §5.0 `route` or `direction`).

    Returns an empty DataFrame when the bucket key is absent.
    """
    df = normalize_cycles_df(cycles)
    if df.empty or key not in df.columns:
        return pd.DataFrame(columns=[key, "n", "net_pnl"])
    grouped = (
        df.groupby(key)
        .agg(n=("net_pnl", "size"), net_pnl=("net_pnl", "sum"))
        .reset_index()
        .sort_values("net_pnl", ascending=False)
    )
    return grouped
