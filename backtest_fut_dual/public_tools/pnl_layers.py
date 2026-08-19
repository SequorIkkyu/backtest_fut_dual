# Canonical PnL layer names and core layer construction helpers.

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

    def pnl_cum_col(self, column: str) -> str:
        return f"{column}_Cum"

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
