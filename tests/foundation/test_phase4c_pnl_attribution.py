"""Phase-4c acceptance tests for canonical PnL attribution and reconciliation."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from common.execution import DepthBook, DepthExecutionService, DepthLevel
from common.foundation_contracts import (
    CapacityEnvelope,
    EodCloseRequest,
    EodCompletion,
    EodDisposition,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    OrderIntent,
    OrderRole,
    OrderSide,
    PnlAccountingView,
    PnlPriceObservation,
    TrialDeclaration,
)
from common.ledger import DualLegLedger, EodCompletionService
from common.lifecycle import IntentLifecycleService
from common.pnl_attribution import PnlAttributionService
from common.telemetry import TelemetryEmitter, load_canonical_table
from common.tests.foundation.fixtures import make_dual_book_fixture


def _at(milliseconds: int):
    return make_dual_book_fixture().decision_context.dec_ts + timedelta(milliseconds=milliseconds)


def _artifacts() -> dict[str, object]:
    return {
        "market_data": "phase4c-market",
        "signal_data": "phase4c-signal",
        "configuration": "phase4c-config",
        "code": "phase4c-code",
        "schema": "telemetry-schema-v0.4",
        "fee_profile": "phase4c-fees",
        "instrument_roll_mapping": "phase4c-roll",
        "execution_models": "full-depth-1.0.0",
    }


def _trial(pair) -> TrialDeclaration:
    return TrialDeclaration(
        "phase4c-trial",
        "2025-01-01:2025-01-31",
        "2025-02-01:2025-02-14",
        "2025-02-15:2025-02-28",
        "frozen-before-holdout",
        "phase4c-policy-v1",
        pair,
        (ExecutionModelRef("full-depth", "1.0.0"),),
        ("fixture-cleaning",),
    )


def _build_ledger_and_telemetry(root: Path):
    fixture = make_dual_book_fixture()
    model = ExecutionModelConfig("full-depth", "1.0.0", 1.0)
    lifecycle = IntentLifecycleService(
        (model,),
        ExecutionModelRef(model.model_id, model.version),
        (CapacityEnvelope("quoted-cap", fixture.hedge_pair, fixture.quoted_spec.product, 5),),
    )
    mapping = HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0)
    ledger = DualLegLedger(fixture.decision_context.run_id, mapping, lifecycle)
    maker = OrderIntent(
        "pnl-maker",
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        1,
        78000.0,
    )
    lifecycle.submit_intent(maker, fixture.decision_context, occurred_at=_at(1), envelope_id="quoted-cap")
    lifecycle.arrive(maker.intent_id, occurred_at=_at(2))
    maker_event = ledger.record_lifecycle(lifecycle.record_passive_fill(maker.intent_id, 1, occurred_at=_at(3)).event, fee=1.0, rebate=0.25)
    assert maker_event is not None

    hedge = OrderIntent(
        "pnl-hedge",
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.SELL,
        1,
        77980.0,
    )
    execution = DepthExecutionService(
        (fixture.quoted_spec, fixture.hedge_spec), (model,), ExecutionModelRef(model.model_id, model.version)
    )
    execution.ingest_book(DepthBook(fixture.hedge_book, bids=(DepthLevel(77980.0, 1),), asks=()))
    lifecycle.submit_intent(hedge, fixture.decision_context, occurred_at=_at(4))
    lifecycle.arrive(hedge.intent_id, occurred_at=_at(5))
    execution.register_intent(hedge, fixture.decision_context)
    result = execution.execute(hedge.intent_id, executed_at=_at(6))
    lifecycle.record_execution(result)
    hedge_event = ledger.record_execution(result, fee=0.5, rebate=0.1)
    assert hedge_event is not None

    emitter = TelemetryEmitter(root, fixture.decision_context.run_id, fixture.hedge_pair)
    emitter.emit_ledger_effect(maker_event)
    emitter.emit_ledger_effect(hedge_event)
    observations = (
        PnlPriceObservation(maker_event.event_id, 78000.0, 78002.0),
        PnlPriceObservation(hedge_event.event_id, 77980.0, 77982.0),
    )
    marks = {fixture.quoted_spec.product: 78004.0, fixture.hedge_spec.product: 77978.0}
    return fixture, ledger, emitter, observations, marks


def _attribute(root: Path, *, accounting_total: float = 28.85, cycle_total: float = 28.85, eod=None):
    fixture, ledger, emitter, observations, marks = _build_ledger_and_telemetry(root)
    result = PnlAttributionService().attribute(
        "phase4c-waterfall",
        ledger,
        observations,
        marks,
        (fixture.quoted_spec, fixture.hedge_spec),
        telemetry_run_dir=emitter.run_dir,
        accounting_view=PnlAccountingView("accounting-v1", accounting_total),
        cycle_view=PnlAccountingView("cycles-v1", cycle_total),
        tolerance=1e-9,
        eod_completion=eod,
    )
    return fixture, ledger, emitter, observations, marks, result


def test_waterfall_uses_each_priced_ledger_effect_once_and_sums_to_independent_totals():
    with TemporaryDirectory() as temporary:
        _, ledger, _, _, _, result = _attribute(Path(temporary))
    assert result.economics_eligible
    assert math.isclose(result.maker_capture, 10.0)
    assert math.isclose(result.quoted_leg_price_pnl, 10.0)
    assert math.isclose(result.hedge_leg_price_pnl, 20.0)
    assert math.isclose(result.hedge_execution_shortfall, 10.0)
    assert math.isclose(result.fees, 1.5)
    assert math.isclose(result.rebates, 0.35)
    assert math.isclose(result.waterfall_total, 28.85)
    assert {effect.ledger_event_id for effect in result.effects} == {event.event_id for event in ledger.events()}
    assert len(result.effects) == len(ledger.events())


def test_residual_basis_is_derived_combined_leg_price_pnl_not_an_extra_waterfall_item():
    with TemporaryDirectory() as temporary:
        _, _, _, _, _, result = _attribute(Path(temporary))
    assert math.isclose(result.residual_basis_pnl, result.quoted_leg_price_pnl + result.hedge_leg_price_pnl)
    assert math.isclose(
        result.waterfall_total,
        result.maker_capture
        + result.quoted_leg_price_pnl
        + result.hedge_leg_price_pnl
        - result.hedge_execution_shortfall
        - result.fees
        + result.rebates,
    )
    assert not math.isclose(result.waterfall_total + result.residual_basis_pnl, result.waterfall_total)


def test_reconciled_waterfall_row_passes_the_telemetry_outcome_invariant():
    with TemporaryDirectory() as temporary:
        fixture, _, emitter, _, _, result = _attribute(Path(temporary))
        emitter.emit_pnl_attribution(result)
        for table in emitter.schema.tables:
            emitter.declare_empty_table(table)
        emitter.capture_provenance(_trial(fixture.hedge_pair), _artifacts())
        telemetry_result = emitter.finalize()
        outcome = next(item for item in telemetry_result.invariants if item.invariant_id == "outcome.economics_eligibility")
        assert outcome.passed


def test_missing_or_duplicate_price_observations_fail_closed_before_a_pnl_claim():
    with TemporaryDirectory() as temporary:
        _, ledger, _, observations, marks = _build_ledger_and_telemetry(Path(temporary))
        try:
            PnlAttributionService().attribute(
                "bad-prices",
                ledger,
                observations[:1],
                marks,
                (make_dual_book_fixture().quoted_spec, make_dual_book_fixture().hedge_spec),
                telemetry_run_dir=Path(temporary) / ledger.run_id,
                accounting_view=PnlAccountingView("accounting-v1", 0.0),
                cycle_view=PnlAccountingView("cycles-v1", 0.0),
            )
        except FoundationContractError as exc:
            assert "cover exactly" in str(exc)
        else:
            raise AssertionError("every ledger effect must have one price observation")


def test_reconciliation_residual_is_emitted_and_makes_the_telemetry_run_ineligible():
    with TemporaryDirectory() as temporary:
        fixture, _, emitter, _, _, result = _attribute(Path(temporary), accounting_total=29.85)
        assert not result.economics_eligible
        assert "accounting:accounting-v1" in result.reconciliation_failures
        emitter.emit_pnl_attribution(result)
        for table in emitter.schema.tables:
            emitter.declare_empty_table(table)
        emitter.capture_provenance(_trial(fixture.hedge_pair), _artifacts())
        telemetry_result = emitter.finalize()
        outcome = next(item for item in telemetry_result.invariants if item.invariant_id == "outcome.economics_eligibility")
        assert not telemetry_result.eligible
        assert not outcome.passed
        rows = tuple(load_canonical_table(emitter.run_dir, "outcome_pnl"))
        assert rows[0]["attribution_status"] == "unreconciled"


def test_forged_reconciled_outcome_row_cannot_bypass_waterfall_validation():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = TelemetryEmitter(Path(temporary), fixture.decision_context.run_id, fixture.hedge_pair)
        emitter.emit_row(
            "outcome_pnl",
            "forged-outcome",
            {"outcome_id": "forged-outcome", "attribution_status": "reconciled", "economics_eligible": True},
        )
        for table in emitter.schema.tables:
            emitter.declare_empty_table(table)
        emitter.capture_provenance(_trial(fixture.hedge_pair), _artifacts())
        result = emitter.finalize()
        outcome = next(item for item in result.invariants if item.invariant_id == "outcome.economics_eligibility")
        assert not result.eligible
        assert not outcome.passed


def test_eod_residual_positions_must_match_the_canonical_ledger_state():
    source_fixture = make_dual_book_fixture()
    bad_eod = EodCompletion(
        "bad-eod",
        source_fixture.decision_context.run_id,
        source_fixture.decision_context.decision_id,
        source_fixture.hedge_pair,
        _at(8),
        EodDisposition.FLAT,
        (),
        (),
        0,
        0,
        0.0,
    )
    with TemporaryDirectory() as temporary:
        fixture, _, _, _, _, result = _attribute(Path(temporary), eod=bad_eod)
    assert fixture.hedge_pair == result.hedge_pair
    assert not result.economics_eligible
    assert "eod:residual_positions" in result.reconciliation_failures


def test_zero_fill_eod_is_reconciled_by_its_residual_inventory_not_a_missing_ledger_effect():
    fixture = make_dual_book_fixture()
    model = ExecutionModelConfig("full-depth", "1.0.0", 1.0)
    lifecycle = IntentLifecycleService(
        (model,),
        ExecutionModelRef(model.model_id, model.version),
        (CapacityEnvelope("quoted-cap", fixture.hedge_pair, fixture.quoted_spec.product, 5),),
    )
    ledger = DualLegLedger(fixture.decision_context.run_id, HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0), lifecycle)
    maker = OrderIntent(
        "zero-eod-maker",
        fixture.decision_context.run_id,
        fixture.decision_context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        1,
        78000.0,
    )
    lifecycle.submit_intent(maker, fixture.decision_context, occurred_at=_at(1), envelope_id="quoted-cap")
    lifecycle.arrive(maker.intent_id, occurred_at=_at(2))
    maker_event = ledger.record_lifecycle(lifecycle.record_passive_fill(maker.intent_id, 1, occurred_at=_at(3)).event)
    assert maker_event is not None
    execution = DepthExecutionService(
        (fixture.quoted_spec, fixture.hedge_spec), (model,), ExecutionModelRef(model.model_id, model.version)
    )
    execution.ingest_book(DepthBook(fixture.quoted_book, bids=(), asks=()))
    completion = EodCompletionService(lifecycle, execution, ledger).complete(
        EodCloseRequest(
            "zero-fill-eod",
            fixture.decision_context,
            {fixture.quoted_spec.product: 77995.0, fixture.hedge_spec.product: 77990.0},
        ),
        executed_at=_at(6),
    )
    assert completion.disposition is EodDisposition.INCOMPLETE_LIQUIDITY
    assert len(completion.execution_ids) == 1
    assert all(event.source_event_id not in completion.execution_ids for event in ledger.events())

    with TemporaryDirectory() as temporary:
        emitter = TelemetryEmitter(Path(temporary), fixture.decision_context.run_id, fixture.hedge_pair)
        emitter.emit_ledger_effect(maker_event)
        result = PnlAttributionService().attribute(
            "zero-fill-eod-pnl",
            ledger,
            (PnlPriceObservation(maker_event.event_id, 78000.0, 78001.0),),
            {fixture.quoted_spec.product: 78002.0, fixture.hedge_spec.product: 77980.0},
            (fixture.quoted_spec, fixture.hedge_spec),
            telemetry_run_dir=emitter.run_dir,
            accounting_view=PnlAccountingView("accounting-zero-eod", 10.0),
            cycle_view=PnlAccountingView("cycles-zero-eod", 10.0),
            eod_completion=completion,
        )
    assert result.economics_eligible
    assert result.eod_reconciled
