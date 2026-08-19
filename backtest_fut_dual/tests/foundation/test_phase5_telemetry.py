"""Phase-5 acceptance tests for canonical telemetry, invariants, and provenance."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from common.foundation_contracts import (
    CapacityReservationEvent,
    DualLegLedgerState,
    ExecutionLevel,
    ExecutionModelRef,
    ExecutionResult,
    ExecutionStatus,
    FoundationContractError,
    HedgeMappingSpec,
    IngressKind,
    IntentLifecycleEvent,
    IntentLifecycleState,
    LedgerEvent,
    LedgerLeg,
    OrderIntent,
    OrderRole,
    OrderSide,
    PassiveFillEvidence,
    ReservationAction,
    SignalSnapshotRef,
    TrialDeclaration,
)
from common.ingress import CausalIngress
from common.telemetry import TelemetryEmitter, load_canonical_table
from common.tests.foundation.fixtures import make_dual_book_fixture


def _context(run_id: str = "fixture-run"):
    fixture = make_dual_book_fixture()
    ingress = CausalIngress(run_id, fixture.events)
    tuple(ingress.replay())
    context = ingress.decision_context(
        "telemetry-decision",
        fixture.hedge_pair,
        consumed_signal_ids=("inventory-estimator",),
    )
    return fixture, ingress, context


def _trial(pair) -> TrialDeclaration:
    return TrialDeclaration(
        "trial-telemetry",
        "2025-01-01:2025-01-31",
        "2025-02-01:2025-02-14",
        "2025-02-15:2025-02-28",
        "frozen-before-holdout",
        "policy-v1",
        pair,
        (ExecutionModelRef("full-depth", "1.0.0"),),
        ("zero_volume_levels_rejected",),
    )


def _artifacts() -> dict[str, object]:
    return {
        "market_data": {"source": "fixture-market", "hash_input": 1},
        "signal_data": {"source": "fixture-signal", "hash_input": 1},
        "configuration": {"calendar": "fixture", "capacity": 10},
        "code": "foundation-source-v1",
        "schema": "telemetry-schema-v0.3",
        "fee_profile": {"model": "fixture-fee-v1"},
        "instrument_roll_mapping": {"quoted": "ZN-main", "hedge": "ZN-next"},
        "execution_models": {"full-depth": "1.0.0"},
    }


def _make_orders(fixture, context):
    maker = OrderIntent(
        "maker-telemetry",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        context.feed_seq,
        78000.0,
    )
    hedge = OrderIntent(
        "hedge-telemetry",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.hedge_spec.product,
        OrderRole.HEDGE,
        OrderSide.SELL,
        context.feed_seq,
        77980.0,
    )
    return maker, hedge


def _emit_valid_run(root: Path, run_id: str = "phase5-valid") -> tuple[TelemetryEmitter, object, object, object, object]:
    fixture, ingress, context = _context(run_id)
    emitter = TelemetryEmitter(root, run_id, fixture.hedge_pair)
    emitter.emit_book_event(fixture.events[0], ingress.book_ref_for_event(fixture.events[0].event_id))
    emitter.emit_book_event(fixture.events[1], ingress.book_ref_for_event(fixture.events[1].event_id))
    emitter.emit_book_snapshot(context.quoted_book, ingress.book_snapshot(context.quoted_book))
    emitter.emit_book_snapshot(context.hedge_book, ingress.book_snapshot(context.hedge_book))
    signal = context.consumed_signals[0]
    emitter.emit_signal_snapshot(signal, ingress.signal_snapshot(signal))
    emitter.emit_decision(context)

    maker, hedge = _make_orders(fixture, context)
    emitter.emit_order(maker, context)
    emitter.emit_order(hedge, context)
    at = context.dec_ts + timedelta(milliseconds=1)
    model_ref = ExecutionModelRef("full-depth", "1.0.0")
    maker_lifecycle = IntentLifecycleEvent(
        "maker-telemetry-filled",
        context.run_id,
        context.decision_id,
        maker.intent_id,
        fixture.hedge_pair,
        maker.product,
        IntentLifecycleState.FILLED,
        at,
        model_ref,
        1,
        0,
    )
    hedge_lifecycle = IntentLifecycleEvent(
        "hedge-telemetry-filled",
        context.run_id,
        context.decision_id,
        hedge.intent_id,
        fixture.hedge_pair,
        hedge.product,
        IntentLifecycleState.FILLED,
        at,
        model_ref,
        1,
        0,
        "hedge-telemetry-execution",
    )
    emitter.emit_lifecycle(maker_lifecycle)
    emitter.emit_lifecycle(hedge_lifecycle)
    reserve = CapacityReservationEvent(
        "maker-telemetry-reserve",
        context.run_id,
        context.decision_id,
        maker.intent_id,
        fixture.hedge_pair,
        maker.product,
        "quoted-cap",
        ReservationAction.RESERVE,
        1.0,
        at,
    )
    release = CapacityReservationEvent(
        "maker-telemetry-release",
        context.run_id,
        context.decision_id,
        maker.intent_id,
        fixture.hedge_pair,
        maker.product,
        "quoted-cap",
        ReservationAction.RELEASE,
        1.0,
        at + timedelta(milliseconds=1),
    )
    emitter.emit_reservation(reserve, 2)
    emitter.emit_reservation(release, 2)

    maker_ledger = LedgerEvent(
        "ledger-maker-telemetry",
        context.run_id,
        context.decision_id,
        maker_lifecycle.event_id,
        fixture.hedge_pair,
        LedgerLeg.QUOTED,
        maker.product,
        1,
        at,
        {"intent_id": maker.intent_id, "order_role": "maker"},
        0.2,
        0.0,
    )
    hedge_ledger = LedgerEvent(
        "ledger-hedge-telemetry",
        context.run_id,
        context.decision_id,
        hedge_lifecycle.execution_id,
        fixture.hedge_pair,
        LedgerLeg.HEDGE,
        hedge.product,
        -1,
        at,
        {"intent_id": hedge.intent_id, "order_role": "hedge"},
        0.3,
        0.1,
    )
    emitter.emit_ledger_effect(maker_ledger)
    emitter.emit_ledger_effect(hedge_ledger)
    result = ExecutionResult(
        "hedge-telemetry-execution",
        hedge.intent_id,
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        hedge.product,
        hedge.side,
        ExecutionStatus.FILLED,
        1,
        1,
        0,
        at,
        model_ref,
        1.0,
        context.feed_seq,
        context.feed_seq,
        context.hedge_book,
        hedge.limit_price,
        (ExecutionLevel(77980.0, 1),),
        77980.0,
        77980.0,
    )
    emitter.emit_execution(result)
    emitter.emit_trigger_evaluation("trigger-telemetry", context, at, {"passed": True})
    emitter.emit_outcome_pnl("outcome-telemetry", "awaiting_g4c_attribution")
    mapping = HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0)
    state = DualLegLedgerState(
        context.run_id,
        fixture.hedge_pair,
        mapping,
        1,
        -1,
        0.0,
        0.0,
        0.5,
        0.1,
        (maker_ledger.event_id, hedge_ledger.event_id),
    )
    emitter.emit_inventory("inventory-telemetry", state, at)
    emitter.capture_provenance(_trial(fixture.hedge_pair), _artifacts())
    return emitter, fixture, ingress, context, result


def _invariant(result, invariant_id: str):
    return next(item for item in result.invariants if item.invariant_id == invariant_id)


def test_canonical_telemetry_streams_all_tables_reconstructs_snapshots_and_is_eligible():
    with TemporaryDirectory() as temporary:
        emitter, fixture, ingress, context, _ = _emit_valid_run(Path(temporary))
        result = emitter.finalize()
        assert result.eligible
        assert all(item.passed for item in result.invariants)
        assert emitter.buffered_rows == 0
        assert emitter.snapshot_payload("book", context.quoted_book.snapshot_hash) == ingress.book_snapshot(context.quoted_book)
        decisions = tuple(load_canonical_table(emitter.run_dir, "decisions"))
        assert decisions[0]["decision_id"] == context.decision_id
        assert {row["table"] for row in decisions} == {"decisions"}
        assert (emitter.run_dir / "provenance" / "manifest.json").exists()
        assert (emitter.run_dir / "meta" / "run_result.json").exists()
        try:
            emitter.emit_outcome_pnl("after-finalize", "must-fail")
        except FoundationContractError as exc:
            assert "finalized" in str(exc)
        else:
            raise AssertionError("finalized telemetry must be immutable")


def test_book_event_telemetry_uses_resolved_batch_ordinal_and_preserves_source_sequence():
    with TemporaryDirectory() as temporary:
        fixture = make_dual_book_fixture()
        events = tuple(
            replace(event, exchange_batch_id="source-batch-37", exchange_batch_seq=37) for event in fixture.events
        )
        ingress = CausalIngress("phase5-batch-sequence", events)
        tuple(ingress.replay())
        emitter = TelemetryEmitter(Path(temporary), "phase5-batch-sequence", fixture.hedge_pair)
        for event in events:
            if event.kind is IngressKind.BOOK:
                emitter.emit_book_event(event, ingress.book_ref_for_event(event.event_id))
        rows = tuple(load_canonical_table(emitter.run_dir, "book_events"))

    assert len(rows) == 2
    for event, row in zip(events[:2], rows):
        snapshot = ingress.book_ref_for_event(event.event_id)
        assert row["exchange_batch_id"] == snapshot.exchange_batch.batch_id
        assert row["exchange_batch_seq"] == snapshot.exchange_batch.sequence
        assert row["source_exchange_batch_seq"] == 37
        assert row["exchange_batch_seq"] != row["source_exchange_batch_seq"]


def test_verified_passive_control_rejects_maker_ledger_effect_without_match_evidence():
    with TemporaryDirectory() as temporary:
        emitter, _, _, _, _ = _emit_valid_run(Path(temporary), "phase5-missing-passive-evidence")
        emitter.set_run_controls(require_verified_passive_fills=True)
        result = emitter.finalize()

    evidence = _invariant(result, "passive.fill_evidence")
    assert not result.eligible
    assert not evidence.passed
    assert "ledger-maker-telemetry" in evidence.related_ids


def test_verified_passive_control_rejects_trade_quantity_overallocation():
    with TemporaryDirectory() as temporary:
        emitter, fixture, _, context, _ = _emit_valid_run(Path(temporary), "phase5-overallocated-passive-trade")
        emitter.set_run_controls(require_verified_passive_fills=True)
        for fill_id, cumulative_fill_qty in (("passive-evidence-1", 1), ("passive-evidence-2", 2)):
            emitter.emit_passive_fill_evidence(
                PassiveFillEvidence(
                    fill_id,
                    "overallocated-trade",
                    "quoted-event-1",
                    1,
                    "maker-telemetry",
                    context.run_id,
                    context.decision_id,
                    fixture.hedge_pair,
                    fixture.quoted_spec.product,
                    OrderSide.BUY,
                    context.dec_ts,
                    context.feed_seq,
                    context.quoted_book,
                    78000.0,
                    1,
                    cumulative_fill_qty,
                    0,
                    0,
                    0.0,
                    object(),
                )
            )
        result = emitter.finalize()

    evidence = _invariant(result, "passive.fill_evidence")
    assert not result.eligible
    assert not evidence.passed
    assert "quoted-event-1:overallocated-trade" in evidence.related_ids


def test_invariant_breaches_make_the_run_ineligible_with_machine_readable_reasons():
    with TemporaryDirectory() as temporary:
        emitter, fixture, _, context, _ = _emit_valid_run(Path(temporary), "phase5-invalid")
        late_signal = SignalSnapshotRef(
            "late-signal",
            fixture.quoted_spec.product,
            context.feed_seq,
            "late-signal-event",
            context.dec_ts + timedelta(seconds=1),
            "late-signal-snapshot",
            "sha256:8a1c1dd9b82c44b88019c5a8d61b4967e5c2dc20b58ee2f0a3d75df4a1b0bf0e",
        )
        # The declared hash is for this exact payload and proves the failure is causal, not reconstruction.
        late_payload = {"signal_id": "late-signal", "score": 1}
        from common.telemetry import _sha256  # test-only exact canonical digest helper

        late_signal = SignalSnapshotRef(
            late_signal.signal_id,
            late_signal.product,
            late_signal.feed_seq,
            late_signal.event_id,
            late_signal.available_at,
            late_signal.snapshot_id,
            _sha256(late_payload),
        )
        emitter.emit_signal_snapshot(late_signal, late_payload)
        emitter.emit_row(
            "decisions",
            "late-decision",
            {
                "decision_id": "late-decision",
                "dec_ts": context.dec_ts,
                "feed_seq": context.feed_seq,
                "quoted_book_snapshot_id": context.quoted_book.snapshot_id,
                "hedge_book_snapshot_id": context.hedge_book.snapshot_id,
                "consumed_signal_snapshot_ids": [late_signal.snapshot_id],
            },
        )
        mixed_offset_payload = {"signal_id": "mixed-offset-signal", "score": 1}
        mixed_offset_signal = SignalSnapshotRef(
            "mixed-offset-signal",
            fixture.quoted_spec.product,
            context.feed_seq,
            "mixed-offset-event",
            context.dec_ts.replace(hour=8, tzinfo=timezone(timedelta(hours=-5))),
            "mixed-offset-snapshot",
            _sha256(mixed_offset_payload),
        )
        emitter.emit_signal_snapshot(mixed_offset_signal, mixed_offset_payload)
        emitter.emit_row(
            "decisions",
            "mixed-offset-decision",
            {
                "decision_id": "mixed-offset-decision",
                "dec_ts": context.dec_ts,
                "feed_seq": context.feed_seq,
                "quoted_book_snapshot_id": context.quoted_book.snapshot_id,
                "hedge_book_snapshot_id": context.hedge_book.snapshot_id,
                "consumed_signal_snapshot_ids": [mixed_offset_signal.snapshot_id],
            },
        )
        emitter.emit_row(
            "hedge_executions",
            "stale-success",
            {
                "execution_id": "stale-success",
                "order_id": "hedge-telemetry",
                "decision_id": context.decision_id,
                "product": fixture.hedge_spec.product,
                "status": "filled",
                "requested_qty": 1,
                "filled_qty": 1,
                "residual_qty": 0,
                "book_snapshot_id": "missing-book",
                "decision_feed_seq": context.feed_seq,
                "execution_feed_seq": 99,
                "executed_at": context.dec_ts + timedelta(seconds=1),
            },
        )
        emitter.emit_row(
            "hedge_executions",
            "duplicate-execution-row",
            {
                "execution_id": "stale-success",
                "order_id": "hedge-telemetry",
                "decision_id": context.decision_id,
                "product": fixture.hedge_spec.product,
                "status": "filled",
                "requested_qty": 1,
                "filled_qty": 1,
                "residual_qty": 0,
                "book_snapshot_id": context.hedge_book.snapshot_id,
                "decision_feed_seq": context.feed_seq,
                "execution_feed_seq": 100,
                "executed_at": context.dec_ts + timedelta(seconds=1),
            },
        )
        emitter.emit_row(
            "hedge_executions",
            "wrong-context-book",
            {
                "execution_id": "wrong-context-book",
                "order_id": "hedge-telemetry",
                "decision_id": context.decision_id,
                "product": fixture.hedge_spec.product,
                "status": "filled",
                "requested_qty": 1,
                "filled_qty": 1,
                "residual_qty": 0,
                "book_snapshot_id": context.quoted_book.snapshot_id,
                "decision_feed_seq": context.feed_seq,
                "execution_feed_seq": 101,
                "executed_at": context.dec_ts + timedelta(seconds=1),
            },
        )
        emitter.emit_row(
            "orders",
            "capacity-breach",
            {
                "order_id": "maker-telemetry",
                "decision_id": context.decision_id,
                "product": fixture.quoted_spec.product,
                "record_type": "capacity_reservation",
                "occurred_at": context.dec_ts + timedelta(seconds=2),
                "reservation_id": "capacity-breach",
                "envelope_id": "quoted-cap",
                "reservation_action": "reserve",
                "amount": 10.0,
                "max_reserved_qty": 2,
            },
        )
        emitter.emit_row(
            "fills",
            "orphan-fill",
            {
                "fill_id": "orphan-fill",
                "order_id": "missing-order",
                "decision_id": context.decision_id,
                "product": fixture.quoted_spec.product,
                "record_type": "ledger_effect",
                "quantity": 1,
                "position_delta": 1,
            },
        )
        emitter.emit_row(
            "orders",
            "open-only",
            {
                "order_id": "open-only",
                "decision_id": context.decision_id,
                "product": fixture.quoted_spec.product,
                "record_type": "lifecycle",
                "occurred_at": context.dec_ts + timedelta(seconds=3),
                "lifecycle_state": "submitted",
            },
        )
        result = emitter.finalize()
        assert not result.eligible
        causality = _invariant(result, "causality.snapshot_availability")
        assert not causality.passed
        assert "mixed-offset-decision" in causality.related_ids
        depth_use = _invariant(result, "execution.depth_use")
        assert not depth_use.passed
        assert "wrong-context-book" in depth_use.related_ids, depth_use.related_ids
        assert not _invariant(result, "capacity.envelope").passed
        assert not _invariant(result, "joins.fill_order_decision").passed
        assert not _invariant(result, "lifecycle.finality").passed
        assert not _invariant(result, "ledger.quantity").passed


def test_provenance_changes_for_any_artifact_and_every_trial_keeps_its_own_manifest():
    fixture, _, _ = _context()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = TelemetryEmitter(root, "trial-one", fixture.hedge_pair)
        first_provenance = first.capture_provenance(_trial(fixture.hedge_pair), _artifacts())
        modified = _artifacts()
        modified["configuration"] = {"calendar": "fixture", "capacity": 11}
        second = TelemetryEmitter(root, "trial-two", fixture.hedge_pair)
        second_provenance = second.capture_provenance(_trial(fixture.hedge_pair), modified)
        assert first_provenance.provenance_hash != second_provenance.provenance_hash
        assert (first.run_dir / "provenance" / "manifest.json").exists()
        assert (second.run_dir / "provenance" / "manifest.json").exists()


def test_large_synthetic_stream_keeps_no_unbounded_row_history():
    fixture, _, context = _context("large-stream")
    with TemporaryDirectory() as temporary:
        emitter = TelemetryEmitter(Path(temporary), "large-stream", fixture.hedge_pair)
        for index in range(1_000):
            emitter.emit_trigger_evaluation(
                f"trigger-{index}",
                context,
                context.dec_ts + timedelta(milliseconds=index),
                {"index": index},
            )
        assert emitter.emitted_rows == 1_000
        assert emitter.buffered_rows == 0
        assert sum(1 for _ in load_canonical_table(emitter.run_dir, "trigger_evaluations")) == 1_000


def test_explicit_empty_tables_keep_a_no_action_run_eligible():
    fixture, _, _ = _context()
    with TemporaryDirectory() as temporary:
        emitter = TelemetryEmitter(Path(temporary), "no-action-run", fixture.hedge_pair)
        for table in emitter.schema.tables:
            emitter.declare_empty_table(table)
        emitter.capture_provenance(_trial(fixture.hedge_pair), _artifacts())
        result = emitter.finalize()
        assert result.eligible
        assert tuple(load_canonical_table(emitter.run_dir, "fills")) == ()
