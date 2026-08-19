"""Foundation-native causal passive matching for verified maker fills.

This service deliberately reimplements the validated FIFO queue mechanics without
calling legacy Market or Strategy surfaces.  It accepts only registered maker
intents and causally available aggressor trades, then produces opaque
PassiveFillEvidence records that the public foundation API can verify.
"""

from __future__ import annotations

import math
from datetime import datetime
from dataclasses import dataclass

from common.foundation_contracts import (
    BookSnapshotRef,
    DecisionContext,
    FoundationContractError,
    OrderIntent,
    OrderRole,
    OrderSide,
    PassiveFillEvidence,
    PassiveTrade,
    SnapshotInterval,
    SnapshotIntervalQueueProxyEvidence,
)


@dataclass
class _RestingMaker:
    intent: OrderIntent
    context: DecisionContext
    queue_ahead_submit: int
    queue_ahead: int
    arrived_at: datetime
    arrival_sequence: int
    cumulative_fill_qty: int = 0

    @property
    def remaining_qty(self) -> int:
        return self.intent.requested_qty - self.cumulative_fill_qty


class PassiveMatchingService:
    """Derive maker fills from registered orders and causally replayed trades."""

    def __init__(self, authority_token: object) -> None:
        if authority_token is None:
            raise FoundationContractError("passive matcher requires an opaque authority token")
        self._authority_token = authority_token
        self._orders: dict[str, _RestingMaker] = {}
        self._issued: dict[str, PassiveFillEvidence] = {}
        self._issued_snapshot_proxy: dict[str, SnapshotIntervalQueueProxyEvidence] = {}
        self._recorded_fill_ids: set[str] = set()
        self._seen_trade_references: set[str] = set()
        self._seen_snapshot_intervals: set[str] = set()
        self._arrival_sequence = 0

    def register_intent(
        self,
        intent: OrderIntent,
        context: DecisionContext,
        *,
        queue_ahead_submit: int,
        arrival_book_snapshot: BookSnapshotRef,
        arrived_at: datetime | None = None,
    ) -> None:
        """Register an arrived maker order with its conservative queue proxy."""
        if not isinstance(intent, OrderIntent) or intent.role is not OrderRole.MAKER:
            raise FoundationContractError("passive matcher accepts only maker OrderIntent values")
        if not isinstance(context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        if intent.run_id != context.run_id or intent.decision_id != context.decision_id or intent.hedge_pair != context.hedge_pair:
            raise FoundationContractError("maker intent must match its decision context")
        if not isinstance(arrival_book_snapshot, BookSnapshotRef):
            raise FoundationContractError("arrival_book_snapshot must be a BookSnapshotRef")
        if arrival_book_snapshot.product != intent.product:
            raise FoundationContractError("arrival book snapshot product must match maker intent product")
        if not isinstance(queue_ahead_submit, int) or queue_ahead_submit < 0:
            raise FoundationContractError("queue_ahead_submit must be a non-negative integer")
        if arrived_at is None:
            arrived_at = _book_visible_at(arrival_book_snapshot)
        if not isinstance(arrived_at, datetime) or arrived_at.tzinfo is None or arrived_at.utcoffset() is None:
            raise FoundationContractError("arrived_at must be a timezone-aware datetime")
        if arrived_at < _book_visible_at(arrival_book_snapshot):
            raise FoundationContractError("arrived_at must not precede its arrival book availability")
        if intent.intent_id in self._orders:
            raise FoundationContractError("maker intent is already registered with passive matcher")
        own_orders_ahead = sum(
            state.remaining_qty
            for state in self._orders.values()
            if state.intent.product == intent.product
            and state.intent.side is intent.side
            and math.isclose(state.intent.limit_price, intent.limit_price, rel_tol=0.0, abs_tol=1e-9)
        )
        effective_queue = queue_ahead_submit + own_orders_ahead
        self._arrival_sequence += 1
        self._orders[intent.intent_id] = _RestingMaker(
            intent,
            context,
            effective_queue,
            effective_queue,
            arrived_at,
            self._arrival_sequence,
        )

    def retire_intent(self, intent_id: str) -> None:
        """Stop a terminal/cancelled maker order from receiving later matches."""
        self._orders.pop(intent_id, None)

    def match_trade(
        self, trade: PassiveTrade, *, fee_rebate_per_contract: float = 0.0
    ) -> tuple[PassiveFillEvidence, ...]:
        """Advance matching queues and return every fill derived from one trade."""
        if not isinstance(trade, PassiveTrade):
            raise FoundationContractError("trade must be a PassiveTrade")
        try:
            fee_rebate_per_contract = float(fee_rebate_per_contract)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("fee_rebate_per_contract must be finite") from exc
        if not math.isfinite(fee_rebate_per_contract):
            raise FoundationContractError("fee_rebate_per_contract must be finite")

        if trade.trade_reference in self._seen_trade_references:
            raise FoundationContractError("passive trade has already been matched in this run")
        self._seen_trade_references.add(trade.trade_reference)

        eligible = tuple(
            state
            for state in self._orders.values()
            if state.remaining_qty > 0
            and trade.trade_ts >= state.arrived_at
            and _trade_can_reach_order(trade, state.intent)
        )
        by_price: dict[float, list[_RestingMaker]] = {}
        for state in eligible:
            by_price.setdefault(state.intent.limit_price, []).append(state)

        # A sell aggressor consumes resting bids from high to low. A buy
        # aggressor consumes resting offers from low to high. Arrival order
        # determines priority within each price level.
        prices = sorted(by_price, reverse=trade.taker_side is OrderSide.SELL)
        remaining_trade_qty = trade.quantity
        fills: list[PassiveFillEvidence] = []
        for price in prices:
            if remaining_trade_qty <= 0:
                break
            states = sorted(by_price[price], key=lambda state: (state.arrived_at, state.arrival_sequence))
            quantity_reaching_level = remaining_trade_qty
            queue_before = {state.intent.intent_id: state.queue_ahead for state in states}

            # Every order at a reached level sees that print advance the queue
            # ahead of it, including later same-price orders whose own-order
            # queue is consumed by an earlier fill below.
            for state in states:
                state.queue_ahead = max(0, state.queue_ahead - quantity_reaching_level)

            quantity_consumed_at_level = 0
            for state in states:
                if remaining_trade_qty <= 0:
                    break
                intent = state.intent
                effective_queue_ahead = max(0, queue_before[intent.intent_id] - quantity_consumed_at_level)
                queue_consumed = min(remaining_trade_qty, effective_queue_ahead)
                remaining_trade_qty -= queue_consumed
                quantity_consumed_at_level += queue_consumed
                fill_qty = min(state.remaining_qty, remaining_trade_qty)
                if fill_qty <= 0:
                    continue
                remaining_trade_qty -= fill_qty
                quantity_consumed_at_level += fill_qty
                state.cumulative_fill_qty += fill_qty
                fill_id = f"{trade.run_id}:{intent.intent_id}:{trade.trade_id}:{state.cumulative_fill_qty}"
                evidence = PassiveFillEvidence(
                    fill_id,
                    trade.trade_id,
                    trade.source_event_key,
                    trade.quantity,
                    intent.intent_id,
                    intent.run_id,
                    intent.decision_id,
                    intent.hedge_pair,
                    intent.product,
                    intent.side,
                    trade.trade_ts,
                    trade.feed_seq,
                    trade.book_snapshot,
                    intent.limit_price,
                    fill_qty,
                    state.cumulative_fill_qty,
                    state.queue_ahead_submit,
                    state.queue_ahead,
                    fee_rebate_per_contract * fill_qty,
                    self._authority_token,
                )
                self._issued[evidence.fill_id] = evidence
                fills.append(evidence)
        return tuple(fills)

    def match_snapshot_interval(
        self, interval: SnapshotInterval, *, fee_rebate_per_contract: float = 0.0
    ) -> tuple[SnapshotIntervalQueueProxyEvidence, ...]:
        """Advance queues from one declared snapshot interval without inventing a trade.

        ``bid_then_ask_v1`` is intentionally explicit: a bucket first reaches
        eligible resting bids from high to low, then eligible resting asks from
        low to high, with one shared bucket budget.  It is a deterministic
        price-reach convention, not an observed aggressor-side assertion.
        """
        if not isinstance(interval, SnapshotInterval):
            raise FoundationContractError("interval must be a SnapshotInterval")
        try:
            fee_rebate_per_contract = float(fee_rebate_per_contract)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("fee_rebate_per_contract must be finite") from exc
        if not math.isfinite(fee_rebate_per_contract):
            raise FoundationContractError("fee_rebate_per_contract must be finite")
        if interval.price_reach_rule != "bid_then_ask_v1":
            raise FoundationContractError("unsupported snapshot interval price_reach_rule")

        interval_key = f"{interval.raw_file_hash}:{interval.interval_id}"
        if interval_key in self._seen_snapshot_intervals:
            raise FoundationContractError("snapshot interval has already been matched in this run")
        self._seen_snapshot_intervals.add(interval_key)

        fills: list[SnapshotIntervalQueueProxyEvidence] = []
        for bucket_index, bucket in enumerate(interval.buckets):
            remaining_bucket_qty = bucket.quantity
            for states in self._interval_price_groups(interval, bucket.price):
                if remaining_bucket_qty <= 0:
                    break
                queue_before = {state.intent.intent_id: state.queue_ahead for state in states}
                for state in states:
                    state.queue_ahead = max(0, state.queue_ahead - remaining_bucket_qty)
                quantity_consumed_at_level = 0
                for state in states:
                    if remaining_bucket_qty <= 0:
                        break
                    intent = state.intent
                    effective_queue_ahead = max(0, queue_before[intent.intent_id] - quantity_consumed_at_level)
                    queue_consumed = min(remaining_bucket_qty, effective_queue_ahead)
                    remaining_bucket_qty -= queue_consumed
                    quantity_consumed_at_level += queue_consumed
                    fill_qty = min(state.remaining_qty, remaining_bucket_qty)
                    if fill_qty <= 0:
                        continue
                    remaining_bucket_qty -= fill_qty
                    quantity_consumed_at_level += fill_qty
                    state.cumulative_fill_qty += fill_qty
                    fill_id = (
                        f"{interval.run_id}:{intent.intent_id}:{interval.interval_id}:"
                        f"bucket:{bucket_index}:{state.cumulative_fill_qty}"
                    )
                    evidence = SnapshotIntervalQueueProxyEvidence(
                        fill_id,
                        interval.interval_id,
                        interval.raw_file_id,
                        interval.raw_file_hash,
                        interval.raw_row_ordinal,
                        interval.model_version,
                        interval.price_reach_rule,
                        interval.availability_convention,
                        interval.quantity,
                        bucket_index,
                        bucket.price,
                        bucket.quantity,
                        intent.intent_id,
                        intent.run_id,
                        intent.decision_id,
                        intent.hedge_pair,
                        intent.product,
                        intent.side,
                        interval.interval_ts,
                        interval.feed_seq,
                        interval.book_snapshot,
                        intent.limit_price,
                        fill_qty,
                        state.cumulative_fill_qty,
                        state.queue_ahead_submit,
                        state.queue_ahead,
                        fee_rebate_per_contract * fill_qty,
                        self._authority_token,
                    )
                    self._issued_snapshot_proxy[evidence.fill_id] = evidence
                    fills.append(evidence)
        return tuple(fills)

    def _interval_price_groups(self, interval: SnapshotInterval, bucket_price: float) -> tuple[list[_RestingMaker], ...]:
        """Return price-priority groups under the declared side-neutral rule."""
        eligible_by_side: dict[OrderSide, dict[float, list[_RestingMaker]]] = {
            OrderSide.BUY: {},
            OrderSide.SELL: {},
        }
        for state in self._orders.values():
            if state.remaining_qty <= 0 or interval.interval_ts < state.arrived_at:
                continue
            intent = state.intent
            if (
                intent.run_id != interval.run_id
                or intent.hedge_pair != interval.hedge_pair
                or intent.product != interval.product
            ):
                continue
            reachable = (
                intent.side is OrderSide.BUY and bucket_price <= intent.limit_price
            ) or (
                intent.side is OrderSide.SELL and bucket_price >= intent.limit_price
            )
            if reachable:
                eligible_by_side[intent.side].setdefault(intent.limit_price, []).append(state)

        groups: list[list[_RestingMaker]] = []
        for side, reverse in ((OrderSide.BUY, True), (OrderSide.SELL, False)):
            for price in sorted(eligible_by_side[side], reverse=reverse):
                groups.append(
                    sorted(
                        eligible_by_side[side][price],
                        key=lambda state: (state.arrived_at, state.arrival_sequence),
                    )
                )
        return tuple(groups)

    def validate_evidence(self, evidence: PassiveFillEvidence) -> None:
        """Reject a forged, unknown, changed, or already-recorded match result."""
        if not isinstance(evidence, PassiveFillEvidence):
            raise FoundationContractError("evidence must be a PassiveFillEvidence")
        issued = self._issued.get(evidence.fill_id)
        if (
            issued is None
            or issued != evidence
            or evidence._authority_token is not self._authority_token
            or evidence.fill_id in self._recorded_fill_ids
        ):
            raise FoundationContractError("passive fill evidence was not issued by this matcher")

    def record_evidence(self, evidence: PassiveFillEvidence) -> None:
        self.validate_evidence(evidence)
        self._recorded_fill_ids.add(evidence.fill_id)

    def validate_snapshot_proxy_evidence(self, evidence: SnapshotIntervalQueueProxyEvidence) -> None:
        """Reject a forged, unknown, changed, or already-recorded proxy result."""
        if not isinstance(evidence, SnapshotIntervalQueueProxyEvidence):
            raise FoundationContractError("evidence must be a SnapshotIntervalQueueProxyEvidence")
        issued = self._issued_snapshot_proxy.get(evidence.fill_id)
        if (
            issued is None
            or issued != evidence
            or evidence._authority_token is not self._authority_token
            or evidence.fill_id in self._recorded_fill_ids
        ):
            raise FoundationContractError("snapshot proxy fill evidence was not issued by this matcher")

    def record_snapshot_proxy_evidence(self, evidence: SnapshotIntervalQueueProxyEvidence) -> None:
        self.validate_snapshot_proxy_evidence(evidence)
        self._recorded_fill_ids.add(evidence.fill_id)


def _book_visible_at(snapshot: BookSnapshotRef) -> datetime:
    """Return the causal clock for a retained book snapshot.

    Exchange-batch snapshots become visible as one atomic market state at their
    exchange timestamp.  Untagged compatibility snapshots retain their
    explicit availability timestamp.
    """
    if snapshot.exchange_batch is not None:
        return snapshot.exchange_batch.exchange_ts
    return snapshot.available_at


def _trade_can_reach_order(trade: PassiveTrade, intent: OrderIntent) -> bool:
    if trade.run_id != intent.run_id or trade.hedge_pair != intent.hedge_pair or trade.product != intent.product:
        return False
    if intent.side is OrderSide.BUY:
        return trade.taker_side is OrderSide.SELL and trade.price <= intent.limit_price
    return trade.taker_side is OrderSide.BUY and trade.price >= intent.limit_price


__all__ = ["PassiveMatchingService"]
