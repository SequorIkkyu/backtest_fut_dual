"""Phase-4a intent lifecycle and worst-case live-maker capacity controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from common.foundation_contracts import (
    CapacityEnvelope,
    CapacityReservationEvent,
    DecisionContext,
    ExecutionModelConfig,
    ExecutionModelRef,
    ExecutionResult,
    ExecutionStatus,
    FoundationContractError,
    HedgePairRef,
    IntentLifecycleEvent,
    IntentLifecycleState,
    OrderIntent,
    OrderRole,
    ReservationAction,
)


_OPEN_STATES = frozenset(
    {
        IntentLifecycleState.SUBMITTED,
        IntentLifecycleState.ARRIVED,
        IntentLifecycleState.PARTIALLY_FILLED,
    }
)
_MANUAL_TERMINAL_STATES = frozenset(
    {
        IntentLifecycleState.CANCELLED,
        IntentLifecycleState.EXPIRED,
        IntentLifecycleState.REJECTED,
        IntentLifecycleState.STALE,
        IntentLifecycleState.DEADLINE,
        IntentLifecycleState.FAILED,
    }
)


@dataclass(frozen=True)
class LifecycleTransition:
    """One immutable lifecycle state transition and its reservation side effects."""

    event: IntentLifecycleEvent
    reservations: tuple[CapacityReservationEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event, IntentLifecycleEvent):
            raise FoundationContractError("event must be an IntentLifecycleEvent")
        if not isinstance(self.reservations, tuple) or any(
            not isinstance(event, CapacityReservationEvent) for event in self.reservations
        ):
            raise FoundationContractError("reservations must be a tuple of CapacityReservationEvent values")


@dataclass
class _IntentRecord:
    intent: OrderIntent
    context: DecisionContext
    execution_model_ref: ExecutionModelRef
    envelope: CapacityEnvelope | None
    state: IntentLifecycleState
    filled_qty: int
    residual_qty: int
    reserved_qty: int
    last_occurred_at: datetime
    events: list[IntentLifecycleEvent]
    reservations: list[CapacityReservationEvent]
    execution_id: str | None = None
    execution_result: ExecutionResult | None = None


class IntentLifecycleService:
    """Fail-closed registry for intent state and maker-order capacity.

    Policy supplies immutable ``CapacityEnvelope`` values. The service reserves
    each submitted maker intent's complete requested quantity, releases only
    from a valid lifecycle transition, and never carries retry or hedge-target
    policy. Aggressive execution results are accepted only when they reconcile
    with the registered intent, decision context, and resolved model reference.
    """

    def __init__(
        self,
        execution_models: Iterable[ExecutionModelConfig],
        default_execution_model: ExecutionModelRef,
        capacity_envelopes: Iterable[CapacityEnvelope],
    ) -> None:
        self._models = _model_map(execution_models)
        if not isinstance(default_execution_model, ExecutionModelRef):
            raise FoundationContractError("default_execution_model must be an ExecutionModelRef")
        if _model_key(default_execution_model) not in self._models:
            raise FoundationContractError("default_execution_model must be configured")
        self.default_execution_model = default_execution_model
        self._envelopes = _envelope_map(capacity_envelopes)
        self._reserved_by_envelope = {envelope_id: 0 for envelope_id in self._envelopes}
        self._records: dict[str, _IntentRecord] = {}
        self._execution_ids: dict[str, str] = {}
        self._event_seq = 0
        self._reservation_seq = 0

    def submit_intent(
        self,
        intent: OrderIntent,
        context: DecisionContext,
        *,
        occurred_at: datetime,
        envelope_id: str | None = None,
    ) -> LifecycleTransition:
        """Register one intent and reserve its worst-case maker capacity.

        A capacity breach is a registered terminal ``REJECTED`` state with the
        explicit ``capacity_envelope_exceeded`` disposition. Invalid identity or
        configuration is a fail-closed contract error and creates no record.
        """
        self._validate_intent_context(intent, context)
        self._validate_occurred_at(occurred_at, context)
        if intent.intent_id in self._records:
            raise FoundationContractError("intent_id is already registered")
        model_ref = self._resolve_model_ref(intent)
        envelope = self._resolve_envelope(intent, envelope_id)
        record = _IntentRecord(
            intent=intent,
            context=context,
            execution_model_ref=model_ref,
            envelope=envelope,
            state=IntentLifecycleState.SUBMITTED,
            filled_qty=0,
            residual_qty=intent.requested_qty,
            reserved_qty=0,
            last_occurred_at=occurred_at,
            events=[],
            reservations=[],
        )
        if envelope is not None and self._reserved_by_envelope[envelope.envelope_id] + intent.requested_qty > envelope.max_reserved_qty:
            transition = self._transition(record, occurred_at, IntentLifecycleState.REJECTED, "capacity_envelope_exceeded")
            self._records[intent.intent_id] = record
            return transition

        reservations: tuple[CapacityReservationEvent, ...] = ()
        commit: Callable[[], None] | None = None
        if envelope is not None:
            reservations = (self._stage_reservation(record, ReservationAction.RESERVE, intent.requested_qty, occurred_at),)
            commit = lambda: self._commit_reservation(record, ReservationAction.RESERVE, intent.requested_qty)
        transition = self._transition(
            record,
            occurred_at,
            IntentLifecycleState.SUBMITTED,
            None,
            reservations=reservations,
            on_commit=commit,
        )
        self._records[intent.intent_id] = record
        return transition

    def arrive(self, intent_id: str, *, occurred_at: datetime) -> LifecycleTransition:
        """Transition a submitted intent to the arrived state."""
        record = self._record(intent_id)
        self._require_state(record, {IntentLifecycleState.SUBMITTED}, "arrive")
        self._validate_transition_time(record, occurred_at)
        return self._transition(record, occurred_at, IntentLifecycleState.ARRIVED, None)

    def record_passive_fill(self, intent_id: str, quantity: int, *, occurred_at: datetime) -> LifecycleTransition:
        """Record one maker fill and release exactly the resolved live-order capacity."""
        record = self._record(intent_id)
        self._require_state(record, _OPEN_STATES, "record a passive fill")
        self._validate_transition_time(record, occurred_at)
        if record.intent.role is not OrderRole.MAKER:
            raise FoundationContractError("passive fills require a registered maker intent")
        if not isinstance(quantity, int) or quantity <= 0 or quantity > record.residual_qty:
            raise FoundationContractError("passive fill quantity must be within remaining intent quantity")
        if quantity > record.reserved_qty:
            raise FoundationContractError("passive fill would release unreserved capacity")

        filled_qty = record.filled_qty + quantity
        residual_qty = record.residual_qty - quantity
        release = self._stage_reservation(record, ReservationAction.RELEASE, quantity, occurred_at)
        state = IntentLifecycleState.FILLED if residual_qty == 0 else IntentLifecycleState.PARTIALLY_FILLED
        return self._transition(
            record,
            occurred_at,
            state,
            None,
            reservations=(release,),
            filled_qty=filled_qty,
            residual_qty=residual_qty,
            on_commit=lambda: self._commit_reservation(record, ReservationAction.RELEASE, quantity),
        )

    def record_execution(self, result: ExecutionResult) -> LifecycleTransition:
        """Attach a validated aggressive execution result to its registered intent."""
        if not isinstance(result, ExecutionResult):
            raise FoundationContractError("result must be an ExecutionResult")
        record = self._record(result.intent_id)
        self._require_state(record, _OPEN_STATES, "record an execution")
        self._validate_transition_time(record, result.executed_at)
        if record.execution_id is not None:
            raise FoundationContractError("registered intent already has an attached execution result")
        if result.execution_id in self._execution_ids:
            raise FoundationContractError("execution_id is already attached to an intent")
        if record.intent.role is OrderRole.MAKER:
            raise FoundationContractError("maker fills must use record_passive_fill")
        self._validate_execution_result(record, result)
        if result.status is ExecutionStatus.FILLED:
            state, reason = IntentLifecycleState.FILLED, None
        elif result.status is ExecutionStatus.PARTIAL:
            state, reason = IntentLifecycleState.PARTIALLY_FILLED, None
        elif result.status is ExecutionStatus.REJECTED:
            state, reason = IntentLifecycleState.REJECTED, _result_reason(result)
        elif result.status is ExecutionStatus.STALE:
            state, reason = IntentLifecycleState.STALE, _result_reason(result)
        elif result.status is ExecutionStatus.DEADLINE:
            state, reason = IntentLifecycleState.DEADLINE, _result_reason(result)
        else:
            state, reason = IntentLifecycleState.FAILED, _result_reason(result)
        return self._transition(
            record,
            result.executed_at,
            state,
            reason,
            execution_id=result.execution_id,
            filled_qty=result.filled_qty,
            residual_qty=result.residual_qty,
            on_commit=lambda: self._commit_execution_attachment(record, result),
        )

    def terminate(
        self,
        intent_id: str,
        state: IntentLifecycleState,
        *,
        occurred_at: datetime,
        disposition_reason: str,
    ) -> LifecycleTransition:
        """Apply an explicit non-fill terminal disposition and release live capacity."""
        record = self._record(intent_id)
        self._require_state(record, _OPEN_STATES, "terminate")
        self._validate_transition_time(record, occurred_at)
        if state not in _MANUAL_TERMINAL_STATES:
            raise FoundationContractError("terminate requires a non-fill terminal IntentLifecycleState")
        if not isinstance(disposition_reason, str) or not disposition_reason.strip():
            raise FoundationContractError("terminal disposition_reason must be a non-empty string")
        reservations: tuple[CapacityReservationEvent, ...] = ()
        commit: Callable[[], None] | None = None
        if record.reserved_qty:
            amount = record.reserved_qty
            reservations = (self._stage_reservation(record, ReservationAction.RELEASE, amount, occurred_at),)
            commit = lambda: self._commit_reservation(record, ReservationAction.RELEASE, amount)
        return self._transition(record, occurred_at, state, disposition_reason, reservations=reservations, on_commit=commit)

    def state_of(self, intent_id: str) -> IntentLifecycleState:
        return self._record(intent_id).state

    def has_intent(self, intent_id: str) -> bool:
        """Return whether an intent ID is registered without exposing registry state."""
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise FoundationContractError("intent_id must be a non-empty string")
        return intent_id in self._records

    def reserved_qty(self, envelope_id: str) -> int:
        try:
            return self._reserved_by_envelope[envelope_id]
        except KeyError as exc:
            raise FoundationContractError("unknown capacity envelope") from exc

    def intent_history(self, intent_id: str) -> tuple[IntentLifecycleEvent, ...]:
        return tuple(self._record(intent_id).events)

    def reservation_history(self, intent_id: str) -> tuple[CapacityReservationEvent, ...]:
        """Return immutable capacity side effects emitted for one registered intent."""
        return tuple(self._record(intent_id).reservations)

    def capacity_envelope(self, envelope_id: str) -> CapacityEnvelope:
        """Return the immutable policy-declared envelope used for telemetry limits."""
        if not isinstance(envelope_id, str) or not envelope_id.strip():
            raise FoundationContractError("envelope_id must be a non-empty string")
        try:
            return self._envelopes[envelope_id]
        except KeyError as exc:
            raise FoundationContractError("unknown capacity envelope") from exc

    def registered_intent(self, intent_id: str) -> OrderIntent:
        """Return the immutable intent owned by this lifecycle registry."""
        return self._record(intent_id).intent

    def decision_context(self, intent_id: str) -> DecisionContext:
        """Return the immutable causal context owned by this lifecycle registry."""
        return self._record(intent_id).context

    def has_attached_execution(self, intent_id: str, execution_id: str) -> bool:
        """Return whether this registry accepted exactly this one-shot execution ID."""
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise FoundationContractError("execution_id must be a non-empty string")
        return self._record(intent_id).execution_id == execution_id

    def attached_execution_result(self, intent_id: str) -> ExecutionResult | None:
        """Return the immutable aggressive result accepted for this intent, if any."""
        return self._record(intent_id).execution_result

    def open_intents(self, hedge_pair: HedgePairRef) -> tuple[OrderIntent, ...]:
        """Return immutable open intents for one pair in submission order."""
        if not isinstance(hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        return tuple(
            record.intent
            for record in self._records.values()
            if record.intent.hedge_pair == hedge_pair and record.state in _OPEN_STATES
        )

    def _validate_intent_context(self, intent: OrderIntent, context: DecisionContext) -> None:
        if not isinstance(intent, OrderIntent):
            raise FoundationContractError("intent must be an OrderIntent")
        if not isinstance(context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        if intent.run_id != context.run_id or intent.decision_id != context.decision_id:
            raise FoundationContractError("intent run_id and decision_id must match its decision context")
        if intent.hedge_pair != context.hedge_pair:
            raise FoundationContractError("intent hedge_pair must match its decision context")
        if intent.product not in (context.quoted_product, context.hedge_product):
            raise FoundationContractError("intent product must match a decision-context product")

    @staticmethod
    def _validate_occurred_at(occurred_at: datetime, context: DecisionContext) -> None:
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise FoundationContractError("occurred_at must be a timezone-aware datetime")
        if occurred_at < context.dec_ts:
            raise FoundationContractError("occurred_at must not precede decision time")

    def _resolve_model_ref(self, intent: OrderIntent) -> ExecutionModelRef:
        model_ref = intent.execution_model_ref or self.default_execution_model
        if _model_key(model_ref) not in self._models:
            raise FoundationContractError("intent references an unconfigured execution model")
        return model_ref

    def _resolve_envelope(self, intent: OrderIntent, envelope_id: str | None) -> CapacityEnvelope | None:
        if intent.role is not OrderRole.MAKER:
            if envelope_id is not None:
                raise FoundationContractError("only maker intents may reserve a capacity envelope")
            return None
        if not isinstance(envelope_id, str) or not envelope_id.strip():
            raise FoundationContractError("maker intents require a capacity envelope")
        try:
            envelope = self._envelopes[envelope_id]
        except KeyError as exc:
            raise FoundationContractError("unknown capacity envelope") from exc
        if envelope.hedge_pair != intent.hedge_pair or envelope.product != intent.product:
            raise FoundationContractError("capacity envelope must match maker intent pair and product")
        return envelope

    def _record(self, intent_id: str) -> _IntentRecord:
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise FoundationContractError("intent_id must be a non-empty string")
        try:
            return self._records[intent_id]
        except KeyError as exc:
            raise FoundationContractError("intent_id is not registered") from exc

    @staticmethod
    def _require_state(record: _IntentRecord, allowed: frozenset[IntentLifecycleState] | set[IntentLifecycleState], action: str) -> None:
        if record.state not in allowed:
            raise FoundationContractError(f"cannot {action} from lifecycle state {record.state.value}")

    @staticmethod
    def _validate_transition_time(record: _IntentRecord, occurred_at: datetime) -> None:
        IntentLifecycleService._validate_occurred_at(occurred_at, record.context)
        if occurred_at < record.last_occurred_at:
            raise FoundationContractError("lifecycle occurred_at must be monotone for an intent")

    def _transition(
        self,
        record: _IntentRecord,
        occurred_at: datetime,
        state: IntentLifecycleState,
        disposition_reason: str | None,
        *,
        execution_id: str | None = None,
        reservations: tuple[CapacityReservationEvent, ...] = (),
        filled_qty: int | None = None,
        residual_qty: int | None = None,
        on_commit: Callable[[], None] | None = None,
    ) -> LifecycleTransition:
        proposed_filled_qty = record.filled_qty if filled_qty is None else filled_qty
        proposed_residual_qty = record.residual_qty if residual_qty is None else residual_qty
        event_seq = self._event_seq + 1
        event = IntentLifecycleEvent(
            f"{record.intent.run_id}:{record.intent.intent_id}:lifecycle:{event_seq}",
            record.intent.run_id,
            record.intent.decision_id,
            record.intent.intent_id,
            record.intent.hedge_pair,
            record.intent.product,
            state,
            occurred_at,
            record.execution_model_ref,
            proposed_filled_qty,
            proposed_residual_qty,
            execution_id,
            disposition_reason,
        )
        transition = LifecycleTransition(event, reservations)
        if on_commit is not None:
            on_commit()
        self._event_seq = event_seq
        record.state = state
        record.filled_qty = proposed_filled_qty
        record.residual_qty = proposed_residual_qty
        record.last_occurred_at = occurred_at
        record.events.append(event)
        record.reservations.extend(reservations)
        return transition

    def _reservation_event(
        self,
        record: _IntentRecord,
        action: ReservationAction,
        amount: int,
        occurred_at: datetime,
    ) -> CapacityReservationEvent:
        if record.envelope is None:
            raise FoundationContractError("reservation requires a capacity envelope")
        reservation_seq = self._reservation_seq + 1
        event = CapacityReservationEvent(
            f"{record.intent.run_id}:{record.intent.intent_id}:reservation:{reservation_seq}",
            record.intent.run_id,
            record.intent.decision_id,
            record.intent.intent_id,
            record.intent.hedge_pair,
            record.intent.product,
            record.envelope.envelope_id,
            action,
            float(amount),
            occurred_at,
        )
        self._reservation_seq = reservation_seq
        return event

    def _stage_reservation(
        self,
        record: _IntentRecord,
        action: ReservationAction,
        amount: int,
        occurred_at: datetime,
    ) -> CapacityReservationEvent:
        if record.envelope is None or not isinstance(amount, int) or amount <= 0:
            raise FoundationContractError("capacity reservation amount must be a positive integer for an envelope")
        envelope_id = record.envelope.envelope_id
        if action is ReservationAction.RESERVE:
            if self._reserved_by_envelope[envelope_id] + amount > record.envelope.max_reserved_qty:
                raise FoundationContractError("capacity reservation would exceed its envelope")
        elif action is ReservationAction.RELEASE:
            if amount > record.reserved_qty:
                raise FoundationContractError("capacity release would underflow an intent reservation")
            if amount > self._reserved_by_envelope[envelope_id]:
                raise FoundationContractError("capacity release would underflow an envelope reservation")
        else:
            raise FoundationContractError("reservation action is not supported")
        return self._reservation_event(record, action, amount, occurred_at)

    def _commit_reservation(self, record: _IntentRecord, action: ReservationAction, amount: int) -> None:
        if record.envelope is None:
            raise FoundationContractError("reservation requires a capacity envelope")
        envelope_id = record.envelope.envelope_id
        if action is ReservationAction.RESERVE:
            record.reserved_qty += amount
            self._reserved_by_envelope[envelope_id] += amount
        elif action is ReservationAction.RELEASE:
            record.reserved_qty -= amount
            self._reserved_by_envelope[envelope_id] -= amount
        else:
            raise FoundationContractError("reservation action is not supported")

    def _commit_execution_attachment(self, record: _IntentRecord, result: ExecutionResult) -> None:
        record.execution_id = result.execution_id
        record.execution_result = result
        self._execution_ids[result.execution_id] = record.intent.intent_id

    def _validate_execution_result(self, record: _IntentRecord, result: ExecutionResult) -> None:
        intent, context = record.intent, record.context
        if (
            result.run_id != intent.run_id
            or result.decision_id != intent.decision_id
            or result.hedge_pair != intent.hedge_pair
            or result.product != intent.product
            or result.side is not intent.side
            or result.requested_qty != intent.requested_qty
            or result.execution_model_ref != record.execution_model_ref
            or result.decision_feed_seq != context.feed_seq
        ):
            raise FoundationContractError("execution result does not match its registered intent/context/model")
        expected_snapshot = context.quoted_book if intent.product == context.quoted_product else context.hedge_book
        if result.decision_book_snapshot != expected_snapshot:
            raise FoundationContractError("execution result decision snapshot does not match its registered context")
        visible_at = (
            result.book_snapshot.exchange_batch.exchange_ts
            if result.book_snapshot.exchange_batch is not None
            else result.book_snapshot.available_at
        )
        if result.book_snapshot.feed_seq > result.execution_feed_seq or visible_at > result.executed_at:
            raise FoundationContractError("execution result uses a book that was not visible at arrival")


def _result_reason(result: ExecutionResult) -> str:
    return result.disposition_reason or result.status.value


def _model_key(model_ref: ExecutionModelRef) -> tuple[str, str]:
    return model_ref.model_id, model_ref.version


def _model_map(models: Iterable[ExecutionModelConfig]) -> Mapping[tuple[str, str], ExecutionModelConfig]:
    result: dict[tuple[str, str], ExecutionModelConfig] = {}
    for model in models:
        if not isinstance(model, ExecutionModelConfig):
            raise FoundationContractError("execution_models must contain ExecutionModelConfig values")
        key = model.model_id, model.version
        if key in result:
            raise FoundationContractError("execution model references must be unique")
        result[key] = model
    if not result:
        raise FoundationContractError("at least one execution model is required")
    return MappingProxyType(result)


def _envelope_map(envelopes: Iterable[CapacityEnvelope]) -> Mapping[str, CapacityEnvelope]:
    result: dict[str, CapacityEnvelope] = {}
    for envelope in envelopes:
        if not isinstance(envelope, CapacityEnvelope):
            raise FoundationContractError("capacity_envelopes must contain CapacityEnvelope values")
        if envelope.envelope_id in result:
            raise FoundationContractError("capacity envelope IDs must be unique")
        result[envelope.envelope_id] = envelope
    return MappingProxyType(result)


__all__ = ["IntentLifecycleService", "LifecycleTransition"]
