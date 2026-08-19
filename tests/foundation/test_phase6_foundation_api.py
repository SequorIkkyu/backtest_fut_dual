"""Phase-6 acceptance tests for the public versioned dual-book foundation API."""

from __future__ import annotations

from dataclasses import fields
from datetime import timedelta
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory

from common.foundation_api import FOUNDATION_API_VERSION, DualBookFoundation
from common.foundation_contracts import (
    CapacityEnvelope,
    EodCloseRequest,
    EodDisposition,
    ExecutionModelConfig,
    ExecutionModelRef,
    ExecutionStatus,
    FoundationContractError,
    HedgeMappingSpec,
    IngressEvent,
    IngressKind,
    IntentLifecycleState,
    MakerHedgeIntentBatch,
    OrderPricingReference,
    OrderIntent,
    OrderRole,
    OrderSide,
)
from common.telemetry import TelemetryEmitter, load_canonical_table
from common.tests.foundation.conformance_client import ConformanceClient
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def _events(pair) -> tuple[IngressEvent, ...]:
    return (
        IngressEvent(
            "phase6-quoted-book",
            pair.quoted_product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=2),
            11,
            {
                "bids": [{"price": 78000.0, "quantity": 2}],
                "asks": [{"price": 78005.0, "quantity": 2}],
            },
        ),
        IngressEvent(
            "phase6-hedge-book",
            pair.hedge_product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=3),
            7,
            {
                "bids": [{"price": 77980.0, "quantity": 2}],
                "asks": [{"price": 77985.0, "quantity": 2}],
            },
        ),
        IngressEvent(
            "phase6-signal",
            pair.quoted_product,
            IngressKind.SIGNAL,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=4),
            3,
            {"signal_id": "phase6-signal", "score": 0.25},
        ),
    )


def _api(
    root: Path,
    run_id: str = "phase6-run",
    *,
    require_exchange_batch_pricing: bool = False,
) -> tuple[DualBookFoundation, object]:
    fixture = make_dual_book_fixture()
    model = ExecutionModelConfig("phase6-depth", "1.0.0", 1.0)
    api = DualBookFoundation(
        run_id=run_id,
        hedge_mapping=HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0),
        instrument_specs=(fixture.quoted_spec, fixture.hedge_spec),
        execution_models=(model,),
        default_execution_model=ExecutionModelRef(model.model_id, model.version),
        capacity_envelopes=(CapacityEnvelope("phase6-quoted-cap", fixture.hedge_pair, fixture.quoted_spec.product, 12),),
        telemetry=TelemetryEmitter(root, run_id, fixture.hedge_pair),
        require_exchange_batch_pricing=require_exchange_batch_pricing,
    )
    return api, fixture.hedge_pair


def _context_ready_api(root: Path, run_id: str = "phase6-guard"):
    from common.ingress import CausalIngress

    api, pair = _api(root, run_id)
    events = _events(pair)
    ingress = CausalIngress(run_id, events)
    tuple(ingress.replay())
    context = ingress.decision_context("phase6-guard-decision", pair, consumed_signal_ids=("phase6-signal",))
    for event in events:
        if event.kind is IngressKind.BOOK:
            api.record_book_event(event, ingress.book_ref_for_event(event.event_id))
    for ref in (context.quoted_book, context.hedge_book):
        api.record_book_snapshot(ref, ingress.book_snapshot(ref))
    for ref in context.consumed_signals:
        api.record_signal_snapshot(ref, ingress.signal_snapshot(ref))
    api.ingest_depth_from_snapshot(context.quoted_book)
    api.ingest_depth_from_snapshot(context.hedge_book)
    return api, pair, context


def _maker(intent_id: str, context, pair, *, model_ref=None) -> OrderIntent:
    return OrderIntent(
        intent_id,
        context.run_id,
        context.decision_id,
        pair,
        pair.quoted_product,
        OrderRole.MAKER,
        OrderSide.BUY,
        1,
        78000.0,
        model_ref,
    )


def test_public_conformance_client_exercises_two_legs_and_finishes_a_green_non_economic_run():
    with TemporaryDirectory() as temporary:
        api, pair = _api(Path(temporary))
        result = ConformanceClient().run(api, run_id="phase6-run", hedge_pair=pair, events=_events(pair))

    assert FOUNDATION_API_VERSION == "1.4.0"
    assert api.api_version == FOUNDATION_API_VERSION
    assert result.maker_state is IntentLifecycleState.PARTIALLY_FILLED
    assert result.first_hedge_result.filled_qty == 1
    assert result.second_hedge_result.filled_qty == 1
    assert result.second_hedge_result.residual_qty == 1
    assert result.eod_disposition.value == "flat"
    assert (result.final_quoted_position, result.final_hedge_position) == (0, 0)
    assert result.telemetry_eligible
    client_source = inspect.getsource(ConformanceClient)
    assert "api._" not in client_source
    assert "._lifecycle" not in client_source and "._execution" not in client_source and "._ledger" not in client_source


