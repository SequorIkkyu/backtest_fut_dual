"""A causally signalled, aggressive hedge-leg policy.

The foundation's declared S0 scope is passive making on the quoted product and
aggressive execution on the correlated hedge product. This policy demonstrates
the latter route; it does not claim to migrate the legacy single-leg taker
semantics.
"""

from __future__ import annotations

import math

from common.foundation_api import PolicyProposal, PolicyTrigger
from common.foundation_contracts import (
    DecisionContext,
    ExecutionModelRef,
    FoundationContractError,
    HedgePairRef,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderPricingReference,
    OrderRole,
    OrderSide,
)


class ThresholdHedgePolicy:
    """Submit one aggressive hedge when a declared score clears a threshold.

    The policy consumes only `signal_id`. That signal must provide a finite
    numeric `score` and a causally available, policy-owned `limit_price`.
    Positive scores sell the hedge product; negative scores buy it. The
    one-order guard makes the small demo deterministic, rather than modelling a
    production position-sizing or retry policy.
    """

    def __init__(
        self,
        hedge_pair: HedgePairRef,
        execution_model: ExecutionModelRef,
        *,
        signal_id: str = "taker-score",
        threshold: float = 0.5,
        quantity: int = 1,
    ) -> None:
        if not isinstance(hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(execution_model, ExecutionModelRef):
            raise FoundationContractError("execution_model must be an ExecutionModelRef")
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise FoundationContractError("signal_id must be a non-empty string")
        if not isinstance(quantity, int) or quantity <= 0:
            raise FoundationContractError("quantity must be a positive integer")
        try:
            numeric_threshold = float(threshold)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("threshold must be a finite positive number") from exc
        if not math.isfinite(numeric_threshold) or numeric_threshold <= 0:
            raise FoundationContractError("threshold must be a finite positive number")

        self._hedge_pair = hedge_pair
        self._execution_model = execution_model
        self._signal_id = signal_id
        self._threshold = numeric_threshold
        self._quantity = quantity
        self._submitted = False

    def select_signal_ids(self, available_signals):
        """Declare the sole signal this policy can consume at this decision."""
        return tuple(signal.signal_id for signal in available_signals if signal.signal_id == self._signal_id)

    def propose(self, context: DecisionContext) -> PolicyProposal:
        """Return a typed hedge declaration or an explicit no-action decision."""
        if not isinstance(context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        if context.hedge_pair != self._hedge_pair:
            raise FoundationContractError("context hedge_pair must match the policy hedge_pair")

        score, limit_price = self._signal_inputs(context)
        active = not self._submitted and score is not None and abs(score) >= self._threshold
        if not active:
            return PolicyProposal(
                MakerHedgeIntentBatch(),
                {
                    "action": "hold",
                    "signal_id": self._signal_id,
                    "signal_score": score,
                    "block_reason": "signal_unavailable_below_threshold_or_already_submitted",
                },
            )

        assert score is not None and limit_price is not None
        side = OrderSide.SELL if score > 0 else OrderSide.BUY
        trigger_id = f"{context.decision_id}:threshold-hedge"
        intent = OrderIntent(
            f"{context.decision_id}:aggressive-hedge",
            context.run_id,
            context.decision_id,
            self._hedge_pair,
            self._hedge_pair.hedge_product,
            OrderRole.HEDGE,
            side,
            self._quantity,
            limit_price,
            self._execution_model,
            pricing_reference=OrderPricingReference(
                context.exchange_batch,
                context.hedge_book.snapshot_id,
                "post_batch_snapshot_v1",
            ),
        )
        self._submitted = True
        return PolicyProposal(
            MakerHedgeIntentBatch(hedge_intent=intent),
            {
                "action": "aggressive_hedge",
                "signal_id": self._signal_id,
                "signal_score": score,
                "side": side.value,
                "limit_price": limit_price,
                "size": self._quantity,
            },
            (
                PolicyTrigger(
                    trigger_id,
                    {
                        "trigger_class": "threshold_score",
                        "signal_id": self._signal_id,
                        "score": score,
                        "threshold": self._threshold,
                        "side": side.value,
                        "fired": True,
                    },
                ),
            ),
        )

    def _signal_inputs(self, context: DecisionContext) -> tuple[float | None, float | None]:
        """Read the declared signal from immutable context-bound values only."""
        selected = tuple(signal for signal in context.consumed_signals if signal.signal_id == self._signal_id)
        if not selected:
            return None, None
        if len(selected) != 1:
            raise FoundationContractError("threshold taker policy requires exactly one selected signal")

        payload = context.signal_value(selected[0]).payload
        try:
            score = float(payload["score"])
            limit_price = float(payload["limit_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FoundationContractError(
                "threshold taker signal must provide finite numeric score and limit_price"
            ) from exc
        if not math.isfinite(score) or not math.isfinite(limit_price) or limit_price <= 0:
            raise FoundationContractError(
                "threshold taker signal must provide finite numeric score and positive limit_price"
            )
        return score, limit_price
