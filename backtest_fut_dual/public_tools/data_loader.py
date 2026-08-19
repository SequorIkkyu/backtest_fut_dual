# Load raw tick CSV files and derive normalized per-tick trade features.

import math
import os
from collections.abc import Sequence
from datetime import time

import numpy as np
import pandas as pd
import polars as pl
from numba import njit

BOOK_DEPTH = 5
DAY_SESSION_START = time(9, 0)
DAY_SESSION_END = time(15, 0)
NIGHT_SESSION_START = time(21, 0)
NIGHT_SESSION_END = time(3, 0)
PRICE_COLUMNS = tuple(
    [*(f"bidpx{level}" for level in range(BOOK_DEPTH)), *(f"askpx{level}" for level in range(BOOK_DEPTH)), "lastpx"]
)
CSV_SCHEMA_OVERRIDES = {column_name: pl.Float64 for column_name in (*PRICE_COLUMNS, "totalvalue")}
EVENT_TIME_ONLY_COLUMNS = ("exchtime", "bidpx0", "totalvol")
EVENT_TIME_SCHEMA_OVERRIDES = {"bidpx0": pl.Float64}


@njit(cache=True)
def _compute_trade_features(
    totalvol_raw: np.ndarray,
    totalvalue_raw: np.ndarray,
    mult: float,
    tick: float,
    precision_scale: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    length = totalvol_raw.shape[0]
    inc_volume = np.empty(length, dtype=np.float64)
    inc_value = np.empty(length, dtype=np.float64)
    traded = np.empty(length, dtype=np.float64)
    traded_p1 = np.empty(length, dtype=np.float64)
    traded_p2 = np.empty(length, dtype=np.float64)
    traded_v1 = np.empty(length, dtype=np.float64)
    traded_v2 = np.empty(length, dtype=np.float64)
    for idx in range(length):
        if idx == 0:
            delta_volume = totalvol_raw[idx]
            delta_value = totalvalue_raw[idx]
        else:
            delta_volume = totalvol_raw[idx] - totalvol_raw[idx - 1]
            delta_value = totalvalue_raw[idx] - totalvalue_raw[idx - 1]
        inc_volume[idx] = delta_volume
        inc_value[idx] = delta_value
        if delta_volume > 0.0:
            traded_px = delta_value / delta_volume / mult
            lower = math.floor(traded_px / tick) * tick
            upper = lower + tick
            if precision_scale > 1.0:
                lower = math.floor(lower * precision_scale + 0.5) / precision_scale
                upper = math.floor(upper * precision_scale + 0.5) / precision_scale
            upper_volume = math.floor(((traded_px - lower) * delta_volume / tick) + 0.5)
            if upper_volume < 0.0:
                upper_volume = 0.0
            elif upper_volume > delta_volume:
                upper_volume = delta_volume
            traded[idx] = traded_px
            traded_p1[idx] = lower
            traded_p2[idx] = upper
            traded_v2[idx] = upper_volume
            traded_v1[idx] = math.floor((delta_volume - upper_volume) + 0.5)
        else:
            traded[idx] = np.nan
            traded_p1[idx] = np.nan
            traded_p2[idx] = np.nan
            traded_v1[idx] = np.nan
            traded_v2[idx] = np.nan

    return inc_volume, inc_value, traded, traded_p1, traded_p2, traded_v1, traded_v2


class DataLoader:
    """Loads raw tick files with overridable cleaning and session-splitting hooks."""

    def tick_precision(self, tick: float) -> int:
        tick_text = f"{tick}".rstrip("0").rstrip(".")
        if "." not in tick_text:
            return 0
        return len(tick_text.split(".", 1)[1])

    def parse_datetime_column(self, column_name: str) -> pl.Expr:
        return pl.col(column_name).cast(pl.Utf8).str.to_datetime(strict=False, exact=False).alias(column_name)

    def fix_missing_book_levels(self, frame: pl.DataFrame) -> pl.DataFrame:
        frame = frame.with_columns(
            pl.when(pl.col("bidvol0") <= 0).then(pl.col("askpx0")).otherwise(pl.col("bidpx0")).alias("bidpx0")
        )
        for level in range(1, BOOK_DEPTH):
            prev_bid_px = f"bidpx{level - 1}"
            bid_px = f"bidpx{level}"
            bid_vol = f"bidvol{level}"
            frame = frame.with_columns(
                pl.when(pl.col(bid_vol) <= 0).then(pl.col(prev_bid_px)).otherwise(pl.col(bid_px)).alias(bid_px)
            )
        frame = frame.with_columns(
            pl.when(pl.col("askvol0") <= 0).then(pl.col("bidpx0")).otherwise(pl.col("askpx0")).alias("askpx0")
        )
        for level in range(1, BOOK_DEPTH):
            prev_ask_px = f"askpx{level - 1}"
            ask_px = f"askpx{level}"
            ask_vol = f"askvol{level}"
            frame = frame.with_columns(
                pl.when(pl.col(ask_vol) <= 0).then(pl.col(prev_ask_px)).otherwise(pl.col(ask_px)).alias(ask_px)
            )
        return frame

    def session_filter(self, session_name: str) -> pl.Expr:
        exchtime = pl.col("exchtime").dt.time()
        if session_name == "night":
            return (exchtime >= NIGHT_SESSION_START) | (exchtime <= NIGHT_SESSION_END)
        return (exchtime >= DAY_SESSION_START) & (exchtime <= DAY_SESSION_END)

    def series_to_datetime_index(self, series: pl.Series) -> pd.DatetimeIndex:
        if len(series) == 0:
            return pd.DatetimeIndex([], dtype="datetime64[ns]")
        return pd.DatetimeIndex(pd.to_datetime(series.to_list())).drop_duplicates().sort_values()

    def load_contract_session_event_times(self, file_path: str) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
        frame = pl.read_csv(
            file_path,
            columns=list(EVENT_TIME_ONLY_COLUMNS),
            try_parse_dates=True,
            infer_schema_length=10_000,
            schema_overrides=EVENT_TIME_SCHEMA_OVERRIDES,
        ).with_columns(
            self.parse_datetime_column("exchtime"),
        )

        frame = frame.unique(subset=["exchtime"], keep="last", maintain_order=True)
        frame = frame.filter((pl.col("bidpx0") > 0) & (pl.col("totalvol") > 0))

        night_session = frame.filter(self.session_filter("night"))
        day_session = frame.filter(self.session_filter("day"))
        return self.series_to_datetime_index(night_session.get_column("exchtime")), self.series_to_datetime_index(
            day_session.get_column("exchtime")
        )

    def load_contract_sessions(
        self,
        file_path: str,
        mult: int,
        tick: float,
        contract: str | None = None,
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        contract_name = contract or os.path.splitext(os.path.basename(file_path))[0]
        tick_precision = self.tick_precision(tick)
        precision_scale = float(10**tick_precision)

        frame = pl.read_csv(
            file_path,
            try_parse_dates=True,
            infer_schema_length=10_000,
            schema_overrides=CSV_SCHEMA_OVERRIDES,
        ).with_columns(
            self.parse_datetime_column("timestamp"),
            self.parse_datetime_column("exchtime"),
        )

        frame = frame.unique(subset=["exchtime"], keep="last", maintain_order=True)
        frame = frame.filter((pl.col("bidpx0") > 0) & (pl.col("totalvol") > 0))
        frame = self.fix_missing_book_levels(frame)

        raw_totalvol = frame.get_column("totalvol").cast(pl.Float64).to_numpy()
        raw_totalvalue = frame.get_column("totalvalue").cast(pl.Float64).to_numpy()
        inc_volume, inc_value, traded, traded_p1, traded_p2, traded_v1, traded_v2 = _compute_trade_features(
            raw_totalvol,
            raw_totalvalue,
            float(mult),
            float(tick),
            precision_scale,
        )

        frame = frame.with_columns(
            pl.Series("totalvol", inc_volume),
            pl.Series("totalvalue", inc_value),
            pl.Series("traded", traded),
        )

        fill_columns = [column for column in frame.columns if column != "traded"]
        frame = frame.with_columns(*(pl.col(column).fill_null(strategy="forward").alias(column) for column in fill_columns))

        frame = frame.with_columns(
            pl.Series("traded_p1", traded_p1),
            pl.Series("traded_p2", traded_p2),
            pl.Series("traded_v1", traded_v1),
            pl.Series("traded_v2", traded_v2),
            pl.lit(contract_name).alias("contract"),
            pl.col("exchtime").alias("datetime"),
        )

        night_session = frame.filter(self.session_filter("night"))
        day_session = frame.filter(self.session_filter("day"))

        return night_session, day_session

    def combine_sessions(self, session_frames: Sequence[pl.DataFrame]) -> pl.DataFrame | None:
        if not session_frames:
            return None
        return pl.concat(session_frames, how="vertical_relaxed").sort("timestamp")
