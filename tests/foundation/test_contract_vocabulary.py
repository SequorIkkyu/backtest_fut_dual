"""Phase-0 contract vocabulary tests; no production engine behaviour is exercised here."""

from __future__ import annotations

from collections.abc import MutableMapping, MutableSequence, MutableSet
from dataclasses import FrozenInstanceError
from datetime import datetime

import pandas as pd

from common.foundation_contracts import (
    BookSnapshotRef,
    CapacityReservationEvent,
    DecisionContext,
    ExecutionLevel,
    ExecutionModelRef,
    ExecutionResult,
    ExecutionStatus,
    FOUNDATION_CONTRACT_VERSION,
    FoundationContractError,
    HedgePairRef,
    InvariantResult,
    InvariantSeverity,
    LedgerEvent,
    LedgerLeg,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderRole,
    OrderSide,
    ReservationAction,
    S0_DUAL_BOOK_IDENTITY_FIELDS,
    S0_TELEMETRY_TABLES,
    TELEMETRY_SCHEMA_VERSION,
    TelemetrySchema,
)
from common import foundation_contracts
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def test_contract_versions_and_s0_tables_are_explicit():
    assert FOUNDATION_CONTRACT_VERSION == "0.13.0"
    assert TELEMETRY_SCHEMA_VERSION == "0.6.0"
    schema = TelemetrySchema()
    assert schema.tables == S0_TELEMETRY_TABLES
    assert schema.dual_book_identity_fields == S0_DUAL_BOOK_IDENTITY_FIELDS
    assert "ExchangeBatchRef" in foundation_contracts.__all__
    assert "OrderPricingReference" in foundation_contracts.__all__


def test_fixture_context_is_causal_immutable_and_dual_book_scoped():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    assert all(event.recv_ts <= context.dec_ts for event in fixture.events)
    assert fixture.signal_available_at <= context.dec_ts < fixture.action_arrival_at
    assert context.hedge_pair == fixture.hedge_pair
    assert context.quoted_product == fixture.hedge_pair.quoted_product
    assert context.hedge_product == fixture.hedge_pair.hedge_product
    assert context.quoted_book == fixture.quoted_book
    assert context.hedge_book == fixture.hedge_book
    assert context.quoted_book.snapshot_id == "quoted-snapshot-1"
    assert context.hedge_book.snapshot_hash == "sha256:hedge-1"
    mutable_policy_values = (MutableMapping, MutableSequence, MutableSet, pd.DataFrame, pd.Series)
    assert all(not isinstance(value, mutable_policy_values) for value in context.__dict__.values())
    try:
        context.input_ages_ms["signal"] = 99.0
    except TypeError:
        pass
    else:
        raise AssertionError("DecisionContext input ages must be immutable")
    try:
        fixture.quoted_spec.product = "mutated"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("InstrumentSpec must be immutable")
    try:
        fixture.hedge_pair.pair_id = "mutated"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("HedgePairRef must be immutable")
    try:
        context.quoted_book.snapshot_id = "mutated"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("BookSnapshotRef must be immutable")


def test_dual_book_vocabulary_rejects_invalid_pair_or_leg_binding():
    fixture = make_dual_book_fixture()
    try:
        HedgePairRef("bad-pair", "ZN-main", "ZN-main", "calendar-spread", "1.0.0")
    except FoundationContractError as exc:
        assert "distinct" in str(exc)
    else:
        raise AssertionError("A hedge pair must contain two products")

    context_kwargs = dict(fixture.decision_context.__dict__)
    context_kwargs["quoted_product"] = fixture.hedge_spec.product
    try:
        DecisionContext(**context_kwargs)
    except FoundationContractError as exc:
        assert "hedge_pair" in str(exc)
    else:
        raise AssertionError("Decision context products must match the pair reference")

    context_kwargs = dict(fixture.decision_context.__dict__)
    context_kwargs["quoted_book"] = BookSnapshotRef(
        fixture.hedge_spec.product,
        1,
        1,
        "wrong-quoted-event",
        BASE_TS,
        BASE_TS,
        "wrong-quoted-snapshot",
        "sha256:wrong",
    )
    try:
        DecisionContext(**context_kwargs)
    except FoundationContractError as exc:
        assert "quoted_book product" in str(exc)
    else:
        raise AssertionError("A book snapshot reference must belong to its context leg")

    try:
        OrderIntent(
            "bad-maker",
            fixture.decision_context.run_id,
            fixture.decision_context.decision_id,
            fixture.hedge_pair,
            fixture.hedge_spec.product,
            OrderRole.MAKER,
            OrderSide.BUY,
            1,
            77985.0,
        )
    except FoundationContractError as exc:
        assert "maker intent product" in str(exc)
    else:
        raise AssertionError("Maker intent must use the quoted product")


