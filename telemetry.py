"""Phase-5 canonical telemetry, invariant evaluation, and trial provenance.

Rows are streamed directly to schema-versioned JSONL artifacts.  The emitter
keeps no event history in memory; final invariant evaluation scans those durable
artifacts through a small on-disk SQLite index.  This is the supported source
for foundation reporting and run eligibility, not the legacy strategy records.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from common.foundation_contracts import (
    BookSnapshotRef,
    CapacityReservationEvent,
    DecisionContext,
    DualLegLedgerState,
    ExecutionResult,
    ExecutionStatus,
    FoundationContractError,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    IntentLifecycleEvent,
    InvariantResult,
    InvariantSeverity,
    LedgerEvent,
    PassiveFillEvidence,
    PnlAttributionResult,
    RunProvenance,
    S0_TELEMETRY_TABLES,
    SignalSnapshotRef,
    SnapshotIntervalQueueProxyEvidence,
    TELEMETRY_SCHEMA_VERSION,
    TelemetryRunResult,
    TelemetrySchema,
    TrialDeclaration,
)


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TERMINAL_STATES = frozenset({"filled", "cancelled", "expired", "rejected", "stale", "deadline", "failed"})
_SUCCESSFUL_EXECUTION_STATUSES = frozenset({ExecutionStatus.FILLED.value, ExecutionStatus.PARTIAL.value})

_TABLE_REQUIRED_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "decisions": frozenset(
            {
                "decision_id",
                "dec_ts",
                "feed_seq",
                "quoted_book_snapshot_id",
                "hedge_book_snapshot_id",
                "consumed_signal_snapshot_ids",
            }
        ),
        "book_events": frozenset(
            {
                "event_id",
                "product",
                "exchange_ts",
                "recv_ts",
                "available_at",
                "exchange_batch_id",
                "exchange_batch_seq",
                "source_exchange_batch_seq",
            }
        ),
        "book_snapshots": frozenset(
            {"snapshot_id", "snapshot_hash", "product", "book_seq", "feed_seq", "available_at", "artifact_path"}
        ),
        "orders": frozenset({"order_id", "decision_id", "product", "record_type", "occurred_at"}),
        "fills": frozenset({"fill_id", "order_id", "decision_id", "product", "quantity", "position_delta", "record_type"}),
        "hedge_executions": frozenset(
            {
                "execution_id",
                "order_id",
                "decision_id",
                "product",
                "status",
                "requested_qty",
                "filled_qty",
                "residual_qty",
                "book_snapshot_id",
                "decision_feed_seq",
                "execution_feed_seq",
            }
        ),
        "trigger_evaluations": frozenset({"trigger_id", "decision_id", "occurred_at"}),
        "signal_snapshots": frozenset(
            {"snapshot_id", "snapshot_hash", "signal_id", "product", "feed_seq", "available_at", "artifact_path"}
        ),
        "outcome_pnl": frozenset({"outcome_id", "attribution_status", "economics_eligible"}),
        "inventory_series": frozenset(
            {
                "inventory_id",
                "occurred_at",
                "quoted_position",
                "hedge_position",
                "pending_hedge_quantity",
                "residual_risk",
            }
        ),
    }
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FoundationContractError(f"{field_name} must be a non-empty string")
    return value


def _require_aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FoundationContractError(f"{field_name} must be a timezone-aware datetime")
    return value


def _parse_aware_timestamp(value: object, field_name: str) -> datetime:
    """Parse a durable ISO timestamp and retain instant-aware comparison semantics."""
    if not isinstance(value, str):
        raise FoundationContractError(f"{field_name} must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FoundationContractError(f"{field_name} must be an ISO timestamp string") from exc
    return _require_aware(parsed, field_name)


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        _require_aware(value, "telemetry datetime")
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FoundationContractError("telemetry mapping keys must be strings")
            normalized[key] = _canonical(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise FoundationContractError("telemetry floats must be finite")
        return value
    raise FoundationContractError(f"telemetry value is not canonicalizable: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FoundationContractError("telemetry value must be canonical JSON data") from exc


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_json_bytes(value)).hexdigest()}"


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _pair_fields(pair: HedgePairRef) -> dict[str, str]:
    return {
        "pair_id": pair.pair_id,
        "quoted_product": pair.quoted_product,
        "hedge_product": pair.hedge_product,
        "hedge_mapping_id": pair.hedge_mapping_id,
        "hedge_mapping_version": pair.hedge_mapping_version,
    }


def _artifact_bytes(value: Any) -> bytes:
    if isinstance(value, Path):
        try:
            return value.read_bytes()
        except OSError as exc:
            raise FoundationContractError(f"provenance artifact cannot be read: {value}") from exc
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return _json_bytes(value)


class TelemetryEmitter:
    """Stream canonical Phase-5 telemetry and retain one immutable trial artifact set."""

    def __init__(self, artifact_root: str | Path, run_id: str, hedge_pair: HedgePairRef) -> None:
        _require_text(run_id, "run_id")
        if not _SAFE_SEGMENT.fullmatch(run_id):
            raise FoundationContractError("run_id must be safe for a durable artifact directory")
        if not isinstance(hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        self.schema = TelemetrySchema()
        self.run_id = run_id
        self.hedge_pair = hedge_pair
        self.artifact_root = Path(artifact_root).resolve()
        self.run_dir = self.artifact_root / run_id
        if self.run_dir.exists():
            raise FoundationContractError("run artifact directory already exists; trial artifacts are immutable")
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "tables").mkdir()
        (self.run_dir / "snapshots" / "book").mkdir(parents=True)
        (self.run_dir / "snapshots" / "signal").mkdir(parents=True)
        (self.run_dir / "provenance" / "artifacts").mkdir(parents=True)
        (self.run_dir / "meta").mkdir()
        self._provenance: RunProvenance | None = None
        self._result: TelemetryRunResult | None = None
        self._emitted_rows = 0
        self._run_controls: Mapping[str, Any] | None = None

    @property
    def buffered_rows(self) -> int:
        """Rows are immediately checkpointed, so no history is held in memory."""
        return 0

    @property
    def emitted_rows(self) -> int:
        return self._emitted_rows

    @property
    def provenance(self) -> RunProvenance | None:
        return self._provenance

    def emit_row(self, table: str, entity_id: str, fields: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate and durably append one canonical table row."""
        self._ensure_open()
        if table not in S0_TELEMETRY_TABLES:
            raise FoundationContractError("telemetry table is not in the published S0 schema")
        _require_text(entity_id, "entity_id")
        if not isinstance(fields, Mapping):
            raise FoundationContractError("telemetry fields must be a mapping")
        payload = dict(_canonical(fields))
        forbidden = {"run_id", "schema_version", "table", "row_id", *(_pair_fields(self.hedge_pair))}
        if forbidden & set(payload):
            raise FoundationContractError("canonical telemetry identity fields are owned by the emitter")
        missing = _TABLE_REQUIRED_FIELDS[table] - set(payload)
        if missing:
            raise FoundationContractError(f"telemetry row is missing required {table} fields: {sorted(missing)}")
        row = {
            "run_id": self.run_id,
            "schema_version": self.schema.version,
            "table": table,
            "row_id": f"{self.run_id}:{table}:{entity_id}",
            **_pair_fields(self.hedge_pair),
            **payload,
        }
        self._append_jsonl(self.run_dir / "tables" / f"{table}.jsonl", row)
        self._emitted_rows += 1
        return MappingProxyType(row)

    def declare_empty_table(self, table: str) -> None:
        """Retain an explicit empty canonical table for a valid no-record outcome."""
        self._ensure_open()
        if table not in S0_TELEMETRY_TABLES:
            raise FoundationContractError("telemetry table is not in the published S0 schema")
        (self.run_dir / "tables" / f"{table}.jsonl").touch(exist_ok=True)

    def set_run_controls(self, *, require_verified_passive_fills: bool) -> None:
        """Persist one immutable operational-control declaration before emission."""
        self._ensure_open()
        if not isinstance(require_verified_passive_fills, bool):
            raise FoundationContractError("require_verified_passive_fills must be boolean")
        controls = {"require_verified_passive_fills": require_verified_passive_fills}
        if self._run_controls is not None and self._run_controls != controls:
            raise FoundationContractError("run controls are immutable once declared")
        if self._run_controls is None:
            self._atomic_write(self.run_dir / "meta" / "run_controls.json", _json_bytes(controls))
            self._run_controls = MappingProxyType(controls)

    def emit_book_event(self, event: IngressEvent, book_snapshot: BookSnapshotRef) -> Mapping[str, Any]:
        """Emit source provenance with the resolved replay batch identity.

        ``IngressEvent.exchange_batch_seq`` is source-declared metadata used
        only to disambiguate equal exchange timestamps.  The canonical
        ``exchange_batch_seq`` is instead the resolved monotone replay ordinal
        from the retained snapshot; the source value is preserved separately.
        """
        if not isinstance(event, IngressEvent) or event.kind is not IngressKind.BOOK:
            raise FoundationContractError("book event telemetry requires a BOOK IngressEvent")
        if not isinstance(book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book event telemetry requires a BookSnapshotRef")
        if (
            book_snapshot.event_id != event.event_id
            or book_snapshot.product != event.product
            or book_snapshot.exchange_batch is None
            or book_snapshot.exchange_batch.exchange_ts != event.exchange_ts
        ):
            raise FoundationContractError("book event telemetry snapshot must be the event's resolved exchange batch")
        self._validate_pair_product(event.product)
        return self.emit_row(
            "book_events",
            event.event_id,
            {
                "event_id": event.event_id,
                "product": event.product,
                "exchange_ts": event.exchange_ts,
                "recv_ts": event.recv_ts,
                "available_at": event.available_at,
                "source_seq": event.source_seq,
                "exchange_batch_id": book_snapshot.exchange_batch.batch_id,
                "exchange_batch_seq": book_snapshot.exchange_batch.sequence,
                "source_exchange_batch_seq": event.exchange_batch_seq,
            },
        )

    def emit_book_snapshot(self, ref: BookSnapshotRef, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(ref, BookSnapshotRef):
            raise FoundationContractError("ref must be a BookSnapshotRef")
        self._validate_pair_product(ref.product)
        self._persist_snapshot("book", ref.snapshot_hash, payload)
        return self.emit_row(
            "book_snapshots",
            ref.snapshot_id,
            {
                "snapshot_id": ref.snapshot_id,
                "snapshot_hash": ref.snapshot_hash,
                "product": ref.product,
                "book_seq": ref.book_seq,
                "feed_seq": ref.feed_seq,
                "event_id": ref.event_id,
                "recv_ts": ref.recv_ts,
                "available_at": ref.exchange_batch.exchange_ts if ref.exchange_batch is not None else ref.available_at,
                "exchange_batch_id": None if ref.exchange_batch is None else ref.exchange_batch.batch_id,
                "exchange_batch_seq": None if ref.exchange_batch is None else ref.exchange_batch.sequence,
                "exchange_ts": None if ref.exchange_batch is None else ref.exchange_batch.exchange_ts,
                "artifact_path": self._snapshot_relative_path("book", ref.snapshot_hash),
            },
        )

    def emit_signal_snapshot(self, ref: SignalSnapshotRef, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(ref, SignalSnapshotRef):
            raise FoundationContractError("ref must be a SignalSnapshotRef")
        self._validate_pair_product(ref.product)
        self._persist_snapshot("signal", ref.snapshot_hash, payload)
        return self.emit_row(
            "signal_snapshots",
            ref.snapshot_id,
            {
                "snapshot_id": ref.snapshot_id,
                "snapshot_hash": ref.snapshot_hash,
                "signal_id": ref.signal_id,
                "product": ref.product,
                "feed_seq": ref.feed_seq,
                "event_id": ref.event_id,
                "available_at": ref.exchange_batch.exchange_ts if ref.exchange_batch is not None else ref.available_at,
                "exchange_batch_id": None if ref.exchange_batch is None else ref.exchange_batch.batch_id,
                "exchange_batch_seq": None if ref.exchange_batch is None else ref.exchange_batch.sequence,
                "exchange_ts": None if ref.exchange_batch is None else ref.exchange_batch.exchange_ts,
                "artifact_path": self._snapshot_relative_path("signal", ref.snapshot_hash),
            },
        )

    def emit_decision(self, context: DecisionContext) -> Mapping[str, Any]:
        self._validate_context(context)
        return self.emit_row(
            "decisions",
            context.decision_id,
            {
                "decision_id": context.decision_id,
                "dec_ts": context.dec_ts,
                "feed_seq": context.feed_seq,
                "quoted_book_snapshot_id": context.quoted_book.snapshot_id,
                "hedge_book_snapshot_id": context.hedge_book.snapshot_id,
                "consumed_signal_snapshot_ids": [signal.snapshot_id for signal in context.consumed_signals],
                "input_ages_ms": context.input_ages_ms,
                "exchange_batch_id": None if context.exchange_batch is None else context.exchange_batch.batch_id,
                "exchange_batch_seq": None if context.exchange_batch is None else context.exchange_batch.sequence,
                "exchange_ts": None if context.exchange_batch is None else context.exchange_batch.exchange_ts,
                "previous_exchange_batch_id": (
                    None if context.previous_quoted_book is None or context.previous_quoted_book.exchange_batch is None
                    else context.previous_quoted_book.exchange_batch.batch_id
                ),
                "interval_id": context.interval_id,
                "observed_fill_ids": list(context.observed_fill_ids),
            },
        )

    def emit_order(self, intent: Any, context: DecisionContext) -> Mapping[str, Any]:
        from common.foundation_contracts import OrderIntent

        if not isinstance(intent, OrderIntent):
            raise FoundationContractError("intent must be an OrderIntent")
        self._validate_context(context)
        if (
            intent.run_id != self.run_id
            or intent.decision_id != context.decision_id
            or intent.hedge_pair != self.hedge_pair
            or intent.product not in (context.quoted_product, context.hedge_product)
        ):
            raise FoundationContractError("order does not match telemetry context")
        return self.emit_row(
            "orders",
            f"{intent.intent_id}:declared",
            {
                "order_id": intent.intent_id,
                "decision_id": intent.decision_id,
                "product": intent.product,
                "record_type": "order_declared",
                "occurred_at": context.dec_ts,
                "role": intent.role.value,
                "side": intent.side.value,
                "requested_qty": intent.requested_qty,
                "limit_price": intent.limit_price,
                "execution_model_ref": (
                    None
                    if intent.execution_model_ref is None
                    else {"model_id": intent.execution_model_ref.model_id, "version": intent.execution_model_ref.version}
                ),
                "pricing_reference": (
                    None
                    if intent.pricing_reference is None
                    else {
                        "pricing_batch_id": intent.pricing_reference.pricing_batch.batch_id,
                        "pricing_batch_seq": intent.pricing_reference.pricing_batch.sequence,
                        "pricing_snapshot_id": intent.pricing_reference.pricing_snapshot_id,
                        "basis": intent.pricing_reference.basis,
                        "trigger_fill_id": intent.pricing_reference.trigger_fill_id,
                    }
                ),
            },
        )

    def emit_lifecycle(self, event: IntentLifecycleEvent) -> Mapping[str, Any]:
        if not isinstance(event, IntentLifecycleEvent):
            raise FoundationContractError("event must be an IntentLifecycleEvent")
        self._validate_event_identity(event.run_id, event.hedge_pair, event.product)
        return self.emit_row(
            "orders",
            event.event_id,
            {
                "order_id": event.intent_id,
                "decision_id": event.decision_id,
                "product": event.product,
                "record_type": "lifecycle",
                "occurred_at": event.occurred_at,
                "lifecycle_state": event.state.value,
                "filled_qty": event.filled_qty,
                "residual_qty": event.residual_qty,
                "execution_id": event.execution_id,
                "disposition_reason": event.disposition_reason,
            },
        )

    def emit_reservation(self, event: CapacityReservationEvent, max_reserved_qty: int) -> Mapping[str, Any]:
        if not isinstance(event, CapacityReservationEvent):
            raise FoundationContractError("event must be a CapacityReservationEvent")
        self._validate_event_identity(event.run_id, event.hedge_pair, event.product)
        if not isinstance(max_reserved_qty, int) or max_reserved_qty <= 0:
            raise FoundationContractError("max_reserved_qty must be a positive integer")
        return self.emit_row(
            "orders",
            event.reservation_id,
            {
                "order_id": event.intent_id,
                "decision_id": event.decision_id,
                "product": event.product,
                "record_type": "capacity_reservation",
                "occurred_at": event.occurred_at,
                "reservation_id": event.reservation_id,
                "envelope_id": event.envelope_id,
                "reservation_action": event.action.value,
                "amount": event.amount,
                "max_reserved_qty": max_reserved_qty,
            },
        )

    def emit_ledger_effect(self, event: LedgerEvent) -> Mapping[str, Any]:
        if not isinstance(event, LedgerEvent):
            raise FoundationContractError("event must be a LedgerEvent")
        self._validate_event_identity(event.run_id, event.hedge_pair, event.product)
        intent_id = event.attributes.get("intent_id")
        _require_text(intent_id, "ledger event attributes.intent_id")
        return self.emit_row(
            "fills",
            event.event_id,
            {
                "fill_id": event.event_id,
                "order_id": intent_id,
                "decision_id": event.decision_id,
                "product": event.product,
                "record_type": "ledger_effect",
                "quantity": abs(event.position_delta),
                "position_delta": event.position_delta,
                "source_event_id": event.source_event_id,
                "matched_passive_fill_id": event.attributes.get("passive_fill_evidence_id"),
                "order_role": event.attributes.get("order_role"),
                "fee": event.fee,
                "rebate": event.rebate,
                "occurred_at": event.occurred_at,
            },
        )

    def emit_passive_fill_evidence(self, evidence: PassiveFillEvidence) -> Mapping[str, Any]:
        """Persist matcher-issued evidence separately from the derived ledger effect."""
        if not isinstance(evidence, PassiveFillEvidence):
            raise FoundationContractError("evidence must be a PassiveFillEvidence")
        self._validate_event_identity(evidence.run_id, evidence.hedge_pair, evidence.product)
        position_delta = evidence.fill_qty if evidence.side.value == "buy" else -evidence.fill_qty
        return self.emit_row(
            "fills",
            evidence.fill_id,
            {
                "fill_id": evidence.fill_id,
                "order_id": evidence.intent_id,
                "decision_id": evidence.decision_id,
                "product": evidence.product,
                "record_type": "passive_match_evidence",
                "quantity": evidence.fill_qty,
                "position_delta": position_delta,
                "matched_trade_ref": evidence.trade_reference,
                "matched_trade_quantity": evidence.trade_quantity,
                "matched_trade_source_event_id": evidence.trade_source_event_id,
                "fill_ts": evidence.fill_ts,
                "feed_seq": evidence.feed_seq,
                "book_snapshot_id": evidence.book_snapshot.snapshot_id,
                "queue_ahead_submit": evidence.queue_ahead_submit,
                "queue_ahead_fill": evidence.queue_ahead_fill,
                "liquidity_role": "maker",
                "fee_rebate": evidence.fee_rebate,
            },
        )

    def emit_snapshot_interval_proxy_evidence(
        self, evidence: SnapshotIntervalQueueProxyEvidence
    ) -> Mapping[str, Any]:
        """Persist matcher-issued snapshot-proxy evidence without a fictitious trade."""
        if not isinstance(evidence, SnapshotIntervalQueueProxyEvidence):
            raise FoundationContractError("evidence must be a SnapshotIntervalQueueProxyEvidence")
        self._validate_event_identity(evidence.run_id, evidence.hedge_pair, evidence.product)
        position_delta = evidence.fill_qty if evidence.side.value == "buy" else -evidence.fill_qty
        return self.emit_row(
            "fills",
            evidence.fill_id,
            {
                "fill_id": evidence.fill_id,
                "order_id": evidence.intent_id,
                "decision_id": evidence.decision_id,
                "product": evidence.product,
                "record_type": "snapshot_interval_proxy_evidence",
                "quantity": evidence.fill_qty,
                "position_delta": position_delta,
                "match_evidence_type": "snapshot_interval_queue_proxy_v1",
                "matched_interval_ref": evidence.interval_reference,
                "matched_interval_quantity": evidence.interval_quantity,
                "matched_interval_bucket_index": evidence.bucket_index,
                "matched_interval_bucket_price": evidence.bucket_price,
                "matched_interval_bucket_quantity": evidence.bucket_quantity,
                "raw_file_id": evidence.raw_file_id,
                "raw_file_hash": evidence.raw_file_hash,
                "raw_row_ordinal": evidence.raw_row_ordinal,
                "proxy_model_version": evidence.model_version,
                "price_reach_rule": evidence.price_reach_rule,
                "availability_convention": evidence.availability_convention,
                "fill_ts": evidence.fill_ts,
                "feed_seq": evidence.feed_seq,
                "book_snapshot_id": evidence.book_snapshot.snapshot_id,
                "queue_ahead_submit": evidence.queue_ahead_submit,
                "queue_ahead_fill": evidence.queue_ahead_fill,
                "liquidity_role": "maker",
                "fee_rebate": evidence.fee_rebate,
            },
        )

    def emit_execution(self, result: ExecutionResult) -> Mapping[str, Any]:
        if not isinstance(result, ExecutionResult):
            raise FoundationContractError("result must be an ExecutionResult")
        self._validate_event_identity(result.run_id, result.hedge_pair, result.product)
        return self.emit_row(
            "hedge_executions",
            result.execution_id,
            {
                "execution_id": result.execution_id,
                "order_id": result.intent_id,
                "decision_id": result.decision_id,
                "product": result.product,
                "status": result.status.value,
                "requested_qty": result.requested_qty,
                "filled_qty": result.filled_qty,
                "residual_qty": result.residual_qty,
                "book_snapshot_id": result.book_snapshot.snapshot_id,
                "decision_feed_seq": result.decision_feed_seq,
                "execution_feed_seq": result.execution_feed_seq,
                "decision_book_snapshot_id": result.decision_book_snapshot.snapshot_id,
                "executed_at": result.executed_at,
                "levels": [{"price": level.price, "quantity": level.quantity} for level in result.levels],
                "vwap": result.vwap,
                "disposition_reason": result.disposition_reason,
            },
        )

    def emit_trigger_evaluation(
        self,
        trigger_id: str,
        context: DecisionContext,
        occurred_at: datetime,
        attributes: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_text(trigger_id, "trigger_id")
        self._validate_context(context)
        _require_aware(occurred_at, "occurred_at")
        return self.emit_row(
            "trigger_evaluations",
            trigger_id,
            {
                "trigger_id": trigger_id,
                "decision_id": context.decision_id,
                "occurred_at": occurred_at,
                "attributes": attributes,
            },
        )

    def emit_inventory(self, inventory_id: str, state: DualLegLedgerState, occurred_at: datetime) -> Mapping[str, Any]:
        _require_text(inventory_id, "inventory_id")
        if not isinstance(state, DualLegLedgerState):
            raise FoundationContractError("state must be a DualLegLedgerState")
        self._validate_event_identity(state.run_id, state.hedge_pair, state.hedge_pair.quoted_product)
        _require_aware(occurred_at, "occurred_at")
        return self.emit_row(
            "inventory_series",
            inventory_id,
            {
                "inventory_id": inventory_id,
                "occurred_at": occurred_at,
                "quoted_position": state.quoted_position,
                "hedge_position": state.hedge_position,
                "pending_hedge_quantity": state.pending_hedge_quantity,
                "residual_risk": state.residual_risk,
                "total_fees": state.total_fees,
                "total_rebates": state.total_rebates,
            },
        )

    def emit_outcome_pnl(self, outcome_id: str, reason: str) -> Mapping[str, Any]:
        """Emit a non-attributed marker when the run has no Phase-4c PnL result."""
        _require_text(outcome_id, "outcome_id")
        _require_text(reason, "reason")
        return self.emit_row(
            "outcome_pnl",
            outcome_id,
            {
                "outcome_id": outcome_id,
                "attribution_status": "not_attributed",
                "economics_eligible": False,
                "reason": reason,
            },
        )

    def emit_pnl_attribution(self, attribution: PnlAttributionResult) -> Mapping[str, Any]:
        """Persist the canonical Phase-4c waterfall and reconciliation verdict."""
        if not isinstance(attribution, PnlAttributionResult):
            raise FoundationContractError("attribution must be a PnlAttributionResult")
        if attribution.run_id != self.run_id or attribution.hedge_pair != self.hedge_pair:
            raise FoundationContractError("PnL attribution does not belong to this telemetry run and pair")
        return self.emit_row(
            "outcome_pnl",
            attribution.attribution_id,
            {
                "outcome_id": attribution.attribution_id,
                "attribution_status": "reconciled" if attribution.economics_eligible else "unreconciled",
                "economics_eligible": attribution.economics_eligible,
                "maker_capture": attribution.maker_capture,
                "quoted_leg_price_pnl": attribution.quoted_leg_price_pnl,
                "hedge_leg_price_pnl": attribution.hedge_leg_price_pnl,
                "hedge_execution_shortfall": attribution.hedge_execution_shortfall,
                "fees": attribution.fees,
                "rebates": attribution.rebates,
                "waterfall_total": attribution.waterfall_total,
                "residual_basis_pnl": attribution.residual_basis_pnl,
                "accounting_total_pnl": attribution.accounting_total_pnl,
                "cycle_total_pnl": attribution.cycle_total_pnl,
                "reconciliation_residual": attribution.reconciliation_residual,
                "cycle_reconciliation_residual": attribution.cycle_reconciliation_residual,
                "telemetry_reconciled": attribution.telemetry_reconciled,
                "eod_reconciled": attribution.eod_reconciled,
                "reconciliation_failures": list(attribution.reconciliation_failures),
                "tolerance": attribution.tolerance,
                "attributed_ledger_event_ids": [effect.ledger_event_id for effect in attribution.effects],
            },
        )

    def capture_provenance(self, trial: TrialDeclaration, artifacts: Mapping[str, Any]) -> RunProvenance:
        """Persist one complete content-hashed provenance set for this trial run."""
        self._ensure_open()
        if self._provenance is not None:
            raise FoundationContractError("run provenance is immutable and already captured")
        if not isinstance(trial, TrialDeclaration) or trial.hedge_pair != self.hedge_pair:
            raise FoundationContractError("trial declaration must bind this telemetry hedge pair")
        if not isinstance(artifacts, Mapping):
            raise FoundationContractError("provenance artifacts must be a mapping")
        required = {
            "market_data",
            "signal_data",
            "configuration",
            "code",
            "schema",
            "fee_profile",
            "instrument_roll_mapping",
            "execution_models",
        }
        missing = required - set(artifacts)
        if missing:
            raise FoundationContractError("provenance artifacts are missing required content")
        hashes: dict[str, str] = {}
        artifact_dir = self.run_dir / "provenance" / "artifacts"
        for name in sorted(artifacts):
            if not _SAFE_SEGMENT.fullmatch(name):
                raise FoundationContractError("provenance artifact names must be safe path segments")
            content = _artifact_bytes(artifacts[name])
            hashes[name] = _sha256_bytes(content)
            self._atomic_write(artifact_dir / f"{name}.blob", content)
        manifest_core = {
            "run_id": self.run_id,
            "trial": self._trial_row(trial),
            "schema_version": self.schema.version,
            "artifact_hashes": hashes,
        }
        provenance = RunProvenance(
            self.run_id,
            trial,
            self.schema.version,
            hashes,
            _sha256(manifest_core),
        )
        self._atomic_write(
            self.run_dir / "provenance" / "manifest.json",
            _json_bytes({**manifest_core, "provenance_hash": provenance.provenance_hash}),
        )
        self._provenance = provenance
        return provenance

    def snapshot_payload(self, kind: str, snapshot_hash: str) -> Mapping[str, Any]:
        """Reconstruct and hash-check a durable payload by its recorded digest."""
        if kind not in {"book", "signal"}:
            raise FoundationContractError("snapshot kind must be 'book' or 'signal'")
        path = self._snapshot_path(kind, snapshot_hash)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise FoundationContractError("snapshot artifact is not retained") from exc
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FoundationContractError("snapshot artifact is not canonical JSON") from exc
        if _sha256(payload) != snapshot_hash:
            raise FoundationContractError("snapshot artifact hash does not match its reference")
        return _freeze(payload)

    def finalize(self) -> TelemetryRunResult:
        """Evaluate durable invariants and seal the run against further emission."""
        if self._result is not None:
            return self._result
        if self._provenance is None:
            raise FoundationContractError("provenance must be captured before a telemetry run can finalize")
        invariants = TelemetryInvariantChecker(self.run_dir, self.run_id, self.hedge_pair).evaluate()
        result = TelemetryRunResult(
            self.run_id,
            not any(not invariant.passed and invariant.severity is InvariantSeverity.ERROR for invariant in invariants),
            self._provenance,
            invariants,
        )
        self._append_jsonl(
            self.run_dir / "meta" / "invariants.jsonl",
            {
                "run_id": self.run_id,
                "schema_version": self.schema.version,
                "eligible": result.eligible,
                "provenance_hash": self._provenance.provenance_hash,
                "invariants": [
                    {
                        "invariant_id": item.invariant_id,
                        "passed": item.passed,
                        "severity": item.severity.value,
                        "message": item.message,
                        "related_ids": list(item.related_ids),
                    }
                    for item in invariants
                ],
            },
        )
        self._atomic_write(
            self.run_dir / "meta" / "run_result.json",
            _json_bytes(
                {
                    "run_id": result.run_id,
                    "eligible": result.eligible,
                    "provenance_hash": result.provenance.provenance_hash,
                    "invariant_ids": [item.invariant_id for item in result.invariants],
                }
            ),
        )
        self._result = result
        return result

    def _persist_snapshot(self, kind: str, snapshot_hash: str, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise FoundationContractError("snapshot payload must be a mapping")
        if _sha256(payload) != snapshot_hash:
            raise FoundationContractError("snapshot payload does not match its immutable snapshot hash")
        path = self._snapshot_path(kind, snapshot_hash)
        if path.exists():
            if path.read_bytes() != _json_bytes(payload):
                raise FoundationContractError("snapshot hash collision has non-identical payloads")
            return
        self._atomic_write(path, _json_bytes(payload))

    def _snapshot_path(self, kind: str, snapshot_hash: str) -> Path:
        if not isinstance(snapshot_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_hash):
            raise FoundationContractError("snapshot hash must be a sha256 digest")
        return self.run_dir / "snapshots" / kind / f"{snapshot_hash.split(':', 1)[1]}.json"

    def _snapshot_relative_path(self, kind: str, snapshot_hash: str) -> str:
        return str(self._snapshot_path(kind, snapshot_hash).relative_to(self.run_dir)).replace("\\", "/")

    def _validate_context(self, context: DecisionContext) -> None:
        if not isinstance(context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        if context.run_id != self.run_id or context.hedge_pair != self.hedge_pair:
            raise FoundationContractError("decision context does not belong to this telemetry run and pair")

    def _validate_event_identity(self, run_id: str, pair: HedgePairRef, product: str) -> None:
        if run_id != self.run_id or pair != self.hedge_pair:
            raise FoundationContractError("event does not belong to this telemetry run and pair")
        self._validate_pair_product(product)

    def _validate_pair_product(self, product: str) -> None:
        if product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("telemetry product must belong to the hedge pair")

    def _ensure_open(self) -> None:
        if self._result is not None:
            raise FoundationContractError("telemetry run is finalized and immutable")

    @staticmethod
    def _trial_row(trial: TrialDeclaration) -> Mapping[str, Any]:
        return {
            "trial_id": trial.trial_id,
            "development_window": trial.development_window,
            "calibration_window": trial.calibration_window,
            "holdout_window": trial.holdout_window,
            "candidate_freeze_decision": trial.candidate_freeze_decision,
            "policy_version": trial.policy_version,
            **_pair_fields(trial.hedge_pair),
            "execution_models": [
                {"model_id": model.model_id, "version": model.version} for model in trial.execution_models
            ],
            "data_cleaning_transforms": list(trial.data_cleaning_transforms),
        }

    @staticmethod
    def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
        payload = _json_bytes(row) + b"\n"
        with path.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        if temporary.exists():
            raise FoundationContractError("unexpected existing telemetry temporary artifact")
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


class TelemetryInvariantChecker:
    """Evaluate generic G5 invariants from streamed artifacts without row retention."""

    def __init__(self, run_dir: str | Path, run_id: str, hedge_pair: HedgePairRef) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.run_id = run_id
        self.hedge_pair = hedge_pair
        self._failures: dict[str, list[str]] = {}

    def evaluate(self) -> tuple[InvariantResult, ...]:
        database = self.run_dir / "meta" / "validation.sqlite3"
        connection = sqlite3.connect(database)
        try:
            self._create_schema(connection)
            self._load_tables(connection)
            self._check_table_coverage()
            self._check_snapshot_reconstruction(connection)
            self._check_causality(connection)
            self._check_execution(connection)
            self._check_capacity(connection)
            self._check_lifecycle(connection)
            self._check_fill_joins_and_ledger(connection)
            self._check_verified_passive_fills(connection)
            self._check_outcomes(connection)
            return self._results()
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE snapshots (
                snapshot_id TEXT PRIMARY KEY, kind TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
                feed_seq INTEGER NOT NULL, available_at TEXT NOT NULL, artifact_path TEXT NOT NULL, product TEXT NOT NULL
            );
            CREATE TABLE decisions (
                row_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, feed_seq INTEGER NOT NULL, dec_ts TEXT NOT NULL,
                quoted_snapshot TEXT NOT NULL, hedge_snapshot TEXT NOT NULL, signals TEXT NOT NULL
            );
            CREATE TABLE orders (
                row_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, decision_id TEXT NOT NULL, record_type TEXT NOT NULL,
                lifecycle_state TEXT, occurred_at TEXT NOT NULL
            );
            CREATE TABLE fills (
                fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, decision_id TEXT NOT NULL, product TEXT NOT NULL,
                position_delta INTEGER NOT NULL, quantity INTEGER NOT NULL, record_type TEXT NOT NULL,
                matched_passive_fill_id TEXT, matched_trade_ref TEXT, matched_trade_quantity INTEGER,
                match_evidence_type TEXT, matched_interval_ref TEXT, matched_interval_quantity INTEGER,
                matched_interval_bucket_index INTEGER, matched_interval_bucket_quantity INTEGER,
                raw_file_hash TEXT, raw_row_ordinal INTEGER,
                book_snapshot_id TEXT, feed_seq INTEGER, fill_ts TEXT, order_role TEXT
            );
            CREATE TABLE executions (
                execution_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, decision_id TEXT NOT NULL, product TEXT NOT NULL,
                status TEXT NOT NULL, book_snapshot_id TEXT NOT NULL, decision_book_snapshot_id TEXT,
                decision_feed_seq INTEGER NOT NULL, execution_feed_seq INTEGER NOT NULL, executed_at TEXT NOT NULL
            );
            CREATE TABLE reservations (
                row_id TEXT PRIMARY KEY, envelope_id TEXT NOT NULL, action TEXT NOT NULL, amount REAL NOT NULL,
                max_reserved_qty REAL NOT NULL, occurred_at TEXT NOT NULL
            );
            CREATE TABLE inventory (
                row_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, quoted_position INTEGER NOT NULL, hedge_position INTEGER NOT NULL
            );
            """
        )

    def _load_tables(self, connection: sqlite3.Connection) -> None:
        for table in S0_TELEMETRY_TABLES:
            path = self.run_dir / "tables" / f"{table}.jsonl"
            if not path.exists():
                continue
            with path.open("rb") as stream:
                for line_number, line in enumerate(stream, start=1):
                    try:
                        row = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._fail("telemetry.schema", f"{table}:{line_number} is not valid JSON", f"{table}:{line_number}")
                        continue
                    self._validate_base_row(table, row, line_number)
                    try:
                        self._index_row(connection, table, row)
                    except (sqlite3.IntegrityError, KeyError, TypeError, ValueError) as exc:
                        invariant_id = "execution.depth_use" if table == "hedge_executions" else "telemetry.schema"
                        self._fail(invariant_id, f"{table}:{line_number} violates durable row uniqueness/schema: {exc}", row.get("row_id", "unknown"))
        connection.commit()

    def _validate_base_row(self, table: str, row: Mapping[str, Any], line_number: int) -> None:
        if not isinstance(row, Mapping):
            self._fail("telemetry.schema", f"{table}:{line_number} is not a mapping", f"{table}:{line_number}")
            return
        missing = _TABLE_REQUIRED_FIELDS[table] - set(row)
        if missing:
            self._fail("telemetry.schema", f"{table}:{line_number} missing {sorted(missing)}", row.get("row_id", "unknown"))
        expected = {
            "run_id": self.run_id,
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "table": table,
            **_pair_fields(self.hedge_pair),
        }
        for field_name, expected_value in expected.items():
            if row.get(field_name) != expected_value:
                self._fail("telemetry.schema", f"{table}:{line_number} has invalid {field_name}", row.get("row_id", "unknown"))

    def _index_row(self, connection: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
        if table == "book_snapshots":
            connection.execute(
                "INSERT INTO snapshots VALUES (?, 'book', ?, ?, ?, ?, ?)",
                (row["snapshot_id"], row["snapshot_hash"], row["feed_seq"], row["available_at"], row["artifact_path"], row["product"]),
            )
        elif table == "signal_snapshots":
            connection.execute(
                "INSERT INTO snapshots VALUES (?, 'signal', ?, ?, ?, ?, ?)",
                (row["snapshot_id"], row["snapshot_hash"], row["feed_seq"], row["available_at"], row["artifact_path"], row["product"]),
            )
        elif table == "decisions":
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["row_id"],
                    row["decision_id"],
                    row["feed_seq"],
                    row["dec_ts"],
                    row["quoted_book_snapshot_id"],
                    row["hedge_book_snapshot_id"],
                    json.dumps(row["consumed_signal_snapshot_ids"], separators=(",", ":")),
                ),
            )
        elif table == "orders":
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["row_id"],
                    row["order_id"],
                    row["decision_id"],
                    row["record_type"],
                    row.get("lifecycle_state"),
                    row["occurred_at"],
                ),
            )
            if row["record_type"] == "capacity_reservation":
                connection.execute(
                    "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["row_id"],
                        row["envelope_id"],
                        row["reservation_action"],
                        row["amount"],
                        row["max_reserved_qty"],
                        row["occurred_at"],
                    ),
                )
        elif table == "fills":
            connection.execute(
                "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["fill_id"],
                    row["order_id"],
                    row["decision_id"],
                    row["product"],
                    row["position_delta"],
                    row["quantity"],
                    row["record_type"],
                    row.get("matched_passive_fill_id"),
                    row.get("matched_trade_ref"),
                    row.get("matched_trade_quantity"),
                    row.get("match_evidence_type"),
                    row.get("matched_interval_ref"),
                    row.get("matched_interval_quantity"),
                    row.get("matched_interval_bucket_index"),
                    row.get("matched_interval_bucket_quantity"),
                    row.get("raw_file_hash"),
                    row.get("raw_row_ordinal"),
                    row.get("book_snapshot_id"),
                    row.get("feed_seq"),
                    row.get("fill_ts"),
                    row.get("order_role"),
                ),
            )
        elif table == "hedge_executions":
            connection.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["execution_id"],
                    row["order_id"],
                    row["decision_id"],
                    row["product"],
                    row["status"],
                    row["book_snapshot_id"],
                    row.get("decision_book_snapshot_id", row["book_snapshot_id"]),
                    row["decision_feed_seq"],
                    row["execution_feed_seq"],
                    row["executed_at"],
                ),
            )
        elif table == "inventory_series":
            connection.execute(
                "INSERT INTO inventory VALUES (?, ?, ?, ?)",
                (row["row_id"], row["occurred_at"], row["quoted_position"], row["hedge_position"]),
            )

    def _check_table_coverage(self) -> None:
        missing = [table for table in S0_TELEMETRY_TABLES if not (self.run_dir / "tables" / f"{table}.jsonl").exists()]
        self._record("telemetry.table_coverage", not missing, "all S0 telemetry tables emitted" if not missing else "missing tables", missing)

    def _check_snapshot_reconstruction(self, connection: sqlite3.Connection) -> None:
        failures: list[str] = []
        for snapshot_id, snapshot_hash, artifact_path in connection.execute("SELECT snapshot_id, snapshot_hash, artifact_path FROM snapshots"):
            try:
                path = (self.run_dir / artifact_path).resolve()
                path.relative_to(self.run_dir)
                payload = json.loads(path.read_text(encoding="utf-8"))
                valid = _sha256(payload) == snapshot_hash
            except (OSError, ValueError, json.JSONDecodeError, FoundationContractError):
                valid = False
            if not valid:
                failures.append(snapshot_id)
        self._record(
            "snapshot.reconstruction",
            not failures,
            "all retained snapshot artifacts reconstruct by hash" if not failures else "snapshot artifact missing/corrupt/hash-mismatched",
            failures,
        )

    def _check_causality(self, connection: sqlite3.Connection) -> None:
        failures: list[str] = []
        for decision_id, feed_seq, dec_ts, quoted_id, hedge_id, signals_json in connection.execute(
            "SELECT decision_id, feed_seq, dec_ts, quoted_snapshot, hedge_snapshot, signals FROM decisions"
        ):
            for snapshot_id in (quoted_id, hedge_id, *json.loads(signals_json)):
                row = connection.execute(
                    "SELECT feed_seq, available_at FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
                ).fetchone()
                try:
                    snapshot_feed_seq = int(row[0]) if row is not None else None
                    snapshot_available_at = _parse_aware_timestamp(row[1], "snapshot available_at") if row else None
                    decision_at = _parse_aware_timestamp(dec_ts, "decision dec_ts")
                except (TypeError, ValueError, FoundationContractError):
                    failures.append(decision_id)
                    break
                if (
                    row is None
                    or snapshot_feed_seq is None
                    or snapshot_feed_seq > int(feed_seq)
                    or snapshot_available_at is None
                    or snapshot_available_at > decision_at
                ):
                    failures.append(decision_id)
                    break
        self._record(
            "causality.snapshot_availability",
            not failures,
            "decisions use only retained causal snapshots" if not failures else "decision references missing, late, or future snapshot",
            failures,
        )

    def _check_execution(self, connection: sqlite3.Connection) -> None:
        failures: list[str] = []
        for execution_id, status, snapshot_id, decision_snapshot_id, decision_feed_seq, execution_feed_seq, executed_at, decision_id, product in connection.execute(
            "SELECT execution_id, status, book_snapshot_id, decision_book_snapshot_id, decision_feed_seq, execution_feed_seq, executed_at, decision_id, product FROM executions"
        ):
            if status not in _SUCCESSFUL_EXECUTION_STATUSES:
                continue
            snapshot = connection.execute("SELECT feed_seq, available_at, product FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            decision = connection.execute(
                "SELECT feed_seq, quoted_snapshot, hedge_snapshot FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            expected_snapshot = (
                decision[1]
                if decision is not None and product == self.hedge_pair.quoted_product
                else decision[2]
                if decision is not None and product == self.hedge_pair.hedge_product
                else None
            )
            if (
                snapshot is None
                or decision is None
                or int(snapshot[0]) > int(execution_feed_seq)
                or snapshot[2] != product
                or int(decision[0]) != int(decision_feed_seq)
                or decision_snapshot_id != expected_snapshot
                or int(execution_feed_seq) < int(decision_feed_seq)
                or _parse_aware_timestamp(snapshot[1], "execution snapshot available_at")
                > _parse_aware_timestamp(executed_at, "execution executed_at")
            ):
                failures.append(execution_id)
        self._record(
            "execution.depth_use",
            not failures,
            "successful executions use their decision-bound retained book once per execution ID"
            if not failures
            else "successful execution uses a missing, stale, or decision-mismatched book",
            failures,
        )

    def _check_capacity(self, connection: sqlite3.Connection) -> None:
        failures: list[str] = []
        rows = connection.execute(
            """
            SELECT envelope_id, row_id, max_reserved_qty,
                SUM(CASE action WHEN 'reserve' THEN amount WHEN 'release' THEN -amount ELSE 0 END)
                    OVER (PARTITION BY envelope_id ORDER BY occurred_at, row_id) AS reserved
            FROM reservations
            """
        )
        for envelope_id, row_id, maximum, reserved in rows:
            if float(reserved) < -1e-12 or float(reserved) > float(maximum) + 1e-12:
                failures.append(f"{envelope_id}:{row_id}")
        self._record(
            "capacity.envelope",
            not failures,
            "capacity reservations remain within declared envelopes" if not failures else "capacity reservation underflow/overflow",
            failures,
        )

    def _check_lifecycle(self, connection: sqlite3.Connection) -> None:
        failures = [
            row[0]
            for row in connection.execute(
                """
                SELECT order_id FROM orders GROUP BY order_id
                HAVING SUM(CASE WHEN lifecycle_state IS NOT NULL THEN 1 ELSE 0 END) = 0
                    OR SUM(CASE WHEN lifecycle_state IN ('filled','cancelled','expired','rejected','stale','deadline','failed') THEN 1 ELSE 0 END) = 0
                """
            )
        ]
        self._record(
            "lifecycle.finality",
            not failures,
            "every order has a terminal lifecycle state" if not failures else "order has no terminal lifecycle state",
            failures,
        )

    def _check_fill_joins_and_ledger(self, connection: sqlite3.Connection) -> None:
        orphan_fills = [
            row[0]
            for row in connection.execute(
                """
                SELECT f.fill_id FROM fills f
                WHERE NOT EXISTS (
                    SELECT 1 FROM orders o WHERE o.order_id = f.order_id AND o.decision_id = f.decision_id
                )
                """
            )
        ]
        self._record(
            "joins.fill_order_decision",
            not orphan_fills,
            "every fill joins to one declared order/decision" if not orphan_fills else "orphan fill or decision mismatch",
            orphan_fills,
        )
        fill_count = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        inventory = connection.execute(
            "SELECT quoted_position, hedge_position FROM inventory ORDER BY occurred_at DESC, row_id DESC LIMIT 1"
        ).fetchone()
        failures: list[str] = []
        if fill_count and inventory is None:
            failures.append("inventory_series")
        elif inventory is not None:
            quoted_sum = connection.execute(
                "SELECT COALESCE(SUM(position_delta), 0) FROM fills WHERE product = ? AND record_type = 'ledger_effect'",
                (self.hedge_pair.quoted_product,),
            ).fetchone()[0]
            hedge_sum = connection.execute(
                "SELECT COALESCE(SUM(position_delta), 0) FROM fills WHERE product = ? AND record_type = 'ledger_effect'",
                (self.hedge_pair.hedge_product,),
            ).fetchone()[0]
            if int(quoted_sum) != int(inventory[0]) or int(hedge_sum) != int(inventory[1]):
                failures.append("inventory_series")
        self._record(
            "ledger.quantity",
            not failures,
            "ledger fill deltas reconcile to inventory" if not failures else "ledger fill quantities do not reconcile to inventory",
            failures,
        )

    def _check_verified_passive_fills(self, connection: sqlite3.Connection) -> None:
        """Require causal matcher evidence when the run declares production passive matching."""
        controls_path = self.run_dir / "meta" / "run_controls.json"
        try:
            controls = json.loads(controls_path.read_text(encoding="utf-8")) if controls_path.exists() else {}
            required = controls.get("require_verified_passive_fills", False)
        except (OSError, json.JSONDecodeError):
            required = True
        if required is not True:
            self._record(
                "passive.fill_evidence",
                True,
                "verified passive fill evidence is not required for this compatibility run",
                (),
            )
            return
        failures: list[str] = []
        evidence_ids: set[str] = set()
        for (
            fill_id,
            product,
            record_type,
            trade_ref,
            trade_quantity,
            evidence_type,
            interval_ref,
            interval_quantity,
            bucket_index,
            bucket_quantity,
            raw_file_hash,
            raw_row_ordinal,
            snapshot_id,
            feed_seq,
            fill_ts,
        ) in connection.execute(
            """
            SELECT fill_id, product, record_type, matched_trade_ref, matched_trade_quantity,
                   match_evidence_type, matched_interval_ref, matched_interval_quantity,
                   matched_interval_bucket_index, matched_interval_bucket_quantity,
                   raw_file_hash, raw_row_ordinal, book_snapshot_id, feed_seq, fill_ts
            FROM fills
            WHERE record_type IN ('passive_match_evidence', 'snapshot_interval_proxy_evidence')
            """
        ):
            snapshot = connection.execute(
                "SELECT feed_seq, available_at, product FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            try:
                causal_snapshot = (
                    snapshot is not None
                    and snapshot[2] == product == self.hedge_pair.quoted_product
                    and int(snapshot[0]) <= int(feed_seq)
                    and _parse_aware_timestamp(snapshot[1], "passive snapshot available_at")
                    <= _parse_aware_timestamp(fill_ts, "passive fill_ts")
                )
                if record_type == "passive_match_evidence":
                    causal = (
                        causal_snapshot
                        and isinstance(trade_ref, str)
                        and ":" in trade_ref
                        and isinstance(trade_quantity, int)
                        and trade_quantity > 0
                    )
                else:
                    causal = (
                        causal_snapshot
                        and evidence_type == "snapshot_interval_queue_proxy_v1"
                        and isinstance(interval_ref, str)
                        and interval_ref
                        and isinstance(interval_quantity, int)
                        and interval_quantity > 0
                        and isinstance(bucket_index, int)
                        and bucket_index >= 0
                        and isinstance(bucket_quantity, int)
                        and 0 < bucket_quantity <= interval_quantity
                        and isinstance(raw_file_hash, str)
                        and raw_file_hash.startswith("sha256:")
                        and isinstance(raw_row_ordinal, int)
                        and raw_row_ordinal >= 0
                    )
            except (TypeError, ValueError, FoundationContractError):
                causal = False
            if not causal:
                failures.append(fill_id)
            evidence_ids.add(fill_id)
        for trade_ref, trade_quantity, filled_quantity in connection.execute(
            """
            SELECT matched_trade_ref, matched_trade_quantity, SUM(quantity)
            FROM fills
            WHERE record_type = 'passive_match_evidence'
            GROUP BY matched_trade_ref, matched_trade_quantity
            """
        ):
            if (
                not isinstance(trade_ref, str)
                or not trade_ref
                or not isinstance(trade_quantity, int)
                or trade_quantity <= 0
                or int(filled_quantity) > trade_quantity
            ):
                failures.append(str(trade_ref))
        for interval_ref, bucket_index, bucket_quantity, filled_quantity in connection.execute(
            """
            SELECT matched_interval_ref, matched_interval_bucket_index,
                   matched_interval_bucket_quantity, SUM(quantity)
            FROM fills
            WHERE record_type = 'snapshot_interval_proxy_evidence'
            GROUP BY matched_interval_ref, matched_interval_bucket_index, matched_interval_bucket_quantity
            """
        ):
            if (
                not isinstance(interval_ref, str)
                or not interval_ref
                or not isinstance(bucket_index, int)
                or bucket_index < 0
                or not isinstance(bucket_quantity, int)
                or bucket_quantity <= 0
                or int(filled_quantity) > bucket_quantity
            ):
                failures.append(str(interval_ref))
        for fill_id, matched_id in connection.execute(
            "SELECT fill_id, matched_passive_fill_id FROM fills WHERE record_type = 'ledger_effect' AND product = ? AND order_role = 'maker'",
            (self.hedge_pair.quoted_product,),
        ):
            if not isinstance(matched_id, str) or matched_id not in evidence_ids:
                failures.append(fill_id)
        self._record(
            "passive.fill_evidence",
            not failures,
            "all quoted-leg ledger effects have causal matcher evidence with conserved source quantities"
            if not failures
            else "quoted-leg ledger effect lacks valid or quantity-conserving passive match evidence",
            failures,
        )

    def _check_outcomes(self, connection: sqlite3.Connection) -> None:
        failures: list[str] = []
        path = self.run_dir / "tables" / "outcome_pnl.jsonl"
        if path.exists():
            with path.open("rb") as stream:
                for line in stream:
                    row = json.loads(line.decode("utf-8"))
                    status = row.get("attribution_status")
                    eligible = row.get("economics_eligible")
                    if status == "not_attributed" and eligible is False:
                        continue
                    if status == "reconciled" and eligible is True and self._reconciled_outcome_is_valid(row):
                        continue
                    if status == "unreconciled" and eligible is False:
                        failures.append(str(row.get("outcome_id", "unknown")))
                        continue
                    if status != "not_attributed" or eligible is not False:
                        failures.append(str(row.get("outcome_id", "unknown")))
        self._record(
            "outcome.economics_eligibility",
            not failures,
            "all attributed outcomes are reconciled and economics-eligible"
            if not failures
            else "outcome has an unreconciled or invalid economics eligibility claim",
            failures,
        )

    @staticmethod
    def _reconciled_outcome_is_valid(row: Mapping[str, Any]) -> bool:
        required = (
            "maker_capture",
            "quoted_leg_price_pnl",
            "hedge_leg_price_pnl",
            "hedge_execution_shortfall",
            "fees",
            "rebates",
            "waterfall_total",
            "residual_basis_pnl",
            "accounting_total_pnl",
            "cycle_total_pnl",
            "reconciliation_residual",
            "cycle_reconciliation_residual",
            "telemetry_reconciled",
            "eod_reconciled",
            "reconciliation_failures",
            "tolerance",
            "attributed_ledger_event_ids",
        )
        if any(field_name not in row for field_name in required):
            return False
        try:
            values = {field_name: float(row[field_name]) for field_name in required[:12]}
            tolerance = float(row["tolerance"])
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in (*values.values(), tolerance)) or tolerance < 0:
            return False
        if values["fees"] < 0 or values["rebates"] < 0:
            return False
        expected_total = (
            values["maker_capture"]
            + values["quoted_leg_price_pnl"]
            + values["hedge_leg_price_pnl"]
            - values["hedge_execution_shortfall"]
            - values["fees"]
            + values["rebates"]
        )
        if not math.isclose(values["waterfall_total"], expected_total, rel_tol=0.0, abs_tol=1e-12):
            return False
        if not math.isclose(
            values["residual_basis_pnl"],
            values["quoted_leg_price_pnl"] + values["hedge_leg_price_pnl"],
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return False
        if not math.isclose(
            values["reconciliation_residual"],
            values["accounting_total_pnl"] - values["waterfall_total"],
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return False
        if not math.isclose(
            values["cycle_reconciliation_residual"],
            values["cycle_total_pnl"] - values["waterfall_total"],
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return False
        event_ids = row["attributed_ledger_event_ids"]
        return (
            row["telemetry_reconciled"] is True
            and row["eod_reconciled"] is True
            and row["reconciliation_failures"] == []
            and isinstance(event_ids, list)
            and all(isinstance(event_id, str) and event_id for event_id in event_ids)
            and len(set(event_ids)) == len(event_ids)
            and abs(values["reconciliation_residual"]) <= tolerance
            and abs(values["cycle_reconciliation_residual"]) <= tolerance
        )

    def _record(self, invariant_id: str, passed: bool, message: str, related_ids: Iterable[str]) -> None:
        if not passed:
            self._failures[invariant_id] = list(dict.fromkeys(str(value) for value in related_ids))
        else:
            self._failures.setdefault(invariant_id, [])
        self._messages = getattr(self, "_messages", {})
        self._messages[invariant_id] = message

    def _fail(self, invariant_id: str, message: str, related_id: str) -> None:
        self._failures.setdefault(invariant_id, []).append(str(related_id))
        self._messages = getattr(self, "_messages", {})
        self._messages[invariant_id] = message

    def _results(self) -> tuple[InvariantResult, ...]:
        required = (
            "telemetry.schema",
            "telemetry.table_coverage",
            "snapshot.reconstruction",
            "causality.snapshot_availability",
            "execution.depth_use",
            "capacity.envelope",
            "lifecycle.finality",
            "joins.fill_order_decision",
            "ledger.quantity",
            "passive.fill_evidence",
            "outcome.economics_eligibility",
        )
        results: list[InvariantResult] = []
        for invariant_id in required:
            related_ids = tuple(dict.fromkeys(self._failures.get(invariant_id, ())))
            results.append(
                InvariantResult(
                    invariant_id,
                    not related_ids,
                    InvariantSeverity.ERROR,
                    self._messages.get(invariant_id, "passed"),
                    related_ids,
                )
            )
        return tuple(results)


def load_canonical_table(run_dir: str | Path, table: str) -> Iterable[Mapping[str, Any]]:
    """Stream canonical telemetry rows for reporting; never read strategy-local records."""
    if table not in S0_TELEMETRY_TABLES:
        raise FoundationContractError("table is not in the published S0 telemetry schema")
    path = Path(run_dir).resolve() / "tables" / f"{table}.jsonl"
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise FoundationContractError("canonical telemetry table is not retained") from exc
    with stream:
        for line in stream:
            try:
                yield MappingProxyType(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FoundationContractError("canonical telemetry table contains invalid JSON") from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = ["TelemetryEmitter", "TelemetryInvariantChecker", "load_canonical_table"]
