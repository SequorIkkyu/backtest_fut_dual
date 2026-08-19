"""Phase-4b acceptance tests for the dual-leg ledger and EOD completion."""

from __future__ import annotations

from datetime import timedelta

from common.execution import DepthBook, DepthExecutionService, DepthLevel
from common.foundation_contracts import (
    CapacityEnvelope,
    EodCloseRequest,
    EodDisposition,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    IntentLifecycleState,
    OrderIntent,
    OrderRole,
    OrderSide,
)
from common.ledger import DualLegLedger, EodCompletionService
from common.lifecycle import IntentLifecycleService
from common.tests.foundation.fixtures import make_dual_book_fixture


def _at(milliseconds: int):
    return make_dual_book_fixture().decision_context.dec_ts + timedelta(milliseconds=milliseconds)


def _model() -> ExecutionModelConfig:
    return ExecutionModelConfig("full-depth", "1.0.0", 1.0)


def _lifecycle(capacity: int = 20) -> IntentLifecycleService:
    fixture = make_dual_book_fixture()
    model = _model()
    envelope = CapacityEnvelope("quoted-maker-cap", fixture.hedge_pair, fixture.quoted_spec.product, capacity)
    return IntentLifecycleService((model,), ExecutionModelRef(model.model_id, model.version), (envelope,))


def _mapping(quoted_weight: float = 1.0, hedge_weight: float = 1.0) -> HedgeMappingSpec:
    return HedgeMappingSpec(make_dual_book_fixture().hedge_pair, quoted_weight, hedge_weight)


def _execution() -> DepthExecutionService:
    fixture = make_dual_book_fixture()
    model = _model()
    return DepthExecutionService(
        (fixture.quoted_spec, fixture.hedge_spec),
        (model,),
        ExecutionModelRef(model.model_id, model.version),
    )


def _maker(intent_id: str, qty: int, side: OrderSide = OrderSide.BUY) -> OrderIntent:
    fixture = make_dual_book_fixture()
    return OrderIntent(
        intent_id,
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        side,
        qty,
        78000.0,
    )


def _hedge(intent_id: str, qty: int, side: OrderSide = OrderSide.SELL) -> OrderIntent:
    fixture = make_dual_book_fixture()
    return OrderIntent(
        intent_id,
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        side,
        qty,
        77980.0 if side is OrderSide.SELL else 77990.0,
    )


def _record_maker_fill(
    lifecycle: IntentLifecycleService,
    ledger: DualLegLedger,
    intent: OrderIntent,
    quantity: int,
    *,
    at_ms: int,
    fee: float = 0.0,
    rebate: float = 0.0,
):
    fixture = make_dual_book_fixture()
    lifecycle.submit_intent(intent, fixture.decision_context, occurred_at=_at(at_ms - 2), envelope_id="quoted-maker-cap")
    lifecycle.arrive(intent.intent_id, occurred_at=_at(at_ms - 1))
    transition = lifecycle.record_passive_fill(intent.intent_id, quantity, occurred_at=_at(at_ms))
    return ledger.record_lifecycle(transition.event, fee=fee, rebate=rebate)


def _record_hedge_execution(
    lifecycle: IntentLifecycleService,
    execution: DepthExecutionService,
    ledger: DualLegLedger,
    intent: OrderIntent,
    *,
    at_ms: int,
    fee: float = 0.0,
    rebate: float = 0.0,
):
    fixture = make_dual_book_fixture()
    lifecycle.submit_intent(intent, fixture.decision_context, occurred_at=_at(at_ms - 2))
    lifecycle.arrive(intent.intent_id, occurred_at=_at(at_ms - 1))
    execution.register_intent(intent, fixture.decision_context)
    result = execution.execute(intent.intent_id, executed_at=_at(at_ms))
    lifecycle.record_execution(result)
    return result, ledger.record_execution(result, fee=fee, rebate=rebate)


def test_declared_mapping_reconstructs_q_h_pending_exposure_and_fill_costs():
    fixture = make_dual_book_fixture()
    lifecycle = _lifecycle()
    ledger = DualLegLedger(fixture.decision_context.run_id, _mapping(2.0, 3.0), lifecycle)

    maker_event = _record_maker_fill(lifecycle, ledger, _maker("maker-buy", 3), 3, at_ms=3, fee=1.25, rebate=0.25)
    assert maker_event is not None and maker_event.position_delta == 3

    execution = _execution()
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 2),), asks=()))
    result, hedge_event = _record_hedge_execution(
        lifecycle,
        execution,
        ledger,
        _hedge("hedge-sell", 2),
        at_ms=6,
        fee=0.5,
        rebate=0.1,
    )
    assert result.filled_qty == 2 and hedge_event is not None and hedge_event.position_delta == -2

    state = ledger.reconcile()
    assert (state.quoted_position, state.hedge_position) == (3, -2)
    assert (state.pending_hedge_quantity, state.residual_risk) == (0.0, 0.0)
    assert (state.total_fees, state.total_rebates) == (1.75, 0.35)
    assert all(event.hedge_pair.hedge_mapping_version == "1.0.0" for event in ledger.events())


