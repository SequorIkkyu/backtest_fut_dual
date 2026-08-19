"""Operational foundation tests: arrival-time execution and verified maker fills."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from common.foundation_api import DualBookFoundation
from common.foundation_contracts import (
    CapacityEnvelope,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    IngressEvent,
    IngressKind,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderRole,
    OrderSide,
    PassiveTrade,
)
from common.ingress import CausalIngress
from common.passive_matching import PassiveMatchingService
from common.telemetry import TelemetryEmitter
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def _maker(intent_id, context, pair, quantity=1):
    return OrderIntent(
        intent_id,
        context.run_id,
        context.decision_id,
        pair,
        pair.quoted_product,
        OrderRole.MAKER,
        OrderSide.BUY,
        quantity,
        78000.0,
    )


def _trade(fixture, context, *, quantity=3):
    return PassiveTrade(
        "trade-1",
        context.run_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderSide.SELL,
        context.dec_ts + timedelta(milliseconds=2),
        context.feed_seq,
        context.quoted_book,
        78000.0,
        quantity,
    )


def test_passive_matcher_derives_queue_aware_evidence_and_rejects_forgery():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    maker = _maker("passive-matcher-order", context, fixture.hedge_pair)
    matcher = PassiveMatchingService(object())
    matcher.register_intent(
        maker,
        context,
        queue_ahead_submit=2,
        arrival_book_snapshot=context.quoted_book,
    )

    matches = matcher.match_trade(_trade(fixture, context), fee_rebate_per_contract=0.25)

    assert len(matches) == 1
    evidence = matches[0]
    assert evidence.fill_qty == 1
    assert evidence.cumulative_fill_qty == 1
    assert evidence.queue_ahead_submit == 2
    assert evidence.queue_ahead_fill == 0
    assert evidence.fill_price == maker.limit_price
    assert evidence.fee_rebate == 0.25
    matcher.validate_evidence(evidence)
    try:
        matcher.validate_evidence(replace(evidence, fill_price=evidence.fill_price + 5.0))
    except FoundationContractError as exc:
        assert "not issued" in str(exc)
    else:
        raise AssertionError("hand-modified passive evidence must be rejected")


def test_foundation_requires_matcher_issued_passive_evidence_in_production_mode():
    fixture = make_dual_book_fixture()
    events = (
        IngressEvent(
            "quoted-book",
            fixture.quoted_spec.product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=2),
            1,
            {
                "bids": [{"price": 78000.0, "quantity": 2}],
                "asks": [{"price": 78005.0, "quantity": 2}],
            },
        ),
        IngressEvent(
            "hedge-book",
            fixture.hedge_spec.product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=3),
            2,
            {
                "bids": [{"price": 77980.0, "quantity": 2}],
                "asks": [{"price": 77985.0, "quantity": 2}],
            },
        ),
    )
    with TemporaryDirectory() as temporary:
        ingress = CausalIngress("passive-production", events)
        tuple(ingress.replay())
        context = ingress.decision_context("passive-decision", fixture.hedge_pair)
        model = ExecutionModelConfig("passive-production-depth", "1.0.0", 1.0)
        api = DualBookFoundation(
            run_id=context.run_id,
            hedge_mapping=HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0),
            instrument_specs=(fixture.quoted_spec, fixture.hedge_spec),
            execution_models=(model,),
            default_execution_model=ExecutionModelRef(model.model_id, model.version),
            capacity_envelopes=(CapacityEnvelope("quoted-cap", fixture.hedge_pair, fixture.quoted_spec.product, 2),),
            telemetry=TelemetryEmitter(Path(temporary), context.run_id, fixture.hedge_pair),
            require_verified_passive_fills=True,
        )
        for event in events:
            api.record_book_event(event, ingress.book_ref_for_event(event.event_id))
        for ref in (context.quoted_book, context.hedge_book):
            api.record_book_snapshot(ref, ingress.book_snapshot(ref))
        maker = _maker("passive-production-order", context, fixture.hedge_pair)
        api.submit(MakerHedgeIntentBatch(maker, None, "quoted-cap"), context, occurred_at=context.dec_ts)
        api.arrive(
            maker.intent_id,
            occurred_at=context.dec_ts + timedelta(milliseconds=1),
            passive_book_snapshot=context.quoted_book,
        )

        try:
            api.record_passive_fill(maker.intent_id, 1, occurred_at=context.dec_ts + timedelta(milliseconds=2))
        except FoundationContractError as exc:
            assert "require matcher-derived" in str(exc)
        else:
            raise AssertionError("production mode must reject caller-asserted passive fills")

        evidence = api.match_passive_trade(
            PassiveTrade(
                "passive-production-trade",
                context.run_id,
                fixture.hedge_pair,
                fixture.quoted_spec.product,
                OrderSide.SELL,
                context.dec_ts + timedelta(milliseconds=2),
                context.feed_seq,
                context.quoted_book,
                78000.0,
                3,
            ),
            fee_rebate_per_contract=0.1,
        )

        assert len(evidence) == 1
        assert evidence[0].queue_ahead_submit == 2
        assert api.state_of(maker.intent_id).value == "filled"
        try:
            api.record_passive_match(evidence[0])
        except FoundationContractError as exc:
            assert "not issued" in str(exc)
        else:
            raise AssertionError("one matcher evidence record must not be applied twice")


def test_passive_matcher_preserves_fifo_priority_for_same_price_orders():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    matcher = PassiveMatchingService(object())
    first = _maker("fifo-first", context, fixture.hedge_pair)
    second = _maker("fifo-second", context, fixture.hedge_pair)
    matcher.register_intent(first, context, queue_ahead_submit=2, arrival_book_snapshot=context.quoted_book)
    matcher.register_intent(second, context, queue_ahead_submit=2, arrival_book_snapshot=context.quoted_book)

    first_trade = _trade(fixture, context, quantity=3)
    assert tuple(match.intent_id for match in matcher.match_trade(first_trade)) == ("fifo-first",)

    second_trade = PassiveTrade(
        "trade-2",
        context.run_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderSide.SELL,
        context.dec_ts + timedelta(milliseconds=3),
        context.feed_seq,
        context.quoted_book,
        78000.0,
        1,
    )
    assert tuple(match.intent_id for match in matcher.match_trade(second_trade)) == ("fifo-second",)


def test_passive_matcher_conserves_one_trade_across_price_levels_and_rejects_duplicates():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    matcher = PassiveMatchingService(object())
    lower_bid = _maker("lower-bid", context, fixture.hedge_pair)
    higher_bid = replace(_maker("higher-bid", context, fixture.hedge_pair), limit_price=78001.0)
    matcher.register_intent(lower_bid, context, queue_ahead_submit=0, arrival_book_snapshot=context.quoted_book)
    matcher.register_intent(higher_bid, context, queue_ahead_submit=0, arrival_book_snapshot=context.quoted_book)

    first_trade = PassiveTrade(
        "shared-trade-id",
        context.run_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderSide.SELL,
        context.dec_ts + timedelta(milliseconds=2),
        context.feed_seq,
        context.quoted_book,
        78000.0,
        1,
        "quoted-event-1",
    )
    matches = matcher.match_trade(first_trade)

    assert tuple(match.intent_id for match in matches) == ("higher-bid",)
    assert sum(match.fill_qty for match in matches) == first_trade.quantity
    assert matches[0].trade_reference == "quoted-event-1:shared-trade-id"
    try:
        matcher.match_trade(first_trade)
    except FoundationContractError as exc:
        assert "already been matched" in str(exc)
    else:
        raise AssertionError("one source-qualified passive trade must not be replayed twice")

    distinct_source_trade = replace(first_trade, source_event_id="quoted-event-2")
    assert tuple(match.intent_id for match in matcher.match_trade(distinct_source_trade)) == ("lower-bid",)
