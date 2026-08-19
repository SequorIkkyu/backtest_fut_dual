"""Cycle-normalization tests for common/cycles.py — the schema bridge that lets
taker/ladder and pairs cycles feed the shared reporting/diagnostics."""

from __future__ import annotations

import pandas as pd

from common.cycles import bucket_pnl, normalize_cycles_df


def test_empty_returns_empty_frame():
    assert normalize_cycles_df([]).empty


def test_taker_schema_core_fields():
    cyc = [dict(open_time="2025-01-02 09:00:00", unwind_time="2025-01-02 09:01:00",
                direction="long", qty=1, gross_pnl=60.0, fees=6.0, rebate=0.6, net_pnl=54.6)]
    df = normalize_cycles_df(cyc)
    core = {"cycle_id", "open_time", "close_time", "direction", "qty",
            "gross_pnl", "fees", "rebate", "net_pnl", "duration_seconds"}
    assert core.issubset(df.columns)
    # close_time derived from taker's unwind_time (passed through as-is, not coerced)
    assert pd.to_datetime(df.iloc[0]["close_time"]) == pd.Timestamp("2025-01-02 09:01:00")
    assert df.iloc[0]["duration_seconds"] == 60.0


def test_pairs_schema_maps_entry_dir_and_pnl():
    cyc = [dict(open_time="2025-01-02 21:00:00", close_time="2025-01-02 21:02:00",
                entry_dir="long_basis", route="HEDGE_PASSIVE", pnl=42.0)]
    df = normalize_cycles_df(cyc)
    row = df.iloc[0]
    assert row["direction"] == "long_basis"     # from entry_dir
    assert row["net_pnl"] == 42.0               # from pnl
    assert row["duration_seconds"] == 120.0
    assert row["qty"] == 1                       # default


def test_net_pnl_derived_from_gross_when_absent():
    cyc = [dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01",
                gross_pnl=100.0, fees=10.0, rebate=1.0)]
    df = normalize_cycles_df(cyc)
    assert df.iloc[0]["net_pnl"] == 91.0         # gross - fees + rebate


def test_direction_defaults_to_na():
    cyc = [dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01", pnl=1.0)]
    assert normalize_cycles_df(cyc).iloc[0]["direction"] == "na"


def test_cycle_id_assigned_when_absent():
    cyc = [dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01", pnl=1.0),
           dict(open_time="2025-01-02 09:02", close_time="2025-01-02 09:03", pnl=2.0)]
    assert list(normalize_cycles_df(cyc)["cycle_id"]) == [0, 1]


# ── bucket_pnl ─────────────────────────────────────────────────────────────────
def test_bucket_pnl_by_route():
    cyc = [dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01", route="A", pnl=10.0),
           dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01", route="A", pnl=5.0),
           dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01", route="B", pnl=-3.0)]
    b = bucket_pnl(cyc, "route").set_index("route")
    assert b.loc["A", "n"] == 2 and b.loc["A", "net_pnl"] == 15.0
    assert b.loc["B", "n"] == 1 and b.loc["B", "net_pnl"] == -3.0


def test_bucket_pnl_missing_key_returns_empty():
    cyc = [dict(open_time="2025-01-02 09:00", close_time="2025-01-02 09:01", pnl=1.0)]
    assert bucket_pnl(cyc, "route").empty