def test_partial_maker_hedge_and_eod_retain_residual_risk_with_terminal_eod_disposition():
    fixture = make_dual_book_fixture()
    lifecycle = _lifecycle()
    ledger = DualLegLedger(fixture.decision_context.run_id, _mapping(), lifecycle)
    execution = _execution()
    execution.ingest_book(DepthBook(fixture.quoted_book, bids=(DepthLevel(78000.0, 1),), asks=(DepthLevel(78005.0, 1),)))
    execution.ingest_book(
        DepthBook(
            fixture.hedge_book,
            bids=(DepthLevel(77980.0, 2),),
            asks=(DepthLevel(77985.0, 1),),
        )
    )

    _record_maker_fill(lifecycle, ledger, _maker("maker-partial-route", 5), 4, at_ms=3, fee=0.2)
    hedge_result, _ = _record_hedge_execution(
        lifecycle,
        execution,
        ledger,
        _hedge("hedge-partial-route", 4),
        at_ms=6,
        fee=0.3,
    )
    assert (hedge_result.filled_qty, hedge_result.residual_qty) == (2, 2)
    before_eod = ledger.reconcile()
    assert (before_eod.quoted_position, before_eod.hedge_position, before_eod.residual_risk) == (4, -2, 2.0)

    eod = EodCompletionService(lifecycle, execution, ledger)
    completion = eod.complete(
        EodCloseRequest(
            "eod-partial",
            fixture.decision_context,
            {fixture.quoted_spec.product: 77995.0, fixture.hedge_spec.product: 77990.0},
        ),
        executed_at=_at(10),
        fees_by_product={fixture.quoted_spec.product: 1.0, fixture.hedge_spec.product: 0.5},
    )

    assert completion.disposition is EodDisposition.INCOMPLETE_LIQUIDITY
    assert completion.cancelled_intent_ids == ("maker-partial-route", "hedge-partial-route")
    assert len(completion.execution_ids) == 2
    assert (completion.residual_quoted_position, completion.residual_hedge_position, completion.residual_risk) == (3, -1, 2.0)
    assert lifecycle.state_of("eod-partial:quoted") is IntentLifecycleState.FAILED
    assert lifecycle.state_of("eod-partial:hedge") is IntentLifecycleState.FAILED
    after_eod = ledger.reconcile()
    assert (after_eod.total_fees, after_eod.total_rebates) == (2.0, 0.0)


def test_eod_cancels_live_orders_closes_only_ledger_inventory_and_cannot_double_close():
    fixture = make_dual_book_fixture()
    lifecycle = _lifecycle()
    ledger = DualLegLedger(fixture.decision_context.run_id, _mapping(), lifecycle)
    execution = _execution()
    execution.ingest_book(DepthBook(fixture.quoted_book, bids=(DepthLevel(78000.0, 2),), asks=(DepthLevel(78005.0, 1),)))
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 1),), asks=(DepthLevel(77985.0, 1),)))

    _record_maker_fill(lifecycle, ledger, _maker("maker-filled-before-eod", 2), 2, at_ms=3)
    live_maker = _maker("maker-live-before-eod", 1)
    lifecycle.submit_intent(live_maker, fixture.decision_context, occurred_at=_at(4), envelope_id="quoted-maker-cap")
    lifecycle.arrive(live_maker.intent_id, occurred_at=_at(5))
    assert lifecycle.reserved_qty("quoted-maker-cap") == 1

    eod = EodCompletionService(lifecycle, execution, ledger)
    request = EodCloseRequest(
        "eod-flat",
        fixture.decision_context,
        {fixture.quoted_spec.product: 77995.0, fixture.hedge_spec.product: 77990.0},
    )
    completion = eod.complete(request, executed_at=_at(8))
    assert completion.disposition is EodDisposition.FLAT
    assert completion.cancelled_intent_ids == (live_maker.intent_id,)
    assert completion.residual_quoted_position == completion.residual_hedge_position == 0
    assert lifecycle.state_of(live_maker.intent_id) is IntentLifecycleState.CANCELLED
    assert lifecycle.reserved_qty("quoted-maker-cap") == 0
    event_count = len(ledger.events())

    try:
        eod.complete(request, executed_at=_at(9))
    except FoundationContractError as exc:
        assert "already been claimed" in str(exc)
    else:
        raise AssertionError("EOD must not double-close a declared close request")
    assert len(ledger.events()) == event_count