def test_policy_receives_only_immutable_context_and_public_api_has_no_fabricated_fill_or_ledger_route():
    with TemporaryDirectory() as temporary:
        api, pair = _api(Path(temporary))
        result = ConformanceClient().run(api, run_id="phase6-run", hedge_pair=pair, events=_events(pair))

    context = result.maker_policy_context
    assert {field.name for field in fields(context)} >= {"quoted_book", "hedge_book", "input_ages_ms"}
    assert not any(isinstance(getattr(context, field.name), (dict, list)) for field in fields(context))
    assert not hasattr(context.quoted_book, "bids")
    try:
        context.input_ages_ms["attempted_mutation"] = 1.0
    except TypeError:
        pass
    else:
        raise AssertionError("policy decision context must not expose a mutable mapping")
    assert not hasattr(api, "attach_execution")
    assert not hasattr(api, "record_ledger_effect")
    assert not hasattr(api, "reserve_capacity")
    try:
        api.execute_hedge("phase6-maker-order", executed_at=BASE_TS + timedelta(seconds=1))
    except FoundationContractError as exc:
        assert "only a registered hedge intent" in str(exc)
    else:
        raise AssertionError("maker intent must not be executed through the hedge route")


def test_public_submit_rejects_mismatched_pair_model_and_capacity_before_registration():
    with TemporaryDirectory() as temporary:
        api, pair, context = _context_ready_api(Path(temporary))
        wrong_pair = type(pair)("other-pair", pair.quoted_product, pair.hedge_product, "other", "1.0.0")
        bad_pair = _maker("phase6-bad-pair", context, wrong_pair)
        try:
            api.submit(MakerHedgeIntentBatch(bad_pair, None, "phase6-quoted-cap"), context, occurred_at=context.dec_ts)
        except FoundationContractError as exc:
            assert "batch intent" in str(exc)
        else:
            raise AssertionError("pair-mismatched intent must be rejected")

        bad_model = _maker(
            "phase6-bad-model",
            context,
            pair,
            model_ref=ExecutionModelRef("unconfigured", "1.0.0"),
        )
        try:
            api.submit(MakerHedgeIntentBatch(bad_model, None, "phase6-quoted-cap"), context, occurred_at=context.dec_ts)
        except FoundationContractError as exc:
            assert "unconfigured" in str(exc)
        else:
            raise AssertionError("unconfigured execution model must be rejected")

        valid = _maker("phase6-bad-envelope", context, pair)
        try:
            api.submit(MakerHedgeIntentBatch(valid, None, "missing-envelope"), context, occurred_at=context.dec_ts)
        except FoundationContractError as exc:
            assert "unknown capacity envelope" in str(exc)
        else:
            raise AssertionError("unknown capacity envelope must be rejected")


def test_public_api_records_every_manual_terminal_failure_disposition():
    terminal_states = (
        IntentLifecycleState.CANCELLED,
        IntentLifecycleState.EXPIRED,
        IntentLifecycleState.REJECTED,
        IntentLifecycleState.STALE,
        IntentLifecycleState.DEADLINE,
        IntentLifecycleState.FAILED,
    )
    with TemporaryDirectory() as temporary:
        api, pair, context = _context_ready_api(Path(temporary))
        for sequence, state in enumerate(terminal_states):
            intent = _maker(f"phase6-terminal-{state.value}", context, pair)
            api.submit(
                MakerHedgeIntentBatch(intent, None, "phase6-quoted-cap"),
                context,
                occurred_at=context.dec_ts + timedelta(milliseconds=sequence * 2),
            )
            terminal = api.terminate(
                intent.intent_id,
                state,
                occurred_at=context.dec_ts + timedelta(milliseconds=sequence * 2 + 1),
                disposition_reason=f"phase6-{state.value}",
            )
            assert terminal is state
            assert api.state_of(intent.intent_id) is state


