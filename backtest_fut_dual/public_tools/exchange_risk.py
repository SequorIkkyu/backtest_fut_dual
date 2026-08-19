# Core exchange-risk formulas for OTR and progressive message fees.

from __future__ import annotations

import math
from typing import Any


class ExchangeRiskCalculator:
    """Calculates core exchange-risk metrics with overridable steps."""

    def adjust_traded_order_count(self, traded_order_count: int | float, *, zero_as_one: bool = True) -> int:
        count = max(int(traded_order_count), 0)
        if zero_as_one and count == 0:
            return 1
        return count

    def calculate_otr(
        self,
        message_volume: int | float,
        traded_order_count: int | float,
        *,
        zero_as_one: bool = True,
    ) -> tuple[float, int]:
        message_volume_int = max(int(message_volume), 0)
        adjusted_traded_order_count = self.adjust_traded_order_count(traded_order_count, zero_as_one=zero_as_one)
        if adjusted_traded_order_count <= 0:
            return math.nan, adjusted_traded_order_count
        return message_volume_int / adjusted_traded_order_count - 1.0, adjusted_traded_order_count

    def calculate_progressive_message_fee(
        self,
        message_volume: int | float,
        otr: float,
        fee_schedule: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        otr_threshold: float,
    ) -> tuple[float, list[dict[str, Any]]]:
        message_volume_int = max(int(message_volume), 0)
        current_lower = 0
        fee_total = 0.0
        breakdown: list[dict[str, Any]] = []
        use_low_bucket = not math.isnan(otr) and otr <= otr_threshold
        rate_key = "rate_le_otr" if use_low_bucket else "rate_gt_otr"
        otr_bucket = "otr_le_threshold" if use_low_bucket else "otr_gt_threshold"

        for tier_index, tier in enumerate(fee_schedule, start=1):
            tier_upper = tier.get("up_to")
            upper_bound = math.inf if tier_upper is None else float(tier_upper)
            upper_volume = int(upper_bound) if math.isfinite(upper_bound) else message_volume_int
            tier_volume = max(0, min(message_volume_int, upper_volume) - current_lower)
            rate = float(tier.get(rate_key, 0.0))
            fee_amount = tier_volume * rate
            fee_total += fee_amount

            breakdown.append(
                {
                    "tier_index": tier_index,
                    "lower_bound": current_lower,
                    "upper_bound": None if not math.isfinite(upper_bound) else int(upper_bound),
                    "charged_volume": tier_volume,
                    "rate": rate,
                    "fee_amount": fee_amount,
                    "otr_bucket": otr_bucket,
                    "label": str(tier.get("label") or f"({current_lower}, {tier_upper}]"),
                }
            )

            current_lower = int(upper_bound) if math.isfinite(upper_bound) else message_volume_int
            if current_lower >= message_volume_int:
                break

        return fee_total, breakdown