def test_decision_context_rejects_naive_clock_or_negative_sequence():
    fixture = make_dual_book_fixture()
    kwargs = dict(fixture.decision_context.__dict__)
    kwargs["dec_ts"] = datetime(2025, 1, 2, 9, 0, 0)
    try:
        DecisionContext(**kwargs)
    except FoundationContractError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("Naive decision time must be rejected")
    kwargs["dec_ts"] = BASE_TS
    kwargs["feed_seq"] = -1
    try:
        DecisionContext(**kwargs)
    except FoundationContractError as exc:
        assert "feed_seq" in str(exc)
    else:
        raise AssertionError("Negative feed sequence must be rejected")


def test_dual_leg_vocabulary_has_traceable_intent_execution_reservation_and_ledger_events():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    maker_intent = OrderIntent(
        "intent-maker-1",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        5,
        78000.0,
    )
    hedge_intent = OrderIntent(
        "intent-hedge-1",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.BUY,
        5,
        77990.0,
        ExecutionModelRef(fixture.execution_model.model_id, fixture.execution_model.version),
    )
    result = ExecutionResult(
        "execution-1",
        hedge_intent.intent_id,
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        hedge_intent.product,
        OrderSide.BUY,
        ExecutionStatus.PARTIAL,
        requested_qty=5,
        filled_qty=3,
        residual_qty=2,
        executed_at=fixture.action_arrival_at,
        execution_model_ref=ExecutionModelRef(fixture.execution_model.model_id, fixture.execution_model.version),
        participation_rate=fixture.execution_model.participation_rate,
        decision_feed_seq=context.feed_seq,
        execution_feed_seq=context.feed_seq,
        book_snapshot=context.hedge_book,
        limit_price=hedge_intent.limit_price,
        levels=(ExecutionLevel(77985.0, 2), ExecutionLevel(77990.0, 1)),
        executable_touch=77985.0,
        vwap=(2 * 77985.0 + 77990.0) / 3,
        decision_mid=77982.5,
        cost_vs_decision_mid=4.1666666667,
    )
    reserve = CapacityReservationEvent(
        "reserve-1",
        context.run_id,
        context.decision_id,
        maker_intent.intent_id,
        fixture.hedge_pair,
        maker_intent.product,
        "maker-cap-v1",
        ReservationAction.RESERVE,
        5.0,
        BASE_TS,
    )
    ledger = LedgerEvent(
        "ledger-1",
        context.run_id,
        context.decision_id,
        result.execution_id,
        fixture.hedge_pair,
        LedgerLeg.HEDGE,
        hedge_intent.product,
        result.filled_qty,
        BASE_TS,
    )
    assert result.residual_qty == hedge_intent.requested_qty - result.filled_qty
    assert reserve.amount == maker_intent.requested_qty
    assert ledger.position_delta == result.filled_qty
    assert maker_intent.execution_model_ref is None
    assert hedge_intent.execution_model_ref == ExecutionModelRef("depth-participation", "0.1.0")
    assert {maker_intent.hedge_pair, hedge_intent.hedge_pair, result.hedge_pair, reserve.hedge_pair, ledger.hedge_pair} == {
        fixture.hedge_pair
    }