def test_ledger_rejects_unattached_or_duplicate_execution_effects():
    fixture = make_dual_book_fixture()
    lifecycle = _lifecycle()
    ledger = DualLegLedger(fixture.decision_context.run_id, _mapping(), lifecycle)
    execution = _execution()
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 1),), asks=()))
    hedge = _hedge("ledger-idempotency", 1)
    lifecycle.submit_intent(hedge, fixture.decision_context, occurred_at=_at(1))
    lifecycle.arrive(hedge.intent_id, occurred_at=_at(2))
    execution.register_intent(hedge, fixture.decision_context)
    result = execution.execute(hedge.intent_id, executed_at=_at(3))
    try:
        ledger.record_execution(result)
    except FoundationContractError as exc:
        assert "not accepted by the lifecycle" in str(exc)
    else:
        raise AssertionError("ledger must not accept an execution that lifecycle has not attached")

    lifecycle.record_execution(result)
    ledger.record_execution(result)
    try:
        ledger.record_execution(result)
    except FoundationContractError as exc:
        assert "already recorded" in str(exc)
    else:
        raise AssertionError("ledger must record an execution effect exactly once")
    assert ledger.reconcile().hedge_position == -1


def test_eod_catchup_recovers_unrecorded_position_effect_without_inferred_costs():
    fixture = make_dual_book_fixture()
    lifecycle = _lifecycle()
    ledger = DualLegLedger(fixture.decision_context.run_id, _mapping(), lifecycle)
    execution = _execution()
    execution.ingest_book(DepthBook(fixture.quoted_book, bids=(), asks=(DepthLevel(78005.0, 1),)))
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(), asks=(DepthLevel(77985.0, 1),)))

    maker = _maker("maker-catchup", 2)
    lifecycle.submit_intent(maker, fixture.decision_context, occurred_at=_at(1), envelope_id="quoted-maker-cap")
    lifecycle.arrive(maker.intent_id, occurred_at=_at(2))
    lifecycle.record_passive_fill(maker.intent_id, 1, occurred_at=_at(3))
    assert ledger.events() == ()

    completion = EodCompletionService(lifecycle, execution, ledger).complete(
        EodCloseRequest(
            "eod-catchup",
            fixture.decision_context,
            {fixture.quoted_spec.product: 77995.0, fixture.hedge_spec.product: 77990.0},
        ),
        executed_at=_at(6),
    )
    state = ledger.reconcile()
    assert completion.disposition is EodDisposition.INCOMPLETE_LIQUIDITY
    assert (state.quoted_position, state.total_fees, state.total_rebates) == (1, 0.0, 0.0)
    assert ledger.events()[0].attributes["effect_source"] == "maker_lifecycle_fill"


def test_eod_preflights_execution_model_before_claiming_id_or_cancelling_orders():
    fixture = make_dual_book_fixture()
    lifecycle = _lifecycle()
    ledger = DualLegLedger(fixture.decision_context.run_id, _mapping(), lifecycle)
    execution = _execution()
    execution.ingest_book(DepthBook(fixture.quoted_book, bids=(DepthLevel(78000.0, 1),), asks=(DepthLevel(78005.0, 1),)))
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(), asks=(DepthLevel(77985.0, 1),)))
    _record_maker_fill(lifecycle, ledger, _maker("maker-before-model-preflight", 1), 1, at_ms=3)
    live_maker = _maker("maker-remains-live", 1)
    lifecycle.submit_intent(live_maker, fixture.decision_context, occurred_at=_at(4), envelope_id="quoted-maker-cap")

    eod = EodCompletionService(lifecycle, execution, ledger)
    bad_request = EodCloseRequest(
        "eod-model-preflight",
        fixture.decision_context,
        {fixture.quoted_spec.product: 77995.0, fixture.hedge_spec.product: 77990.0},
        ExecutionModelRef("not-configured", "1.0.0"),
    )
    try:
        eod.complete(bad_request, executed_at=_at(6))
    except FoundationContractError as exc:
        assert "unconfigured execution model" in str(exc)
    else:
        raise AssertionError("unconfigured EOD execution model must fail before lifecycle mutation")
    assert lifecycle.open_intents(fixture.hedge_pair) == (live_maker,)
    assert lifecycle.reserved_qty("quoted-maker-cap") == 1

    completion = eod.complete(
        EodCloseRequest(
            "eod-model-preflight",
            fixture.decision_context,
            {fixture.quoted_spec.product: 77995.0, fixture.hedge_spec.product: 77990.0},
        ),
        executed_at=_at(7),
    )
    assert completion.disposition is EodDisposition.FLAT
    assert lifecycle.state_of(live_maker.intent_id) is IntentLifecycleState.CANCELLED