def test_public_eod_facade_reports_incomplete_liquidity_for_a_retained_empty_book():
    with TemporaryDirectory() as temporary:
        api, pair = _api(Path(temporary), "phase6-empty-eod")
        events = _events(pair) + (
            IngressEvent(
                "phase6-empty-quoted-book",
                pair.quoted_product,
                IngressKind.BOOK,
                BASE_TS + timedelta(milliseconds=5),
                BASE_TS + timedelta(milliseconds=6),
                12,
                {"bids": [], "asks": []},
            ),
            IngressEvent(
                "phase6-empty-hedge-book",
                pair.hedge_product,
                IngressKind.BOOK,
                BASE_TS + timedelta(milliseconds=5),
                BASE_TS + timedelta(milliseconds=6),
                13,
                {
                    "bids": [{"price": 77980.0, "quantity": 2}],
                    "asks": [{"price": 77985.0, "quantity": 2}],
                },
            ),
        )
        from common.ingress import CausalIngress

        ingress = CausalIngress("phase6-empty-eod", events)
        tuple(ingress.replay())
        context = ingress.decision_context("phase6-empty-eod-decision", pair, consumed_signal_ids=("phase6-signal",))
        for event in events:
            if event.kind is IngressKind.BOOK:
                api.record_book_event(event, ingress.book_ref_for_event(event.event_id))
                ref = ingress.book_ref_for_event(event.event_id)
                api.record_book_snapshot(ref, ingress.book_snapshot(ref))
        for ref in context.consumed_signals:
            api.record_signal_snapshot(ref, ingress.signal_snapshot(ref))
        api.ingest_depth_from_snapshot(context.quoted_book)
        api.ingest_depth_from_snapshot(context.hedge_book)

        maker = _maker("phase6-empty-eod-maker", context, pair)
        api.submit(MakerHedgeIntentBatch(maker, None, "phase6-quoted-cap"), context, occurred_at=context.dec_ts)
        api.arrive(maker.intent_id, occurred_at=context.dec_ts + timedelta(milliseconds=1))
        api.record_passive_fill(maker.intent_id, 1, occurred_at=context.dec_ts + timedelta(milliseconds=2))

        completion = api.complete_eod(
            EodCloseRequest(
                "phase6-empty-eod-close",
                context,
                {pair.quoted_product: 77995.0, pair.hedge_product: 77990.0},
            ),
            executed_at=context.dec_ts + timedelta(milliseconds=3),
        )

    assert completion.disposition is EodDisposition.INCOMPLETE_LIQUIDITY
    assert (completion.residual_quoted_position, completion.residual_hedge_position) == (1, 0)
    assert (api.ledger_state().quoted_position, api.ledger_state().hedge_position) == (1, 0)
    result = api.execution_result("phase6-empty-eod-close:quoted")
    assert result is not None and result.status is ExecutionStatus.NO_LIQUIDITY and result.filled_qty == 0
    assert api.state_of("phase6-empty-eod-close:quoted") is IntentLifecycleState.FAILED


def test_exchange_batch_fill_trigger_requires_a_policy_owned_prior_snapshot_price_reference():
    from common.ingress import CausalIngress

    with TemporaryDirectory() as temporary:
        api, pair = _api(Path(temporary), "phase6-pricing", require_exchange_batch_pricing=True)
        events = _events(pair) + (
            IngressEvent(
                "phase6-next-quoted-book",
                pair.quoted_product,
                IngressKind.BOOK,
                BASE_TS + timedelta(milliseconds=5),
                BASE_TS + timedelta(milliseconds=7),
                12,
                {"bids": [{"price": 78000.0, "quantity": 2}], "asks": [{"price": 78005.0, "quantity": 2}]},
            ),
            IngressEvent(
                "phase6-next-hedge-book",
                pair.hedge_product,
                IngressKind.BOOK,
                BASE_TS + timedelta(milliseconds=5),
                BASE_TS + timedelta(milliseconds=6),
                13,
                {"bids": [{"price": 77980.0, "quantity": 2}], "asks": [{"price": 77985.0, "quantity": 2}]},
            ),
        )
        ingress = CausalIngress("phase6-pricing", events, required_book_products=(pair.quoted_product, pair.hedge_product))
        tuple(ingress.replay())
        context = ingress.decision_context("phase6-pricing-decision", pair, observed_fill_ids=("observed-fill",))
        for event in events:
            if event.kind is IngressKind.BOOK:
                api.record_book_event(event, ingress.book_ref_for_event(event.event_id))
                ref = ingress.book_ref_for_event(event.event_id)
                api.record_book_snapshot(ref, ingress.book_snapshot(ref))
        missing_reference = OrderIntent(
            "phase6-missing-pricing-reference", context.run_id, context.decision_id, pair,
            pair.hedge_product, OrderRole.HEDGE, OrderSide.SELL, 1, 77980.0,
        )
        try:
            api.submit(MakerHedgeIntentBatch(hedge_intent=missing_reference), context, occurred_at=context.dec_ts)
        except FoundationContractError as exc:
            assert "requires a policy-owned pricing_reference" in str(exc)
        else:
            raise AssertionError("an interval-fill order must declare a price basis")

        assert context.previous_hedge_book is not None and context.previous_hedge_book.exchange_batch is not None
        valid_reference = OrderPricingReference(
            context.previous_hedge_book.exchange_batch,
            context.previous_hedge_book.snapshot_id,
            "previous_batch_interval_fill_v1",
            "observed-fill",
        )
        valid_intent = OrderIntent(
            "phase6-prior-batch-price", context.run_id, context.decision_id, pair,
            pair.hedge_product, OrderRole.HEDGE, OrderSide.SELL, 1, 77980.0,
            pricing_reference=valid_reference,
        )
        api.submit(MakerHedgeIntentBatch(hedge_intent=valid_intent), context, occurred_at=context.dec_ts)