def test_s0_policy_batch_is_immutable_and_binds_only_a_maker_envelope():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    maker = OrderIntent(
        "batch-maker",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        1,
        78000.0,
    )
    hedge = OrderIntent(
        "batch-hedge",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.SELL,
        1,
        77980.0,
    )
    batch = MakerHedgeIntentBatch(maker, hedge, "quoted-cap")
    assert batch.maker_intent is maker and batch.hedge_intent is hedge
    assert MakerHedgeIntentBatch() == MakerHedgeIntentBatch()
    try:
        MakerHedgeIntentBatch(None, hedge, "quoted-cap")
    except FoundationContractError as exc:
        assert "requires maker_intent" in str(exc)
    else:
        raise AssertionError("a hedge declaration must not carry a maker capacity envelope")
    try:
        MakerHedgeIntentBatch(hedge, None, "quoted-cap")
    except FoundationContractError as exc:
        assert "maker OrderIntent" in str(exc)
    else:
        raise AssertionError("a maker slot must reject a hedge intent")
    duplicate_id_hedge = OrderIntent(
        maker.intent_id,
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.SELL,
        1,
        77980.0,
    )
    try:
        MakerHedgeIntentBatch(maker, duplicate_id_hedge, "quoted-cap")
    except FoundationContractError as exc:
        assert "distinct intent IDs" in str(exc)
    else:
        raise AssertionError("one policy batch must not register two roles under one intent ID")


def test_execution_and_ledger_reject_unreconciled_or_mismatched_leg_records():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    try:
        ExecutionResult(
            "execution-1",
            "intent-1",
            context.run_id,
            context.decision_id,
            fixture.hedge_pair,
            fixture.hedge_spec.product,
            OrderSide.BUY,
            ExecutionStatus.PARTIAL,
            5,
            3,
            2,
            fixture.action_arrival_at,
            ExecutionModelRef(fixture.execution_model.model_id, fixture.execution_model.version),
            fixture.execution_model.participation_rate,
            context.feed_seq,
            context.feed_seq,
            context.hedge_book,
            77990.0,
            levels=(ExecutionLevel(100.0, 2),),
            executable_touch=100.0,
            vwap=100.0,
        )
    except FoundationContractError as exc:
        assert "execution-level quantity" in str(exc)
    else:
        raise AssertionError("Execution levels must reconcile to filled quantity")
    try:
        LedgerEvent(
            "ledger-1",
            context.run_id,
            context.decision_id,
            "execution-1",
            fixture.hedge_pair,
            LedgerLeg.HEDGE,
            fixture.quoted_spec.product,
            3,
            fixture.action_arrival_at,
        )
    except FoundationContractError as exc:
        assert "ledger product" in str(exc)
    else:
        raise AssertionError("A ledger leg must match its hedge-pair product")
    result = InvariantResult("causality", False, InvariantSeverity.ERROR, "late signal", ("decision-1",))
    assert not result.passed and result.severity is InvariantSeverity.ERROR


def test_execution_result_status_must_match_fill_and_residual_quantities():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    try:
        ExecutionResult(
            "bad-filled-status",
            "intent-1",
            context.run_id,
            context.decision_id,
            fixture.hedge_pair,
            fixture.hedge_spec.product,
            OrderSide.BUY,
            ExecutionStatus.FILLED,
            5,
            3,
            2,
            fixture.action_arrival_at,
            ExecutionModelRef(fixture.execution_model.model_id, fixture.execution_model.version),
            fixture.execution_model.participation_rate,
            context.feed_seq,
            context.feed_seq,
            context.hedge_book,
            77985.0,
            (ExecutionLevel(77985.0, 3),),
            77985.0,
            77985.0,
        )
    except FoundationContractError as exc:
        assert "filled status" in str(exc)
    else:
        raise AssertionError("a filled execution status must not hide residual quantity")


def test_order_intent_execution_model_reference_is_explicit_or_uses_run_default():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    default_intent = OrderIntent(
        "intent-default-1",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        1,
        78000.0,
    )
    explicit_intent = OrderIntent(
        "intent-explicit-1",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.BUY,
        1,
        77985.0,
        ExecutionModelRef("depth-participation", "0.1.0"),
    )
    assert default_intent.execution_model_ref is None
    assert explicit_intent.execution_model_ref == ExecutionModelRef("depth-participation", "0.1.0")
    try:
        OrderIntent(
            "intent-invalid-model-1",
            context.run_id,
            context.decision_id,
            fixture.hedge_pair,
            fixture.hedge_spec.product,
            OrderRole.HEDGE,
            OrderSide.BUY,
            1,
            77985.0,
            "participation=1.0",
        )
    except FoundationContractError as exc:
        assert "execution_model_ref" in str(exc)
    else:
        raise AssertionError("An intent may reference a model but may not embed a raw execution override")
