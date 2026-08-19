"""Phase-4b dual-leg inventory ledger and depth-realistic EOD completion.

This module deliberately records immutable lifecycle and execution records; it
does not read policy-local position state or calculate PnL.  The only supported
EOD route creates declared ``EOD`` intents and sends them through the Phase-3
``DepthExecutionService``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from common.execution import DepthExecutionService
from common.foundation_contracts import (
    BookSnapshotRef,
    DecisionContext,
    DualLegLedgerState,
    EodCloseRequest,
    EodCompletion,
    EodDisposition,
    ExecutionResult,
    ExecutionStatus,
    FoundationContractError,
    HedgeMappingSpec,
    IntentLifecycleEvent,
    IntentLifecycleState,
    LedgerEvent,
    LedgerLeg,
    OrderIntent,
    OrderRole,
    OrderSide,
)
from common.lifecycle import IntentLifecycleService


class DualLegLedger:
    """Append-only position/fill-cost ledger for one run and hedge mapping.

    Maker position effects are derived from cumulative lifecycle fills. Hedge
    and EOD position effects are derived from the immutable execution result
    that the lifecycle registry accepted. This prevents a policy adapter from
    supplying an unrelated mutable position or a forged fill to the ledger.
    Fees and rebates must be supplied when that effect is first recorded; EOD
    catch-up can recover an omitted position effect, but deliberately supplies
    no inferred costs.
    """

    def __init__(self, run_id: str, hedge_mapping: HedgeMappingSpec, lifecycle: IntentLifecycleService) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise FoundationContractError("run_id must be a non-empty string")
        if not isinstance(hedge_mapping, HedgeMappingSpec):
            raise FoundationContractError("hedge_mapping must be a HedgeMappingSpec")
        if not isinstance(lifecycle, IntentLifecycleService):
            raise FoundationContractError("lifecycle must be an IntentLifecycleService")
        self.run_id = run_id
        self.hedge_mapping = hedge_mapping
        self._lifecycle = lifecycle
        self._events: list[LedgerEvent] = []
        self._source_keys: set[str] = set()
        self._maker_filled_by_intent: dict[str, int] = {}
        self._quoted_position = 0
        self._hedge_position = 0
        self._total_fees = 0.0
        self._total_rebates = 0.0
        self._event_seq = 0

    @property
    def lifecycle(self) -> IntentLifecycleService:
        """The single lifecycle registry from which this ledger accepts effects."""
        return self._lifecycle

    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    def state(self) -> DualLegLedgerState:
        return DualLegLedgerState(
            self.run_id,
            self.hedge_mapping.hedge_pair,
            self.hedge_mapping,
            self._quoted_position,
            self._hedge_position,
            self.hedge_mapping.pending_hedge_quantity(self._quoted_position, self._hedge_position),
            self.hedge_mapping.residual_risk(self._quoted_position, self._hedge_position),
            self._total_fees,
            self._total_rebates,
            tuple(event.event_id for event in self._events),
        )

    def has_recorded_lifecycle(self, lifecycle_event_id: str) -> bool:
        return self._source_key("lifecycle", lifecycle_event_id) in self._source_keys

    def has_recorded_execution(self, execution_id: str) -> bool:
        return self._source_key("execution", execution_id) in self._source_keys

    def record_lifecycle(
        self,
        event: IntentLifecycleEvent,
        *,
        fee: float = 0.0,
        rebate: float = 0.0,
        passive_fill_evidence_id: str | None = None,
    ) -> LedgerEvent | None:
        """Record the incremental position effect of a maker lifecycle fill.

        Events with no additional maker fill are retained as observed source
        events but produce no zero-quantity ledger row. Fees/rebates therefore
        must be supplied only with a real fill effect.
        """
        if not isinstance(event, IntentLifecycleEvent):
            raise FoundationContractError("event must be an IntentLifecycleEvent")
        if passive_fill_evidence_id is not None and (
            not isinstance(passive_fill_evidence_id, str) or not passive_fill_evidence_id.strip()
        ):
            raise FoundationContractError("passive_fill_evidence_id must be a non-empty string or None")
        intent, context = self._validated_lifecycle_event(event)
        if intent.role is not OrderRole.MAKER:
            raise FoundationContractError("only maker lifecycle fills may create ledger effects")
        source_key = self._source_key("lifecycle", event.event_id)
        if source_key in self._source_keys:
            raise FoundationContractError("lifecycle event is already recorded by the ledger")
        previous_filled = self._maker_filled_by_intent.get(intent.intent_id, 0)
        if event.filled_qty < previous_filled or event.filled_qty + event.residual_qty != intent.requested_qty:
            raise FoundationContractError("maker lifecycle quantities do not reconcile to the registered intent")
        delta_qty = event.filled_qty - previous_filled
        numeric_fee, numeric_rebate = _costs(fee, rebate)
        if delta_qty == 0 and (numeric_fee != 0.0 or numeric_rebate != 0.0):
            raise FoundationContractError("a zero-quantity lifecycle event cannot carry fee or rebate")
        self._source_keys.add(source_key)
        self._maker_filled_by_intent[intent.intent_id] = event.filled_qty
        if delta_qty == 0:
            return None
        return self._append_effect(
            source_event_id=event.event_id,
            context=context,
            intent=intent,
            quantity=delta_qty,
            occurred_at=event.occurred_at,
            fee=numeric_fee,
            rebate=numeric_rebate,
            attributes={
                "effect_source": "maker_lifecycle_fill",
                "intent_id": intent.intent_id,
                "order_role": intent.role.value,
                "lifecycle_state": event.state.value,
                "fill_qty": delta_qty,
                **(
                    {} if passive_fill_evidence_id is None else {"passive_fill_evidence_id": passive_fill_evidence_id}
                ),
            },
        )

    def record_execution(
        self,
        result: ExecutionResult,
        *,
        fee: float = 0.0,
        rebate: float = 0.0,
    ) -> LedgerEvent | None:
        """Record a hedge/EOD fill only after lifecycle accepted that result."""
        if not isinstance(result, ExecutionResult):
            raise FoundationContractError("result must be an ExecutionResult")
        intent, context = self._validated_execution_result(result)
        if intent.role not in {OrderRole.HEDGE, OrderRole.EOD}:
            raise FoundationContractError("maker execution effects must use record_lifecycle")
        source_key = self._source_key("execution", result.execution_id)
        if source_key in self._source_keys:
            raise FoundationContractError("execution result is already recorded by the ledger")
        numeric_fee, numeric_rebate = _costs(fee, rebate)
        if result.filled_qty == 0 and (numeric_fee != 0.0 or numeric_rebate != 0.0):
            raise FoundationContractError("a zero-fill execution result cannot carry fee or rebate")
        self._source_keys.add(source_key)
        if result.filled_qty == 0:
            return None
        return self._append_effect(
            source_event_id=result.execution_id,
            context=context,
            intent=intent,
            quantity=result.filled_qty,
            occurred_at=result.executed_at,
            fee=numeric_fee,
            rebate=numeric_rebate,
            attributes={
                "effect_source": "aggressive_execution",
                "intent_id": intent.intent_id,
                "order_role": intent.role.value,
                "execution_id": result.execution_id,
                "execution_status": result.status.value,
                "fill_qty": result.filled_qty,
                "vwap": result.vwap,
                "execution_model_id": result.execution_model_ref.model_id,
                "execution_model_version": result.execution_model_ref.version,
            },
        )

    def reconcile(self) -> DualLegLedgerState:
        """Fail closed unless the immutable ledger rows reconstruct every total."""
        quoted_position = sum(event.position_delta for event in self._events if event.leg is LedgerLeg.QUOTED)
        hedge_position = sum(event.position_delta for event in self._events if event.leg is LedgerLeg.HEDGE)
        total_fees = sum(float(event.fee) for event in self._events)
        total_rebates = sum(float(event.rebate) for event in self._events)
        if (
            quoted_position != self._quoted_position
            or hedge_position != self._hedge_position
            or not math.isclose(total_fees, self._total_fees, rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(total_rebates, self._total_rebates, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise FoundationContractError("ledger totals do not reconcile to immutable ledger events")
        return self.state()

    def _validated_lifecycle_event(self, event: IntentLifecycleEvent) -> tuple[OrderIntent, DecisionContext]:
        intent = self._lifecycle.registered_intent(event.intent_id)
        context = self._lifecycle.decision_context(event.intent_id)
        if event not in self._lifecycle.intent_history(event.intent_id):
            raise FoundationContractError("lifecycle event was not emitted by this lifecycle registry")
        if (
            event.run_id != self.run_id
            or event.run_id != intent.run_id
            or event.decision_id != intent.decision_id
            or event.hedge_pair != self.hedge_mapping.hedge_pair
            or event.hedge_pair != intent.hedge_pair
            or event.product != intent.product
            or context.run_id != self.run_id
            or context.hedge_pair != self.hedge_mapping.hedge_pair
        ):
            raise FoundationContractError("lifecycle event does not match this ledger's registered run/pair/intent")
        return intent, context

    def _validated_execution_result(self, result: ExecutionResult) -> tuple[OrderIntent, DecisionContext]:
        intent = self._lifecycle.registered_intent(result.intent_id)
        context = self._lifecycle.decision_context(result.intent_id)
        if not self._lifecycle.has_attached_execution(result.intent_id, result.execution_id):
            raise FoundationContractError("execution result was not accepted by the lifecycle registry")
        if self._lifecycle.attached_execution_result(result.intent_id) != result:
            raise FoundationContractError("execution result differs from the lifecycle-attached result")
        expected_snapshot = context.quoted_book if intent.product == context.quoted_product else context.hedge_book
        lifecycle_event = next(
            (event for event in self._lifecycle.intent_history(result.intent_id) if event.execution_id == result.execution_id),
            None,
        )
        if lifecycle_event is None or lifecycle_event.execution_model_ref != result.execution_model_ref:
            raise FoundationContractError("execution result does not match its lifecycle model reference")
        if (
            result.run_id != self.run_id
            or result.run_id != intent.run_id
            or result.decision_id != intent.decision_id
            or result.hedge_pair != self.hedge_mapping.hedge_pair
            or result.hedge_pair != intent.hedge_pair
            or result.product != intent.product
            or result.side is not intent.side
            or result.requested_qty != intent.requested_qty
            or result.decision_feed_seq != context.feed_seq
            or result.decision_book_snapshot != expected_snapshot
            or result.executed_at < context.dec_ts
            or result.book_snapshot.feed_seq > result.execution_feed_seq
            or _book_visible_at(result.book_snapshot) > result.executed_at
        ):
            raise FoundationContractError("execution result does not match this ledger's registered run/pair/intent")
        return intent, context

    def _append_effect(
        self,
        *,
        source_event_id: str,
        context: DecisionContext,
        intent: OrderIntent,
        quantity: int,
        occurred_at: datetime,
        fee: float,
        rebate: float,
        attributes: Mapping[str, object],
    ) -> LedgerEvent:
        if not isinstance(quantity, int) or quantity <= 0:
            raise FoundationContractError("ledger effect quantity must be a positive integer")
        leg = LedgerLeg.QUOTED if intent.product == self.hedge_mapping.hedge_pair.quoted_product else LedgerLeg.HEDGE
        position_delta = quantity if intent.side is OrderSide.BUY else -quantity
        event_seq = self._event_seq + 1
        event = LedgerEvent(
            f"{self.run_id}:ledger:{event_seq}",
            self.run_id,
            context.decision_id,
            source_event_id,
            self.hedge_mapping.hedge_pair,
            leg,
            intent.product,
            position_delta,
            occurred_at,
            attributes,
            fee,
            rebate,
        )
        self._event_seq = event_seq
        self._events.append(event)
        if leg is LedgerLeg.QUOTED:
            self._quoted_position += position_delta
        else:
            self._hedge_position += position_delta
        self._total_fees += fee
        self._total_rebates += rebate
        return event

    @staticmethod
    def _source_key(source_type: str, source_id: str) -> str:
        if not isinstance(source_id, str) or not source_id.strip():
            raise FoundationContractError("source ID must be a non-empty string")
        return f"{source_type}:{source_id}"


class EodCompletionService:
    """Cancel live orders and close actual ledger inventory through depth execution."""

    def __init__(
        self,
        lifecycle: IntentLifecycleService,
        execution: DepthExecutionService,
        ledger: DualLegLedger,
    ) -> None:
        if not isinstance(lifecycle, IntentLifecycleService):
            raise FoundationContractError("lifecycle must be an IntentLifecycleService")
        if not isinstance(execution, DepthExecutionService):
            raise FoundationContractError("execution must be a DepthExecutionService")
        if not isinstance(ledger, DualLegLedger):
            raise FoundationContractError("ledger must be a DualLegLedger")
        if ledger.lifecycle is not lifecycle:
            raise FoundationContractError("EOD lifecycle must be the ledger lifecycle")
        self._lifecycle = lifecycle
        self._execution = execution
        self._ledger = ledger
        self._claimed_eod_ids: set[str] = set()
        self._completions: dict[str, EodCompletion] = {}

    def completion(self, eod_id: str) -> EodCompletion:
        try:
            return self._completions[eod_id]
        except KeyError as exc:
            raise FoundationContractError("EOD completion is not recorded") from exc

    def complete(
        self,
        request: EodCloseRequest,
        *,
        executed_at: datetime,
        fees_by_product: Mapping[str, float] | None = None,
        rebates_by_product: Mapping[str, float] | None = None,
    ) -> EodCompletion:
        """Complete one declared EOD close-out without fabricating liquidity."""
        if not isinstance(request, EodCloseRequest):
            raise FoundationContractError("request must be an EodCloseRequest")
        if not isinstance(executed_at, datetime) or executed_at.tzinfo is None or executed_at.utcoffset() is None:
            raise FoundationContractError("executed_at must be a timezone-aware datetime")
        context = request.context
        if executed_at < context.dec_ts:
            raise FoundationContractError("executed_at must not precede the EOD decision time")
        if context.run_id != self._ledger.run_id or context.hedge_pair != self._ledger.hedge_mapping.hedge_pair:
            raise FoundationContractError("EOD request does not match the ledger run and hedge mapping")
        if request.eod_id in self._claimed_eod_ids:
            raise FoundationContractError("EOD request ID has already been claimed; close-out cannot be retried")
        self._execution.resolve_execution_model(request.execution_model_ref)
        open_intents = self._lifecycle.open_intents(context.hedge_pair)
        if any(intent.role is OrderRole.EOD for intent in open_intents):
            raise FoundationContractError("cannot begin EOD while an EOD intent is already open")
        fee_map = _cost_map(fees_by_product, context, "fees_by_product")
        rebate_map = _cost_map(rebates_by_product, context, "rebates_by_product")
        self._claimed_eod_ids.add(request.eod_id)

        cancelled_intent_ids: list[str] = []
        for intent in open_intents:
            transition = self._lifecycle.terminate(
                intent.intent_id,
                state=self._cancelled_state(),
                occurred_at=executed_at,
                disposition_reason="eod_closeout",
            )
            cancelled_intent_ids.append(intent.intent_id)
            if intent.role is OrderRole.MAKER and not self._ledger.has_recorded_lifecycle(transition.event.event_id):
                self._ledger.record_lifecycle(transition.event)
            prior_result = self._lifecycle.attached_execution_result(intent.intent_id)
            if prior_result is not None and not self._ledger.has_recorded_execution(prior_result.execution_id):
                self._ledger.record_execution(prior_result)

        starting_state = self._ledger.reconcile()
        close_intents = self._close_intents(request, starting_state)
        for intent in close_intents:
            self._lifecycle.submit_intent(intent, context, occurred_at=executed_at)
            self._lifecycle.arrive(intent.intent_id, occurred_at=executed_at)
            self._execution.register_intent(intent, context)

        results: list[ExecutionResult] = []
        for intent in close_intents:
            result = self._execution.execute(intent.intent_id, executed_at=executed_at)
            self._lifecycle.record_execution(result)
            self._ledger.record_execution(
                result,
                fee=fee_map[result.product],
                rebate=rebate_map[result.product],
            )
            if result.status is ExecutionStatus.PARTIAL:
                self._lifecycle.terminate(
                    intent.intent_id,
                    IntentLifecycleState.FAILED,
                    occurred_at=executed_at,
                    disposition_reason="eod_incomplete_liquidity",
                )
            results.append(result)

        final_state = self._ledger.reconcile()
        expected_quoted = starting_state.quoted_position
        expected_hedge = starting_state.hedge_position
        for result in results:
            signed_fill = result.filled_qty if result.side is OrderSide.BUY else -result.filled_qty
            if result.product == context.quoted_product:
                expected_quoted += signed_fill
            else:
                expected_hedge += signed_fill
        tolerance = float(self._ledger.hedge_mapping.quantity_tolerance)
        if (
            abs(final_state.quoted_position - expected_quoted) > tolerance
            or abs(final_state.hedge_position - expected_hedge) > tolerance
        ):
            raise FoundationContractError("EOD fills and ledger positions exceed the declared quantity tolerance")

        disposition = (
            EodDisposition.FLAT
            if final_state.quoted_position == 0 and final_state.hedge_position == 0
            else EodDisposition.INCOMPLETE_LIQUIDITY
        )
        completion = EodCompletion(
            request.eod_id,
            context.run_id,
            context.decision_id,
            context.hedge_pair,
            executed_at,
            disposition,
            tuple(cancelled_intent_ids),
            tuple(result.execution_id for result in results),
            final_state.quoted_position,
            final_state.hedge_position,
            final_state.residual_risk,
        )
        self._completions[request.eod_id] = completion
        return completion

    def _close_intents(self, request: EodCloseRequest, state: DualLegLedgerState) -> tuple[OrderIntent, ...]:
        context = request.context
        positions = (
            (LedgerLeg.QUOTED, state.quoted_position, context.quoted_product),
            (LedgerLeg.HEDGE, state.hedge_position, context.hedge_product),
        )
        intents: list[OrderIntent] = []
        for leg, position, product in positions:
            if position == 0:
                continue
            intents.append(
                OrderIntent(
                    f"{request.eod_id}:{leg.value}",
                    context.run_id,
                    context.decision_id,
                    context.hedge_pair,
                    product,
                    OrderRole.EOD,
                    OrderSide.SELL if position > 0 else OrderSide.BUY,
                    abs(position),
                    float(request.limit_prices[product]),
                    request.execution_model_ref,
                    {"eod_id": request.eod_id, "close_leg": leg.value},
                )
            )
        return tuple(intents)

    @staticmethod
    def _cancelled_state():
        return IntentLifecycleState.CANCELLED


def _costs(fee: float, rebate: float) -> tuple[float, float]:
    try:
        numeric_fee, numeric_rebate = float(fee), float(rebate)
    except (TypeError, ValueError) as exc:
        raise FoundationContractError("fee and rebate must be finite non-negative numbers") from exc
    if (
        not math.isfinite(numeric_fee)
        or not math.isfinite(numeric_rebate)
        or numeric_fee < 0
        or numeric_rebate < 0
    ):
        raise FoundationContractError("fee and rebate must be finite non-negative numbers")
    return numeric_fee, numeric_rebate


def _book_visible_at(snapshot: BookSnapshotRef) -> datetime:
    """Use exchange-batch visibility when a retained book is batch tagged."""
    return snapshot.exchange_batch.exchange_ts if snapshot.exchange_batch is not None else snapshot.available_at


def _cost_map(values: Mapping[str, float] | None, context: DecisionContext, field_name: str) -> dict[str, float]:
    if values is None:
        return {context.quoted_product: 0.0, context.hedge_product: 0.0}
    if not isinstance(values, Mapping):
        raise FoundationContractError(f"{field_name} must be a mapping when supplied")
    expected_products = {context.quoted_product, context.hedge_product}
    if not set(values).issubset(expected_products):
        raise FoundationContractError(f"{field_name} may contain only EOD context products")
    result = {context.quoted_product: 0.0, context.hedge_product: 0.0}
    for product, value in values.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError(f"{field_name} values must be finite non-negative numbers") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise FoundationContractError(f"{field_name} values must be finite non-negative numbers")
        result[product] = numeric
    return result


__all__ = ["DualLegLedger", "EodCompletionService"]
