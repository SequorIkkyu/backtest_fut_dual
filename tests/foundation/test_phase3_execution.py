"""Phase-3 acceptance tests for the registered aggressive execution service."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from common.execution import DepthBook, DepthExecutionService, DepthLevel
from common.foundation_contracts import (
    BookSnapshotRef,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    OrderIntent,
    OrderRole,
    OrderSide,
)
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def _model(rate: float, model_id: str = "depth-participation") -> ExecutionModelConfig:
    return ExecutionModelConfig(model_id, "1.0.0", rate)


def _service(*models: ExecutionModelConfig) -> DepthExecutionService:
    fixture = make_dual_book_fixture()
    return DepthExecutionService(
        (fixture.quoted_spec, fixture.hedge_spec),
        models,
        ExecutionModelRef(models[0].model_id, models[0].version),
    )


def _intent(
    intent_id: str,
    *,
    side: OrderSide,
    qty: int,
    limit: float,
    model_ref: ExecutionModelRef | None = None,
    role: OrderRole = OrderRole.HEDGE,
) -> OrderIntent:
    fixture = make_dual_book_fixture()
    product = fixture.hedge_spec.product if role is OrderRole.HEDGE else fixture.quoted_spec.product
    return OrderIntent(
        intent_id,
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        product,
        role,
        side,
        qty,
        limit,
        model_ref,
    )


def _execute_at():
    return make_dual_book_fixture().decision_context.dec_ts + timedelta(milliseconds=1)


def test_off_touch_marketable_buy_and_sell_report_touch_and_vwap_not_limit():
    fixture = make_dual_book_fixture()
    model = _model(1.0)

    buy_service = _service(model)
    buy_service.ingest_book(
        DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 10),), asks=(DepthLevel(77985.0, 10),))
    )
    buy = _intent("buy-off-touch", side=OrderSide.BUY, qty=3, limit=77995.0)
    buy_service.register_intent(buy, fixture.decision_context)
    buy_result = buy_service.execute(buy.intent_id, executed_at=_execute_at(), decision_mid=77982.5)
    assert buy_result.executable_touch == buy_result.vwap == 77985.0
    assert buy_result.vwap != buy_result.limit_price == buy.limit_price and buy_result.cost_vs_decision_mid == 2.5

    sell_service = _service(model)
    sell_service.ingest_book(
        DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 10),), asks=(DepthLevel(77985.0, 10),))
    )
    sell = _intent("sell-off-touch", side=OrderSide.SELL, qty=3, limit=77970.0)
    sell_service.register_intent(sell, fixture.decision_context)
    sell_result = sell_service.execute(sell.intent_id, executed_at=_execute_at(), decision_mid=77982.5)
    assert sell_result.executable_touch == sell_result.vwap == 77980.0
    assert sell_result.vwap != sell_result.limit_price == sell.limit_price and sell_result.cost_vs_decision_mid == 2.5


def test_consumed_depth_is_removed_once_and_second_intent_sees_only_remaining_depth():
    fixture = make_dual_book_fixture()
    service = _service(_model(1.0))
    service.ingest_book(
        DepthBook(
            fixture.hedge_book,
            bids=(DepthLevel(77980.0, 10),),
            asks=(DepthLevel(77985.0, 5), DepthLevel(77990.0, 5)),
        )
    )
    first = _intent("first-sweep", side=OrderSide.BUY, qty=4, limit=77990.0)
    second = _intent("second-sweep", side=OrderSide.BUY, qty=4, limit=77990.0)
    service.register_intent(first, fixture.decision_context)
    service.register_intent(second, fixture.decision_context)

    first_result = service.execute(first.intent_id, executed_at=_execute_at())
    second_result = service.execute(second.intent_id, executed_at=_execute_at())

    assert [(level.price, level.quantity) for level in first_result.levels] == [(77985.0, 4)]
    assert [(level.price, level.quantity) for level in second_result.levels] == [(77985.0, 1), (77990.0, 3)]
    assert [(level.price, level.quantity) for level in service.remaining_depth(fixture.hedge_spec.product, OrderSide.BUY)] == [
        (77985.0, 0),
        (77990.0, 2),
    ]
    try:
        service.execute(first.intent_id, executed_at=_execute_at())
    except FoundationContractError as exc:
        assert "already been executed" in str(exc)
    else:
        raise AssertionError("one registered aggressive intent must not consume depth twice")


def test_other_product_book_update_cannot_restore_consumed_hedge_depth():
    fixture = make_dual_book_fixture()
    service = _service(_model(1.0))
    service.ingest_book(DepthBook(fixture.quoted_book, bids=(DepthLevel(78000.0, 10),), asks=(DepthLevel(78005.0, 10),)))
    service.ingest_book(DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 5),), asks=(DepthLevel(77985.0, 5),)))
    first = _intent("hedge-consume", side=OrderSide.BUY, qty=5, limit=77985.0)
    service.register_intent(first, fixture.decision_context)
    assert service.execute(first.intent_id, executed_at=_execute_at()).filled_qty == 5

    newer_quote = BookSnapshotRef(
        fixture.quoted_spec.product,
        2,
        fixture.decision_context.feed_seq + 1,
        "quoted-book-2",
        BASE_TS + timedelta(milliseconds=6),
        BASE_TS + timedelta(milliseconds=6),
        "quoted-snapshot-2",
        "sha256:quoted-2",
    )
    service.ingest_book(DepthBook(newer_quote, bids=(DepthLevel(78000.0, 20),), asks=(DepthLevel(78005.0, 20),)))
    second = _intent("hedge-after-quoted-update", side=OrderSide.BUY, qty=1, limit=77985.0)
    service.register_intent(second, fixture.decision_context)
    result = service.execute(second.intent_id, executed_at=_execute_at())
    assert result.status.value == "no_liquidity" and result.filled_qty == 0 and result.residual_qty == 1


def test_execution_uses_latest_causally_available_book_at_arrival_and_keeps_decision_mark():
    fixture = make_dual_book_fixture()
    service = _service(_model(1.0))
    intent = _intent("arrival-time-hedge", side=OrderSide.BUY, qty=2, limit=77990.0)
    service.register_intent(intent, fixture.decision_context)
    newer_book = BookSnapshotRef(
        fixture.hedge_spec.product,
        2,
        fixture.decision_context.feed_seq + 1,
        "hedge-book-arrival",
        BASE_TS + timedelta(milliseconds=6),
        BASE_TS + timedelta(milliseconds=6),
        "hedge-snapshot-arrival",
        "sha256:hedge-arrival",
    )
    service.ingest_book(DepthBook(newer_book, bids=(DepthLevel(77975.0, 2),), asks=(DepthLevel(77980.0, 2),)))

    result = service.execute(
        intent.intent_id,
        executed_at=BASE_TS + timedelta(milliseconds=7),
        decision_mid=77985.0,
        execution_feed_seq=fixture.decision_context.feed_seq + 1,
    )

    assert result.status.value == "filled"
    assert result.book_snapshot == newer_book
    assert result.decision_book_snapshot == fixture.hedge_book
    assert result.execution_feed_seq == fixture.decision_context.feed_seq + 1
    assert result.vwap == 77980.0
    assert result.cost_vs_decision_mid == -5.0


def test_execution_rejects_a_book_not_yet_available_at_arrival():
    fixture = make_dual_book_fixture()
    service = _service(_model(1.0))
    intent = _intent("future-book-hedge", side=OrderSide.BUY, qty=1, limit=77990.0)
    service.register_intent(intent, fixture.decision_context)
    future_book = BookSnapshotRef(
        fixture.hedge_spec.product,
        2,
        fixture.decision_context.feed_seq + 1,
        "hedge-book-future",
        BASE_TS + timedelta(milliseconds=9),
        BASE_TS + timedelta(milliseconds=9),
        "hedge-snapshot-future",
        "sha256:hedge-future",
    )
    service.ingest_book(DepthBook(future_book, bids=(DepthLevel(77975.0, 1),), asks=(DepthLevel(77980.0, 1),)))

    result = service.execute(intent.intent_id, executed_at=BASE_TS + timedelta(milliseconds=7))

    assert result.status.value == "stale"
    assert result.disposition_reason == "active_book_not_available_at_execution"
    assert result.filled_qty == 0


def test_partial_empty_gapped_crossed_and_zero_depth_books_have_explicit_outcomes():
    fixture = make_dual_book_fixture()

    partial_service = _service(_model(1.0))
    partial_service.ingest_book(DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 3),), bids=()))
    partial = _intent("partial", side=OrderSide.BUY, qty=5, limit=77985.0)
    partial_service.register_intent(partial, fixture.decision_context)
    partial_result = partial_service.execute(partial.intent_id, executed_at=_execute_at())
    assert partial_result.status.value == "partial" and (partial_result.filled_qty, partial_result.residual_qty) == (3, 2)

    gap_service = _service(_model(1.0))
    gap_service.ingest_book(
        DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 2), DepthLevel(78005.0, 10)), bids=())
    )
    gapped = _intent("gapped", side=OrderSide.BUY, qty=5, limit=77990.0)
    gap_service.register_intent(gapped, fixture.decision_context)
    assert gap_service.execute(gapped.intent_id, executed_at=_execute_at()).status.value == "partial"

    empty_service = _service(_model(1.0))
    empty_service.ingest_book(DepthBook(fixture.hedge_book, asks=(), bids=()))
    empty = _intent("empty", side=OrderSide.BUY, qty=1, limit=77985.0)
    empty_service.register_intent(empty, fixture.decision_context)
    assert empty_service.execute(empty.intent_id, executed_at=_execute_at()).status.value == "no_liquidity"

    zero_service = _service(_model(1.0))
    zero_service.ingest_book(DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 0),), bids=()))
    zero = _intent("zero", side=OrderSide.BUY, qty=1, limit=77985.0)
    zero_service.register_intent(zero, fixture.decision_context)
    assert zero_service.execute(zero.intent_id, executed_at=_execute_at()).status.value == "no_liquidity"

    rejected_service = _service(_model(1.0))
    rejected_service.ingest_book(DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 5),), bids=()))
    rejected = _intent("limit-rejected", side=OrderSide.BUY, qty=1, limit=77980.0)
    rejected_service.register_intent(rejected, fixture.decision_context)
    rejected_result = rejected_service.execute(rejected.intent_id, executed_at=_execute_at())
    assert rejected_result.status.value == "rejected" and rejected_result.residual_qty == 1

    crossed_service = _service(_model(1.0))
    crossed_service.ingest_book(
        DepthBook(fixture.hedge_book, bids=(DepthLevel(77985.0, 5),), asks=(DepthLevel(77985.0, 5),))
    )
    crossed = _intent("crossed", side=OrderSide.BUY, qty=1, limit=77985.0)
    crossed_service.register_intent(crossed, fixture.decision_context)
    crossed_result = crossed_service.execute(crossed.intent_id, executed_at=_execute_at())
    assert crossed_result.status.value == "failed" and crossed_result.disposition_reason == "crossed_book"


def test_participation_is_resolved_from_the_versioned_model_and_reported():
    fixture = make_dual_book_fixture()
    half = _model(0.5, "half-depth")
    full = _model(1.0, "full-depth")
    service = _service(half, full)
    service.ingest_book(DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 10),), bids=()))
    intent = _intent(
        "half-participation",
        side=OrderSide.BUY,
        qty=10,
        limit=77985.0,
        model_ref=ExecutionModelRef(full.model_id, full.version),
    )
    service.register_intent(intent, fixture.decision_context)
    result = service.execute(intent.intent_id, executed_at=_execute_at())
    assert result.filled_qty == 10 and result.participation_rate == 1.0
    assert result.execution_model_ref == ExecutionModelRef("full-depth", "1.0.0")

    half_service = _service(half)
    half_service.ingest_book(DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 10),), bids=()))
    default_intent = _intent("default-half", side=OrderSide.BUY, qty=10, limit=77985.0)
    half_service.register_intent(default_intent, fixture.decision_context)
    half_result = half_service.execute(default_intent.intent_id, executed_at=_execute_at())
    assert (half_result.filled_qty, half_result.residual_qty, half_result.participation_rate) == (5, 5, 0.5)


def test_eod_intent_uses_the_same_registered_depth_execution_service():
    fixture = make_dual_book_fixture()
    service = _service(_model(1.0))
    service.ingest_book(DepthBook(fixture.quoted_book, asks=(DepthLevel(78005.0, 2),), bids=(DepthLevel(78000.0, 2),)))
    eod = _intent("eod-close", role=OrderRole.EOD, side=OrderSide.SELL, qty=2, limit=77995.0)
    service.register_intent(eod, fixture.decision_context)
    result = service.execute(eod.intent_id, executed_at=_execute_at())
    assert result.status.value == "filled"
    assert result.product == fixture.quoted_spec.product
    assert result.book_snapshot == fixture.quoted_book


def test_unknown_or_mismatched_intents_and_results_fail_before_depth_mutates():
    fixture = make_dual_book_fixture()
    model = _model(1.0)
    service = _service(model)
    service.ingest_book(DepthBook(fixture.hedge_book, asks=(DepthLevel(77985.0, 5),), bids=()))
    before = service.remaining_depth(fixture.hedge_spec.product, OrderSide.BUY)
    try:
        service.execute("unknown", executed_at=_execute_at())
    except FoundationContractError as exc:
        assert "registered" in str(exc)
    else:
        raise AssertionError("unknown intent must be rejected before mutation")
    assert service.remaining_depth(fixture.hedge_spec.product, OrderSide.BUY) == before

    unknown_model = _intent(
        "unknown-model", side=OrderSide.BUY, qty=1, limit=77985.0, model_ref=ExecutionModelRef("missing", "1")
    )
    try:
        service.register_intent(unknown_model, fixture.decision_context)
    except FoundationContractError as exc:
        assert "unconfigured" in str(exc)
    else:
        raise AssertionError("unknown execution model must be rejected before mutation")
    assert service.remaining_depth(fixture.hedge_spec.product, OrderSide.BUY) == before

    intent = _intent("registered", side=OrderSide.BUY, qty=1, limit=77985.0)
    service.register_intent(intent, fixture.decision_context)
    result = service.execute(intent.intent_id, executed_at=_execute_at())
    mismatched = replace(result, execution_model_ref=ExecutionModelRef("other", "1.0.0"))
    try:
        service.validate_result(mismatched)
    except FoundationContractError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("a mismatched result must be rejected")
    backdated = replace(result, executed_at=fixture.decision_context.dec_ts - timedelta(milliseconds=1))
    try:
        service.validate_result(backdated)
    except FoundationContractError as exc:
        assert "must not precede" in str(exc)
    else:
        raise AssertionError("a pre-decision external execution result must be rejected")


def test_register_intent_rejects_context_product_missing_from_execution_specs():
    fixture = make_dual_book_fixture()
    model = _model(1.0)
    service = DepthExecutionService(
        (fixture.hedge_spec,),
        (model,),
        ExecutionModelRef(model.model_id, model.version),
    )
    eod = _intent("quote-without-spec", role=OrderRole.EOD, side=OrderSide.SELL, qty=1, limit=78000.0)
    try:
        service.register_intent(eod, fixture.decision_context)
    except FoundationContractError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("a context product missing from execution specs must fail at registration")


def test_synthetic_depth_refresh_configuration_is_rejected_until_a_declared_model_exists():
    fixture = make_dual_book_fixture()
    synthetic = ExecutionModelConfig("synthetic", "1.0.0", 1.0, allow_synthetic_depth_refresh=True)
    try:
        DepthExecutionService(
            (fixture.quoted_spec, fixture.hedge_spec),
            (synthetic,),
            ExecutionModelRef(synthetic.model_id, synthetic.version),
        )
    except FoundationContractError as exc:
        assert "synthetic depth refresh" in str(exc)
    else:
        raise AssertionError("Phase 3 must not silently enable synthetic depth refresh")
