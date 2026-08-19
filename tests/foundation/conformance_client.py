"""Test-only client that exercises only the public Phase-6 foundation API."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Mapping

from common.foundation_api import DualBookFoundation
from common.foundation_contracts import (
    EodCloseRequest,
    EodDisposition,
    ExecutionResult,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    IntentLifecycleState,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderRole,
    OrderSide,
    TrialDeclaration,
)
from common.ingress import CausalIngress


class _StaticPolicy:
    """A policy adapter that retains only the immutable context it was handed."""

    def __init__(self, batch: MakerHedgeIntentBatch) -> None:
        self.batch = batch
        self.contexts = []

    def propose(self, context):
        self.contexts.append(context)
        return self.batch


@dataclass(frozen=True)
class ConformanceRun:
    maker_policy_context: object
    maker_state: IntentLifecycleState
    first_hedge_result: ExecutionResult
    second_hedge_result: ExecutionResult
    eod_disposition: EodDisposition
    final_quoted_position: int
    final_hedge_position: int
    telemetry_eligible: bool


class ConformanceClient:
    """Small S0 flow client: maker partial, hedge fills, residual, cancellation, EOD."""

    def run(
        self,
        api: DualBookFoundation,
        *,
        run_id: str,
        hedge_pair: HedgePairRef,
        events: tuple[IngressEvent, ...],
    ) -> ConformanceRun:
        ingress = CausalIngress(run_id, events)
        tuple(ingress.replay())
        maker_context = ingress.decision_context("phase6-maker", hedge_pair, consumed_signal_ids=("phase6-signal",))
        self._record_inputs(api, ingress, events, maker_context)
        api.ingest_depth_from_snapshot(maker_context.quoted_book)
        api.ingest_depth_from_snapshot(maker_context.hedge_book)

        maker = OrderIntent(
            "phase6-maker-order",
            run_id,
            maker_context.decision_id,
            hedge_pair,
            hedge_pair.quoted_product,
            OrderRole.MAKER,
            OrderSide.BUY,
            2,
            78000.0,
        )
        maker_policy = _StaticPolicy(MakerHedgeIntentBatch(maker, None, "phase6-quoted-cap"))
        api.propose(maker_policy, maker_context, occurred_at=maker_context.dec_ts + timedelta(milliseconds=1))
        api.arrive(maker.intent_id, occurred_at=maker_context.dec_ts + timedelta(milliseconds=2))
        maker_state = api.record_passive_fill(
            maker.intent_id,
            1,
            occurred_at=maker_context.dec_ts + timedelta(milliseconds=3),
            fee=0.1,
        )
        api.terminate(
            maker.intent_id,
            IntentLifecycleState.CANCELLED,
            occurred_at=maker_context.dec_ts + timedelta(milliseconds=4),
            disposition_reason="maker_residual_cancelled",
        )

        first_hedge_context = replace(maker_context, decision_id="phase6-hedge-one")
        first_hedge = self._hedge_intent("phase6-hedge-one-order", run_id, first_hedge_context, 1)
        api.propose(
            _StaticPolicy(MakerHedgeIntentBatch(hedge_intent=first_hedge)),
            first_hedge_context,
            occurred_at=first_hedge_context.dec_ts,
        )
        api.arrive(first_hedge.intent_id, occurred_at=first_hedge_context.dec_ts + timedelta(milliseconds=1))
        first_result = api.execute_hedge(
            first_hedge.intent_id,
            executed_at=first_hedge_context.dec_ts + timedelta(milliseconds=2),
            fee=0.2,
        )

        second_hedge_context = replace(maker_context, decision_id="phase6-hedge-two")
        second_hedge = self._hedge_intent("phase6-hedge-two-order", run_id, second_hedge_context, 2)
        api.propose(
            _StaticPolicy(MakerHedgeIntentBatch(hedge_intent=second_hedge)),
            second_hedge_context,
            occurred_at=second_hedge_context.dec_ts,
        )
        api.arrive(second_hedge.intent_id, occurred_at=second_hedge_context.dec_ts + timedelta(milliseconds=1))
        second_result = api.execute_hedge(
            second_hedge.intent_id,
            executed_at=second_hedge_context.dec_ts + timedelta(milliseconds=2),
            fee=0.2,
        )
        api.terminate(
            second_hedge.intent_id,
            IntentLifecycleState.CANCELLED,
            occurred_at=second_hedge_context.dec_ts + timedelta(milliseconds=3),
            disposition_reason="hedge_residual_cancelled",
        )

        eod_context = replace(maker_context, decision_id="phase6-eod")
        completion = api.complete_eod(
            EodCloseRequest(
                "phase6-eod-close",
                eod_context,
                {
                    hedge_pair.quoted_product: 77995.0,
                    hedge_pair.hedge_product: 77990.0,
                },
            ),
            executed_at=eod_context.dec_ts + timedelta(milliseconds=1),
            fees_by_product={hedge_pair.quoted_product: 0.1, hedge_pair.hedge_product: 0.2},
        )
        api.record_trigger(
            "phase6-trigger",
            eod_context,
            occurred_at=eod_context.dec_ts + timedelta(milliseconds=2),
            attributes={"trigger": "conformance"},
        )
        api.record_inventory("phase6-final-inventory", occurred_at=eod_context.dec_ts + timedelta(milliseconds=3))
        api.record_unattributed_outcome("phase6-no-pnl", "conformance run intentionally omits PnL attribution")
        api.capture_provenance(self._trial(hedge_pair), self._artifacts())
        final = api.finalize()
        state = api.ledger_state()
        return ConformanceRun(
            maker_policy.contexts[0],
            maker_state,
            first_result,
            second_result,
            completion.disposition,
            state.quoted_position,
            state.hedge_position,
            final.eligible,
        )

    @staticmethod
    def _record_inputs(api: DualBookFoundation, ingress: CausalIngress, events: tuple[IngressEvent, ...], context) -> None:
        for event in events:
            if event.kind is IngressKind.BOOK:
                api.record_book_event(event, ingress.book_ref_for_event(event.event_id))
        for ref in (context.quoted_book, context.hedge_book):
            api.record_book_snapshot(ref, ingress.book_snapshot(ref))
        for ref in context.consumed_signals:
            api.record_signal_snapshot(ref, ingress.signal_snapshot(ref))

    @staticmethod
    def _hedge_intent(intent_id: str, run_id: str, context, quantity: int) -> OrderIntent:
        return OrderIntent(
            intent_id,
            run_id,
            context.decision_id,
            context.hedge_pair,
            context.hedge_product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            quantity,
            77980.0,
        )

    @staticmethod
    def _trial(pair: HedgePairRef) -> TrialDeclaration:
        from common.foundation_contracts import ExecutionModelRef

        return TrialDeclaration(
            "phase6-conformance",
            "2025-01-01:2025-01-31",
            "2025-02-01:2025-02-14",
            "2025-02-15:2025-02-28",
            "frozen-before-holdout",
            "phase6-client-v1",
            pair,
            (ExecutionModelRef("phase6-depth", "1.0.0"),),
            ("fixture-input",),
        )

    @staticmethod
    def _artifacts() -> Mapping[str, object]:
        return {
            "market_data": "phase6-market",
            "signal_data": "phase6-signal",
            "configuration": "phase6-configuration",
            "code": "phase6-conformance-client",
            "schema": "telemetry-schema-v0.4",
            "fee_profile": "phase6-fees",
            "instrument_roll_mapping": "phase6-roll",
            "execution_models": "phase6-depth-1.0.0",
        }
