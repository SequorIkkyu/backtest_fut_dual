# Canonical PnL layer names + construction/enrichment helpers (production base).
#
# Self-contained copy for the production `common/` base, decoupled from the
# `common/public_tools/` experiment. Provides the column constants, the layer
# builder, and the two module-level helpers `common/strategy.py` imports
# (`build_pnl_layer_dict`, `enrich_strategy_pnl_frame`). The enrichment here is
# dependency-free: message fee defaults to 0 unless a message-fee column is
# already present (the exchange-risk reconstruction lives only in the experiment).

from __future__ import annotations

import numpy as np
import pandas as pd

PNL_GROSS_COL = "PNL_Gross"
PNL_AFT_TRADE_FEE_COL = "PNL_Aft_TradeFee"
PNL_AFT_TRADE_FEE_MSG_COL = "PNL_Aft_TradeFee_Msg"
PNL_AFT_TRADE_FEE_PLUS_REBATE_COL = "PNL_Aft_TradeFee_PlusRebate"
PNL_NET_COL = "PNL_Net"

TRADE_FEE_COL = "TradeFee"
TRADE_FEE_REBATE_COL = "TradeFeeRebate"
MESSAGE_FEE_COL = "MessageFee"
TRADE_FEE_DRAG_RATIO_COL = "TradeFeeDragRatio"
MESSAGE_FEE_DRAG_RATIO_COL = "MessageFeeDragRatio"

PNL_LAYER_COLUMNS = (
    PNL_GROSS_COL,
    PNL_AFT_TRADE_FEE_COL,
    PNL_AFT_TRADE_FEE_MSG_COL,
    PNL_AFT_TRADE_FEE_PLUS_REBATE_COL,
    PNL_NET_COL,
)


class PnLLayerBuilder:
    """Builds canonical PnL layers with overridable conversion and ratio hooks."""

    def coerce_series(self, index: pd.Index, value: object) -> pd.Series:
        if isinstance(value, pd.Series):
            series = value.astype(float)
            if not series.index.equals(index):
                series = series.reindex(index)
            return series.astype(float)
        if isinstance(value, pd.Index):
            return pd.Series(value.to_numpy(dtype=float), index=index, dtype=float)
        if isinstance(value, np.ndarray):
            return pd.Series(value.astype(float), index=index, dtype=float)
        if isinstance(value, (list, tuple)):
            return pd.Series(list(value), index=index, dtype=float)
        scalar = 0.0 if value is None else float(value)  # type: ignore
        return pd.Series(scalar, index=index, dtype=float)

    def safe_positive_ratio(self, numerator: pd.Series, denominator: pd.Series) -> pd.Series:
        ratio = pd.Series(np.nan, index=denominator.index, dtype=float)
        positive_mask = denominator > 0
        ratio.loc[positive_mask] = numerator.loc[positive_mask] / denominator.loc[positive_mask]
        return ratio

    def build_frame(
        self,
        *,
        index: pd.Index,
        pnl_gross: object,
        pnl_aft_trade_fee: object,
        trade_fee: object,
        trade_fee_rebate: object = 0.0,
        message_fee: object = 0.0,
    ) -> pd.DataFrame:
        gross_series = self.coerce_series(index, pnl_gross)
        after_trade_fee_series = self.coerce_series(index, pnl_aft_trade_fee)
        trade_fee_series = self.coerce_series(index, trade_fee)
        rebate_series = self.coerce_series(index, trade_fee_rebate)
        message_fee_series = self.coerce_series(index, message_fee)

        frame = pd.DataFrame(index=index)
        frame[PNL_GROSS_COL] = gross_series
        frame[PNL_AFT_TRADE_FEE_COL] = after_trade_fee_series
        frame[PNL_AFT_TRADE_FEE_MSG_COL] = after_trade_fee_series - message_fee_series
        frame[PNL_AFT_TRADE_FEE_PLUS_REBATE_COL] = after_trade_fee_series + rebate_series
        frame[PNL_NET_COL] = after_trade_fee_series + rebate_series - message_fee_series
        frame[TRADE_FEE_COL] = trade_fee_series
        frame[TRADE_FEE_REBATE_COL] = rebate_series
        frame[MESSAGE_FEE_COL] = message_fee_series
        frame[TRADE_FEE_DRAG_RATIO_COL] = self.safe_positive_ratio(frame[TRADE_FEE_COL], frame[PNL_GROSS_COL])
        frame[MESSAGE_FEE_DRAG_RATIO_COL] = self.safe_positive_ratio(frame[MESSAGE_FEE_COL], frame[PNL_GROSS_COL])
        return frame

    def build_dict(
        self,
        *,
        pnl_gross: float,
        pnl_aft_trade_fee: float,
        trade_fee: float,
        trade_fee_rebate: float = 0.0,
        message_fee: float = 0.0,
    ) -> dict[str, float]:
        gross_value = float(pnl_gross)
        after_trade_fee_value = float(pnl_aft_trade_fee)
        trade_fee_value = float(trade_fee)
        trade_fee_rebate_value = float(trade_fee_rebate)
        message_fee_value = float(message_fee)
        pnl_aft_trade_fee_msg_value = after_trade_fee_value - message_fee_value
        pnl_aft_trade_fee_plus_rebate_value = after_trade_fee_value + trade_fee_rebate_value
        pnl_net_value = pnl_aft_trade_fee_plus_rebate_value - message_fee_value

        if gross_value > 0.0:
            trade_fee_drag_ratio_value = trade_fee_value / gross_value
            message_fee_drag_ratio_value = message_fee_value / gross_value
        else:
            trade_fee_drag_ratio_value = float("nan")
            message_fee_drag_ratio_value = float("nan")

        return {
            PNL_GROSS_COL: gross_value,
            PNL_AFT_TRADE_FEE_COL: after_trade_fee_value,
            PNL_AFT_TRADE_FEE_MSG_COL: pnl_aft_trade_fee_msg_value,
            PNL_AFT_TRADE_FEE_PLUS_REBATE_COL: pnl_aft_trade_fee_plus_rebate_value,
            PNL_NET_COL: pnl_net_value,
            TRADE_FEE_COL: trade_fee_value,
            TRADE_FEE_REBATE_COL: trade_fee_rebate_value,
            MESSAGE_FEE_COL: message_fee_value,
            TRADE_FEE_DRAG_RATIO_COL: trade_fee_drag_ratio_value,
            MESSAGE_FEE_DRAG_RATIO_COL: message_fee_drag_ratio_value,
        }


