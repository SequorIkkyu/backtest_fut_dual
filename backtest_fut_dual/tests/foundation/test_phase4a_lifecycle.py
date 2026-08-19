"""Phase-4a acceptance tests for intent lifecycle and maker capacity controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from common.execution import DepthBook, DepthExecutionService, DepthLevel
from common.foundation_contracts import (
    CapacityEnvelope,
    ExecutionLevel,
    ExecutionModelConfig,
    ExecutionModelRef,
    ExecutionResult,
    ExecutionStatus,
    FoundationContractError,
    IntentLifecycleState,
    OrderIntent,
    OrderRole,
    OrderSide,
    ReservationAction,
)
from common.lifecycle import IntentLifecycleService
from common.tests.foundation.fixtures import make_dual_book_fixture


def _model() -> ExecutionModelConfig:
    return ExecutionModelConfig("full-depth", "1.0.0", 1.0)


def _envelope(capacity: int = 10) -> CapacityEnvelope:
    fixture = make_dual_book_fixture()
    return CapacityEnvelope("quoted-maker-cap", fixture.hedge_pair, fixture.quoted_spec.product, capacity)


def _service(capacity: int = 10) -> IntentLifecycleService:
    model = _model()
    return IntentLifecycleService(
        (model,),
        ExecutionModelRef(model.model_id, model.version),
        (_envelope(capacity),),
    )


def _maker(intent_id: str, qty: int) -> OrderIntent:
    fixture = make_dual_book_fixture()
    return OrderIntent(
        intent_id,
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        qty,
        78000.0,
    )


def _hedge(intent_id: str, qty: int = 2) -> OrderIntent:
    fixture = make_dual_book_fixture()
    return OrderIntent(
        intent_id,
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.BUY,
        qty,
        77985.0,
    )


def _external_execution(
    intent: OrderIntent,
    status: ExecutionStatus,
    *,
    execution_id: str,
    occurred_at,
) -> ExecutionResult:
    """Build a structurally valid external result for lifecycle conformance."""
    fixture = make_dual_book_fixture()
    is_partial = status is ExecutionStatus.PARTIAL
    filled_qty = 1 if is_partial else 0
    return ExecutionResult(
        execution_id,
        intent.intent_id,
        intent.run_id,
        intent.decision_id,
        intent.hedge_pair,
        intent.product,
        intent.side,
        status,
        intent.requested_qty,
        filled_qty,
        intent.requested_qty - filled_qty,
        occurred_at,
        ExecutionModelRef("full-depth", "1.0.0"),
        1.0,
        fixture.decision_context.feed_seq,
        fixture.decision_context.feed_seq,
        fixture.hedge_book,
        intent.limit_price,
        (ExecutionLevel(intent.limit_price, filled_qty),) if is_partial else (),
        intent.limit_price if is_partial else None,
        intent.limit_price if is_partial else None,
    )


def _at(milliseconds: int):
    return make_dual_book_fixture().decision_context.dec_ts + timedelta(milliseconds=milliseconds)


def test_worst_case_live_maker_reservation_caps_all_orders_and_releases_by_fill_cancel():
    lifecycle = _service(capacity=5)
    first = _maker("maker-first", 3)
    second = _maker("maker-second", 3)

    submitted = lifecycle.submit_intent(first, make_dual_book_fixture().decision_context, occurred_at=_at(1), envelope_id="quoted-maker-cap")
    assert submitted.event.state is IntentLifecycleState.SUBMITTED
    assert submitted.reservations[0].action is ReservationAction.RESERVE
    assert submitted.reservations[0].amount == 3.0
    assert submitted.event.execution_model_ref == ExecutionModelRef("full-depth", "1.0.0")
    assert lifecycle.reserved_qty("quoted-maker-cap") == 3

    rejected = lifecycle.submit_intent(second, make_dual_book_fixture().decision_context, occurred_at=_at(2), envelope_id="quoted-maker-cap")
    assert rejected.event.state is IntentLifecycleState.REJECTED
    assert rejected.event.disposition_reason == "capacity_envelope_exceeded"
    assert rejected.reservations == () and lifecycle.reserved_qty("quoted-maker-cap") == 3

    lifecycle.arrive(first.intent_id, occurred_at=_at(3))
    partial = lifecycle.record_passive_fill(first.intent_id, 2, occurred_at=_at(4))
    assert partial.event.state is IntentLifecycleState.PARTIALLY_FILLED
    assert partial.reservations[0].action is ReservationAction.RELEASE
    assert partial.reservations[0].amount == 2.0
    assert lifecycle.reserved_qty("quoted-maker-cap") == 1

    cancelled = lifecycle.terminate(
        first.intent_id,
        IntentLifecycleState.CANCELLED,
        occurred_at=_at(5),
        disposition_reason="quote_withdrawn",
    )
    assert cancelled.reservations[0].amount == 1.0
    assert lifecycle.reserved_qty("quoted-maker-cap") == 0
    assert [event.state for event in lifecycle.intent_history(first.intent_id)] == [
        IntentLifecycleState.SUBMITTED,
        IntentLifecycleState.ARRIVED,
        IntentLifecycleState.PARTIALLY_FILLED,
        IntentLifecycleState.CANCELLED,
    ]


def test_reservation_is_released_only_by_full_fill_or_declared_terminal_transition():
    lifecycle = _service(capacity=10)
    filled = _maker("maker-filled", 2)
    expired = _maker("maker-expired", 3)
    lifecycle.submit_intent(filled, make_dual_book_fixture().decision_context, occurred_at=_at(1), envelope_id="quoted-maker-cap")
    lifecycle.submit_intent(expired, make_dual_book_fixture().decision_context, occurred_at=_at(2), envelope_id="quoted-maker-cap")
    assert lifecycle.reserved_qty("quoted-maker-cap") == 5

    fill_transition = lifecycle.record_passive_fill(filled.intent_id, 2, occurred_at=_at(3))
    assert fill_transition.event.state is IntentLifecycleState.FILLED
    assert fill_transition.reservations[0].amount == 2.0
    assert lifecycle.reserved_qty("quoted-maker-cap") == 3

    expire_transition = lifecycle.terminate(
        expired.intent_id,
        IntentLifecycleState.EXPIRED,
        occurred_at=_at(4),
        disposition_reason="time_in_force_elapsed",
    )
    assert expire_transition.event.state is IntentLifecycleState.EXPIRED
    assert expire_transition.reservations[0].amount == 3.0
    assert lifecycle.reserved_qty("quoted-maker-cap") == 0


def test_invalid_transitions_underflow_and_terminal_retries_fail_closed():
    lifecycle = _service(capacity=5)
    maker = _maker("maker-invalid", 2)
    try:
        lifecycle.arrive("unknown", occurred_at=_at(1))
    except FoundationContractError as exc:
        assert "not registered" in str(exc)
    else:
        raise AssertionError("unknown intent transition must fail")

    lifecycle.submit_intent(maker, make_dual_book_fixture().decision_context, occurred_at=_at(1), envelope_id="quoted-maker-cap")
    try:
        lifecycle.record_passive_fill(maker.intent_id, 3, occurred_at=_at(2))
    except FoundationContractError as exc:
        assert "remaining" in str(exc)
    else:
        raise AssertionError("fill beyond remaining quantity must fail")
    assert lifecycle.reserved_qty("quoted-maker-cap") == 2
    try:
        lifecycle.terminate(
            maker.intent_id,
            IntentLifecycleState.FILLED,
            occurred_at=_at(2),
            disposition_reason="manual_fill",
        )
    except FoundationContractError as exc:
        assert "non-fill terminal" in str(exc)
    else:
        raise AssertionError("filled state cannot bypass a fill lifecycle transition")

    lifecycle.terminate(
        maker.intent_id,
        IntentLifecycleState.FAILED,
        occurred_at=_at(3),
        disposition_reason="venue_reject",
    )
    try:
        lifecycle.arrive(maker.intent_id, occurred_at=_at(4))
    except FoundationContractError as exc:
        assert "cannot arrive" in str(exc)
    else:
        raise AssertionError("terminal intent must not be retried")
    assert lifecycle.reserved_qty("quoted-maker-cap") == 0


def test_each_manual_terminal_disposition_is_reconstructible_and_non_retryable():
    for index, state in enumerate(
        (
            IntentLifecycleState.CANCELLED,
            IntentLifecycleState.EXPIRED,
            IntentLifecycleState.REJECTED,
            IntentLifecycleState.STALE,
            IntentLifecycleState.DEADLINE,
            IntentLifecycleState.FAILED,
        )
    ):
        lifecycle = _service(capacity=10)
        maker = _maker(f"maker-terminal-{state.value}", 1)
        lifecycle.submit_intent(
            maker,
            make_dual_book_fixture().decision_context,
            occurred_at=_at(1),
            envelope_id="quoted-maker-cap",
        )
        transition = lifecycle.terminate(
            maker.intent_id,
            state,
            occurred_at=_at(index + 2),
            disposition_reason=f"declared_{state.value}",
        )
        assert transition.event.state is state
        assert transition.event.disposition_reason == f"declared_{state.value}"
        assert lifecycle.reserved_qty("quoted-maker-cap") == 0
        try:
            lifecycle.terminate(
                maker.intent_id,
                state,
                occurred_at=_at(index + 10),
                disposition_reason="retry",
            )
        except FoundationContractError:
            pass
        else:
            raise AssertionError("terminal disposition must not be retried")


def test_execution_result_attaches_only_to_matching_registered_nonmaker_intent():
    fixture = make_dual_book_fixture()
    model = _model()
    lifecycle = _service(capacity=10)
    execution = DepthExecutionService(
        (fixture.quoted_spec, fixture.hedge_spec),
        (model,),
        ExecutionModelRef(model.model_id, model.version),
    )
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(), asks=(DepthLevel(77985.0, 2),)))
    hedge = _hedge("registered-hedge", 2)
    lifecycle.submit_intent(hedge, fixture.decision_context, occurred_at=_at(1))
    lifecycle.arrive(hedge.intent_id, occurred_at=_at(2))
    execution.register_intent(hedge, fixture.decision_context)
    result = execution.execute(hedge.intent_id, executed_at=_at(3))
    attached = lifecycle.record_execution(result)
    assert attached.event.state is IntentLifecycleState.FILLED
    assert attached.event.execution_id == result.execution_id

    mismatched = _hedge("mismatched-hedge", 1)
    lifecycle.submit_intent(mismatched, fixture.decision_context, occurred_at=_at(4))
    execution.ingest_book(
        DepthBook(
            replace(fixture.hedge_book, book_seq=2, feed_seq=fixture.decision_context.feed_seq + 1, snapshot_id="hedge-2"),
            bids=(),
            asks=(DepthLevel(77985.0, 1),),
        )
    )
    execution.register_intent(mismatched, fixture.decision_context)
    # Its newer active depth intentionally makes the old decision snapshot stale;
    # use an independently valid result object only to prove lifecycle identity checks.
    stale_result = execution.execute(mismatched.intent_id, executed_at=_at(5))
    invalid_result = replace(stale_result, execution_model_ref=ExecutionModelRef("wrong", "1.0.0"))
    try:
        lifecycle.record_execution(invalid_result)
    except FoundationContractError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched execution result must not attach to lifecycle record")
    assert lifecycle.state_of(mismatched.intent_id) is IntentLifecycleState.SUBMITTED


def test_execution_attachment_is_one_shot_and_execution_ids_are_unique():
    lifecycle = _service()
    first = _hedge("one-shot-first")
    lifecycle.submit_intent(first, make_dual_book_fixture().decision_context, occurred_at=_at(1))
    partial = _external_execution(first, ExecutionStatus.PARTIAL, execution_id="execution-first", occurred_at=_at(2))
    attached = lifecycle.record_execution(partial)
    assert attached.event.state is IntentLifecycleState.PARTIALLY_FILLED

    try:
        lifecycle.record_execution(replace(partial, execution_id="execution-second", executed_at=_at(3)))
    except FoundationContractError as exc:
        assert "already has an attached execution result" in str(exc)
    else:
        raise AssertionError("a partial execution intent must not accept a second execution attachment")
    assert len(lifecycle.intent_history(first.intent_id)) == 2

    second = _hedge("one-shot-second")
    lifecycle.submit_intent(second, make_dual_book_fixture().decision_context, occurred_at=_at(4))
    duplicate_id = replace(
        partial,
        intent_id=second.intent_id,
        status=ExecutionStatus.REJECTED,
        filled_qty=0,
        residual_qty=second.requested_qty,
        executed_at=_at(5),
        levels=(),
        executable_touch=None,
        vwap=None,
    )
    try:
        lifecycle.record_execution(duplicate_id)
    except FoundationContractError as exc:
        assert "execution_id is already attached" in str(exc)
    else:
        raise AssertionError("an execution ID must not attach to two intents")
    assert lifecycle.state_of(second.intent_id) is IntentLifecycleState.SUBMITTED


def test_execution_terminal_statuses_map_to_lifecycle_states():
    mappings = (
        (ExecutionStatus.REJECTED, IntentLifecycleState.REJECTED),
        (ExecutionStatus.STALE, IntentLifecycleState.STALE),
        (ExecutionStatus.DEADLINE, IntentLifecycleState.DEADLINE),
        (ExecutionStatus.NO_LIQUIDITY, IntentLifecycleState.FAILED),
        (ExecutionStatus.FAILED, IntentLifecycleState.FAILED),
    )
    for index, (status, expected_state) in enumerate(mappings, start=1):
        lifecycle = _service()
        hedge = _hedge(f"terminal-{status.value}")
        lifecycle.submit_intent(hedge, make_dual_book_fixture().decision_context, occurred_at=_at(index))
        result = _external_execution(
            hedge,
            status,
            execution_id=f"execution-{status.value}",
            occurred_at=_at(index + 10),
        )
        transition = lifecycle.record_execution(result)
        assert transition.event.state is expected_state
        assert transition.event.disposition_reason == status.value


def test_maker_intent_cannot_bypass_capacity_through_aggressive_execution_attachment():
    lifecycle = _service(capacity=3)
    maker = _maker("maker-no-aggressive-bypass", 1)
    fixture = make_dual_book_fixture()
    lifecycle.submit_intent(maker, fixture.decision_context, occurred_at=_at(1), envelope_id="quoted-maker-cap")
    forged_execution = ExecutionResult(
        "forged-maker-execution",
        maker.intent_id,
        maker.run_id,
        maker.decision_id,
        maker.hedge_pair,
        maker.product,
        maker.side,
        ExecutionStatus.FILLED,
        1,
        1,
        0,
        _at(2),
        ExecutionModelRef("full-depth", "1.0.0"),
        1.0,
        fixture.decision_context.feed_seq,
        fixture.decision_context.feed_seq,
        fixture.quoted_book,
        maker.limit_price,
        (ExecutionLevel(78000.0, 1),),
        78000.0,
        78000.0,
    )
    try:
        lifecycle.record_execution(forged_execution)
    except FoundationContractError as exc:
        assert "record_passive_fill" in str(exc)
    else:
        raise AssertionError("maker execution cannot bypass passive-fill lifecycle capacity release")
    assert lifecycle.reserved_qty("quoted-maker-cap") == 1