_PNL_LAYER_BUILDER = PnLLayerBuilder()
build_pnl_layer_dict = _PNL_LAYER_BUILDER.build_dict
build_pnl_layer_frame = _PNL_LAYER_BUILDER.build_frame

# Legacy lower-case PnL columns superseded by the canonical PNL_* layers.
LEGACY_PNL_COLUMNS = (
    "gross_pnl", "pnl", "fees", "fee_drag", "fee_drag_ratio", "rebate", "rebate_pnl",
    "net_pnl", "net", "gross_cum", "pnl_cum", "net_cum",
    "trade_fee", "trade_fee_rebate", "message_fee", "message_fee_cum",
    "trade_fee_drag_ratio", "message_fee_drag_ratio",
)


def drop_legacy_pnl_columns(frame: pd.DataFrame) -> pd.DataFrame:
    drop_columns = [column for column in LEGACY_PNL_COLUMNS if column in frame.columns]
    return frame.drop(columns=drop_columns) if drop_columns else frame


def _first_existing_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for column in candidates:
        if column in frame.columns:
            return column
    return None


def enrich_strategy_pnl_frame(
    frame: pd.DataFrame,
    *,
    events: pd.DataFrame | None = None,           # accepted for API parity; unused here
    exchange_risk_config: dict | None = None,     # accepted for API parity; unused here
    gross_col_candidates: tuple[str, ...] = (PNL_GROSS_COL, "gross_pnl", "gross_pnl_before_all_fees"),
    pnl_col_candidates: tuple[str, ...] = (PNL_AFT_TRADE_FEE_COL, "pnl", "pnl_after_trade_fee"),
    trade_fee_col_candidates: tuple[str, ...] = (TRADE_FEE_COL, "fee_drag", "fees", "trade_fee"),
    rebate_col_candidates: tuple[str, ...] = (TRADE_FEE_REBATE_COL, "trade_fee_rebate", "rebate_pnl", "rebate"),
    message_fee_col_candidates: tuple[str, ...] = (MESSAGE_FEE_COL, "message_fee", "risk_total_message_fee"),
    drop_legacy_columns: bool = False,
) -> pd.DataFrame:
    """Add the canonical PNL_* layer columns to a strategy session record frame,
    detecting source columns by candidate name. Message fee defaults to 0 when no
    message-fee column is present (production base carries no message-fee model)."""
    if frame.empty:
        return frame.copy()

    enriched = frame.copy()
    if "datetime" in enriched.columns:
        enriched["datetime"] = pd.to_datetime(enriched["datetime"])

    pnl_col = _first_existing_column(enriched, pnl_col_candidates)
    if pnl_col is None:
        raise KeyError("Expected a pnl column in frame to build explicit PnL layers.")

    trade_fee_col = _first_existing_column(enriched, trade_fee_col_candidates)
    trade_fee_series = (
        enriched[trade_fee_col].astype(float)
        if trade_fee_col is not None
        else pd.Series(0.0, index=enriched.index, dtype=float)
    )

    gross_col = _first_existing_column(enriched, gross_col_candidates)
    gross_series = (
        enriched[gross_col].astype(float)
        if gross_col is not None
        else enriched[pnl_col].astype(float) + trade_fee_series
    )

    rebate_col = _first_existing_column(enriched, rebate_col_candidates)
    rebate_series = (
        enriched[rebate_col].astype(float)
        if rebate_col is not None
        else pd.Series(0.0, index=enriched.index, dtype=float)
    )

    message_fee_col = _first_existing_column(enriched, message_fee_col_candidates)
    message_fee_series = (
        enriched[message_fee_col].astype(float)
        if message_fee_col is not None
        else pd.Series(0.0, index=enriched.index, dtype=float)
    )

    layers = build_pnl_layer_frame(
        index=enriched.index,
        pnl_gross=gross_series,
        pnl_aft_trade_fee=enriched[pnl_col].astype(float),
        trade_fee=trade_fee_series,
        trade_fee_rebate=rebate_series,
        message_fee=message_fee_series,
    )
    for column in layers.columns:
        enriched[column] = layers[column]

    if drop_legacy_columns:
        enriched = drop_legacy_pnl_columns(enriched)
    return enriched
