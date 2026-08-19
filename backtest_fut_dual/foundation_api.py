"""Versioned public dual-book adapter for the declared S0 maker-hedger scope.

``DualBookFoundation`` is deliberately a narrow coordinator, not a strategy.
It hands a policy only an immutable :class:`DecisionContext` and accepts only a
``MakerHedgeIntentBatch`` in return.  The facade owns the lifecycle registry,
capacity reservations, snapshot-bound depth execution, dual-leg ledger, EOD
completion, and canonical telemetry.  There is no public route to attach an
externally fabricated execution result or ledger effect.

The contract binds the current S0 programme: a passive quoted-leg maker intent
and an aggressive correlated hedge intent.  A policy can deliberately return
an empty batch.  A future policy shape (for example passive hedge-leg making)
requires a new API version rather than weakening this boundary implicitly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from common.execution import DepthBook, DepthExecutionService, DepthLevel
from common.foundation_contracts import (
    BookSnapshotRef,
    CapacityEnvelope,
    DecisionContext,
    DualLegLedgerState,
    EodCloseRequest,
    EodCompletion,
    ExecutionModelConfig,
    ExecutionModelRef,
    ExecutionResult,
    FoundationContractError,
    HedgeMappingSpec,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
    IntentLifecycleState,
    LedgerEvent,
    MakerHedgeIntentBatch,
    MakerQueueEvidence,
    OrderIntent,
    OrderRole,
    OrderSide,
    PassiveFillEvidence,
    PassiveTrade,
    PnlAccountingView,
    PnlAttributionResult,
    PnlPriceObservation,
    RunProvenance,
    SignalSnapshotRef,
    SnapshotInterval,
    SnapshotIntervalQueueProxyEvidence,
    TelemetryRunResult,
    TrialDeclaration,
)
from common.ledger import DualLegLedger, EodCompletionService
from common.lifecycle import IntentLifecycleService
from common.passive_matching import PassiveMatchingService
from common.pnl_attribution import PnlAttributionService
from common.telemetry import TelemetryEmitter
from common.stress import StressScenario, stressed_execution_models


FOUNDATION_API_VERSION = "1.4.0"


class MakerHedgePolicy(Protocol):
    """Minimal S0 policy protocol; it receives no mutable depth or services."""

    def propose(self, context: DecisionContext) -> MakerHedgeIntentBatch:
        """Return a passive-maker/aggressive-hedge declaration for ``context``."""


@dataclass(frozen=True)
class PolicyTrigger:
    """Immutable policy-owned trigger evidence attached to one decision."""

    trigger_id: str
    attributes: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_id, str) or not self.trigger_id.strip():
            raise FoundationContractError("trigger_id must be a non-empty string")
        if not isinstance(self.attributes, Mapping):
            raise FoundationContractError("policy trigger attributes must be a mapping")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True)
class PolicyProposal:
    """Production-policy result with a batch and policy-owned audit metadata."""

    batch: MakerHedgeIntentBatch
    decision_attributes: Mapping[str, Any] = field(default_factory=dict)
    triggers: tuple[PolicyTrigger, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.batch, MakerHedgeIntentBatch):
            raise FoundationContractError("policy proposal batch must be a MakerHedgeIntentBatch")
        if not isinstance(self.decision_attributes, Mapping):
            raise FoundationContractError("policy proposal decision_attributes must be a mapping")
        if not isinstance(self.triggers, tuple) or any(not isinstance(trigger, PolicyTrigger) for trigger in self.triggers):
            raise FoundationContractError("policy proposal triggers must be a tuple of PolicyTrigger values")
        trigger_ids = [trigger.trigger_id for trigger in self.triggers]
        if len(trigger_ids) != len(set(trigger_ids)):
            raise FoundationContractError("policy proposal trigger IDs must be unique per decision")
        object.__setattr__(self, "decision_attributes", MappingProxyType(dict(self.decision_attributes)))


class ProductionMakerHedgePolicy(Protocol):
    """Signal-aware policy contract required by the production replay adapter.

    Signal selection is a declaration over causal identities.  The subsequent
    DecisionContext supplies immutable value-bearing snapshots only for those
    declared identities through consumed_signal_values and signal_value().
    """

    def select_signal_ids(self, available_signals: tuple[SignalSnapshotRef, ...]) -> tuple[str, ...]:
        """Declare the causal signal IDs the policy will consume for one decision."""

    def propose(self, context: DecisionContext) -> PolicyProposal:
        """Return one typed declaration using only values in ``context``."""


@dataclass(frozen=True)
class FoundationSubmission:
    """Immutable receipt for a policy declaration accepted by the facade."""

    decision_id: str
    maker_intent_id: str | None
    hedge_intent_id: str | None


class DualBookFoundation:
    """Public-only S0 coordinator for one run and one dual-book hedge mapping.

    Market connectors register retained snapshots then call
    :meth:`ingest_depth_from_snapshot`.  The latter reconstructs the durable
    payload and derives the mutable execution depth internally, so policy code
    never receives or mutates a ``DepthBook``.  The retained payload uses the
    explicit canonical shape ``{"bids": [{"price", "quantity"}], "asks":
    [...]}``.
    """

    def __init__(
        self,
        *,
        run_id: str,
        hedge_mapping: HedgeMappingSpec,
        instrument_specs: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec],
        execution_models: Iterable[ExecutionModelConfig],
        default_execution_model: ExecutionModelRef,
        capacity_envelopes: Iterable[CapacityEnvelope],
        telemetry: TelemetryEmitter,
        stress_scenario: StressScenario | None = None,
        require_verified_passive_fills: bool = False,
        action_timing_managed_externally: bool = False,
        require_exchange_batch_pricing: bool = False,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise FoundationContractError("run_id must be a non-empty string")
        if not isinstance(hedge_mapping, HedgeMappingSpec):
            raise FoundationContractError("hedge_mapping must be a HedgeMappingSpec")
        if not isinstance(telemetry, TelemetryEmitter):
            raise FoundationContractError("telemetry must be a TelemetryEmitter")
        if telemetry.run_id != run_id or telemetry.hedge_pair != hedge_mapping.hedge_pair:
            raise FoundationContractError("telemetry must match the API run and hedge pair")

        specs = tuple(instrument_specs.values()) if isinstance(instrument_specs, Mapping) else tuple(instrument_specs)
        if any(not isinstance(spec, InstrumentSpec) for spec in specs):
            raise FoundationContractError("instrument_specs must contain InstrumentSpec values")
        configured_products = {spec.product for spec in specs}
        required_products = {hedge_mapping.hedge_pair.quoted_product, hedge_mapping.hedge_pair.hedge_product}
        if not required_products <= configured_products:
            raise FoundationContractError("instrument_specs must configure both products in the hedge mapping")
        models = tuple(execution_models)
        if stress_scenario is not None and not isinstance(stress_scenario, StressScenario):
            raise FoundationContractError("stress_scenario must be a StressScenario or None")
        if not isinstance(action_timing_managed_externally, bool):
            raise FoundationContractError("action_timing_managed_externally must be a bool")
        if not isinstance(require_verified_passive_fills, bool):
            raise FoundationContractError("require_verified_passive_fills must be a bool")
        stress_models = None if stress_scenario is None else stressed_execution_models(models, default_execution_model, stress_scenario)
        if stress_models is not None:
            models = stress_models.models
            default_execution_model = stress_models.default_execution_model
        envelopes = tuple(capacity_envelopes)
        self._run_id = run_id
        self._hedge_mapping = hedge_mapping
        self._telemetry = telemetry
        self._telemetry.set_run_controls(require_verified_passive_fills=require_verified_passive_fills)
        self._stress_scenario = stress_scenario
        self._action_timing_managed_externally = action_timing_managed_externally
        self._require_verified_passive_fills = require_verified_passive_fills
        if not isinstance(require_exchange_batch_pricing, bool):
            raise FoundationContractError("require_exchange_batch_pricing must be a bool")
        self._require_exchange_batch_pricing = require_exchange_batch_pricing
        self._instrument_specs = {spec.product: spec for spec in specs}
        self._stress_model_references = {} if stress_models is None else dict(stress_models.references)
        self._lifecycle = IntentLifecycleService(models, default_execution_model, envelopes)
        self._execution = DepthExecutionService(specs, models, default_execution_model)
        self._ledger = DualLegLedger(run_id, hedge_mapping, self._lifecycle)
        self._eod = EodCompletionService(self._lifecycle, self._execution, self._ledger)
        self._passive_match_authority = object()
        self._passive_matcher = PassiveMatchingService(self._passive_match_authority)
        self._pnl_observations: dict[str, PnlPriceObservation] = {}
        self._book_refs: dict[str, BookSnapshotRef] = {}
        self._signal_refs: dict[str, SignalSnapshotRef] = {}
        self._contexts: dict[str, DecisionContext] = {}
        self._maker_queue_evidence_by_intent: dict[str, MakerQueueEvidence] = {}
        self._recorded_book_event_ids: set[str] = set()
        self._emitted_decision_ids: set[str] = set()
        self._emitted_order_ids: set[str] = set()
        self._emitted_lifecycle_ids: set[str] = set()
        self._emitted_reservation_ids: set[str] = set()
        self._emitted_execution_ids: set[str] = set()
        self._emitted_ledger_ids: set[str] = set()
        self._execution_results_by_id: dict[str, ExecutionResult] = {}

    @property
    def api_version(self) -> str:
        """Return the published facade version used by this implementation."""
        return FOUNDATION_API_VERSION

    @property
    def stress_scenario(self) -> StressScenario | None:
        """Return the immutable declared stress profile, if this is a stress run."""
        return self._stress_scenario

    def record_book_event(self, event: IngressEvent, book_snapshot: BookSnapshotRef) -> None:
        """Persist one causal book event with its resolved batch identity."""
        if not isinstance(event, IngressEvent) or event.kind is not IngressKind.BOOK:
            raise FoundationContractError("event must be a book IngressEvent")
        if not isinstance(book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if event.event_id in self._recorded_book_event_ids:
            raise FoundationContractError("book ingress event is already recorded")
        self._telemetry.emit_book_event(event, book_snapshot)
        self._recorded_book_event_ids.add(event.event_id)

    def record_book_snapshot(self, ref: BookSnapshotRef, payload: Mapping[str, Any]) -> None:
        """Persist one immutable book snapshot before it can drive execution."""
        self._validate_snapshot_identity(ref)
        if ref.snapshot_id in self._book_refs:
            raise FoundationContractError("book snapshot is already recorded")
        self._telemetry.emit_book_snapshot(ref, payload)
        self._book_refs[ref.snapshot_id] = ref

    def record_signal_snapshot(self, ref: SignalSnapshotRef, payload: Mapping[str, Any]) -> None:
        """Persist one immutable consumed-signal snapshot."""
        self._validate_snapshot_identity(ref)
        if ref.snapshot_id in self._signal_refs:
            raise FoundationContractError("signal snapshot is already recorded")
        self._telemetry.emit_signal_snapshot(ref, payload)
        self._signal_refs[ref.snapshot_id] = ref

    def ingest_depth_from_snapshot(self, ref: BookSnapshotRef) -> None:
        """Derive execution depth from a retained, hash-checked book payload."""
        if not isinstance(ref, BookSnapshotRef):
            raise FoundationContractError("ref must be a BookSnapshotRef")
        if self._book_refs.get(ref.snapshot_id) != ref:
            raise FoundationContractError("execution depth requires a previously recorded identical book snapshot")
        payload = self._telemetry.snapshot_payload("book", ref.snapshot_hash)
        bids = self._payload_levels(payload, "bids")
        asks = self._payload_levels(payload, "asks")
        self._execution.ingest_book(DepthBook(ref, bids=bids, asks=asks))

    def propose(self, policy: MakerHedgePolicy, context: DecisionContext, *, occurred_at: datetime) -> FoundationSubmission:
        """Ask a policy for immutable intents, then submit them through this facade."""
        propose = getattr(policy, "propose", None)
        if not callable(propose):
            raise FoundationContractError("policy must provide callable propose(context)")
        batch = propose(context)
        if not isinstance(batch, MakerHedgeIntentBatch):
            raise FoundationContractError("policy.propose must return a MakerHedgeIntentBatch")
        return self.submit(batch, context, occurred_at=occurred_at)

    def submit(
        self,
        batch: MakerHedgeIntentBatch,
        context: DecisionContext,
        *,
        occurred_at: datetime,
    ) -> FoundationSubmission:
        """Register validated policy intents and emit their canonical declarations."""
        self._validate_context(context)
        if not isinstance(batch, MakerHedgeIntentBatch):
            raise FoundationContractError("batch must be a MakerHedgeIntentBatch")
        batch = self._stress_batch(batch)
        self._validate_batch(batch, context)
        occurred_at = self._submission_at(occurred_at)
        self._emit_decision(context)
        if batch.maker_intent is not None:
            self._lifecycle.submit_intent(
                batch.maker_intent,
                context,
                occurred_at=occurred_at,
                envelope_id=batch.maker_capacity_envelope_id,
            )
            self._sync_intent(batch.maker_intent.intent_id)
        if batch.hedge_intent is not None:
            self._lifecycle.submit_intent(batch.hedge_intent, context, occurred_at=occurred_at)
            self._execution.register_intent(batch.hedge_intent, context)
            self._sync_intent(batch.hedge_intent.intent_id)
        return FoundationSubmission(
            context.decision_id,
            None if batch.maker_intent is None else batch.maker_intent.intent_id,
            None if batch.hedge_intent is None else batch.hedge_intent.intent_id,
        )

    def arrive(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        passive_book_snapshot: BookSnapshotRef | None = None,
    ) -> IntentLifecycleState:
        """Record arrival and bind a maker's queue proxy to an arrival book."""
        occurred_at = self._arrival_at(occurred_at)
        intent = self._lifecycle.registered_intent(intent_id)
        queue_ahead_submit: int | None = None
        context = self._lifecycle.decision_context(intent_id)
        if intent.role is OrderRole.MAKER:
            if passive_book_snapshot is None and self._require_verified_passive_fills:
                raise FoundationContractError("verified passive fills require an arrival book snapshot")
            if passive_book_snapshot is not None:
                if self._book_refs.get(passive_book_snapshot.snapshot_id) != passive_book_snapshot:
                    raise FoundationContractError("maker arrival book snapshot must be recorded before order arrival")
                visible_at = (
                    passive_book_snapshot.exchange_batch.exchange_ts
                    if passive_book_snapshot.exchange_batch is not None
                    else passive_book_snapshot.available_at
                )
                if passive_book_snapshot.product != intent.product or visible_at > occurred_at:
                    raise FoundationContractError("maker arrival book snapshot is not visible at order arrival")
                queue_ahead_submit = self._queue_ahead_at_arrival(intent, passive_book_snapshot)
                self._maker_queue_evidence_by_intent[intent_id] = MakerQueueEvidence(
                    intent.intent_id,
                    intent.run_id,
                    intent.decision_id,
                    intent.hedge_pair,
                    intent.product,
                    occurred_at,
                    passive_book_snapshot,
                    queue_ahead_submit,
                )
        self._lifecycle.arrive(intent_id, occurred_at=occurred_at)
        if queue_ahead_submit is not None and passive_book_snapshot is not None:
            self._passive_matcher.register_intent(
                intent,
                context,
                queue_ahead_submit=queue_ahead_submit,
                arrival_book_snapshot=passive_book_snapshot,
                arrived_at=occurred_at,
            )
        self._sync_intent(intent_id)
        return self._lifecycle.state_of(intent_id)

    def record_passive_fill(
        self,
        intent_id: str,
        quantity: int,
        *,
        occurred_at: datetime,
        fee: float = 0.0,
        rebate: float = 0.0,
    ) -> IntentLifecycleState:
        """Compatibility-only passive fill route; production runs must verify matches."""
        if self._require_verified_passive_fills:
            raise FoundationContractError("production runs require matcher-derived PassiveFillEvidence")
        fee, rebate = self._stressed_costs(fee, rebate)
        self._validate_costs(fee, rebate)
        transition = self._lifecycle.record_passive_fill(intent_id, quantity, occurred_at=occurred_at)
        ledger_event = self._ledger.record_lifecycle(transition.event, fee=fee, rebate=rebate)
        if ledger_event is not None:
            context = self._lifecycle.decision_context(intent_id)
            self._capture_passive_observation(
                ledger_event.event_id,
                self._lifecycle.registered_intent(intent_id).limit_price,
                context,
            )
        self._sync_intent(intent_id)
        self._sync_ledger()
        return self._lifecycle.state_of(intent_id)

    def match_passive_trade(
        self, trade: PassiveTrade, *, fee_rebate_per_contract: float = 0.0
    ) -> tuple[PassiveFillEvidence, ...]:
        """Derive and record every verified maker fill caused by one trade event."""
        if (
            not isinstance(trade, PassiveTrade)
            or trade.run_id != self._run_id
            or trade.hedge_pair != self._hedge_mapping.hedge_pair
            or trade.product != self._hedge_mapping.hedge_pair.quoted_product
            or self._book_refs.get(trade.book_snapshot.snapshot_id) != trade.book_snapshot
        ):
            raise FoundationContractError("passive trade must use a retained quoted-leg book in this foundation run")
        stressed_fee_rebate = (
            fee_rebate_per_contract
            if self._stress_scenario is None
            else self._stress_scenario.adjusted_fee(fee_rebate_per_contract)
        )
        matches = self._passive_matcher.match_trade(trade, fee_rebate_per_contract=stressed_fee_rebate)
        for evidence in matches:
            self.record_passive_match(evidence)
        return matches

    def record_passive_match(self, evidence: PassiveFillEvidence) -> IntentLifecycleState:
        """Record a matcher-issued maker fill and its only valid ledger effect."""
        self._passive_matcher.validate_evidence(evidence)
        intent = self._lifecycle.registered_intent(evidence.intent_id)
        if (
            evidence.run_id != self._run_id
            or evidence.hedge_pair != self._hedge_mapping.hedge_pair
            or evidence.decision_id != intent.decision_id
            or evidence.product != intent.product
            or evidence.side is not intent.side
        ):
            raise FoundationContractError("passive fill evidence does not match its registered maker intent")
        fee = max(0.0, -float(evidence.fee_rebate))
        rebate = max(0.0, float(evidence.fee_rebate))
        transition = self._lifecycle.record_passive_fill(
            intent.intent_id, evidence.fill_qty, occurred_at=evidence.fill_ts
        )
        if transition.event.filled_qty != evidence.cumulative_fill_qty:
            raise FoundationContractError("passive fill evidence cumulative quantity does not match lifecycle state")
        self._telemetry.emit_passive_fill_evidence(evidence)
        ledger_event = self._ledger.record_lifecycle(
            transition.event,
            fee=fee,
            rebate=rebate,
            passive_fill_evidence_id=evidence.fill_id,
        )
        if ledger_event is not None:
            context = self._lifecycle.decision_context(intent.intent_id)
            self._capture_passive_observation(ledger_event.event_id, evidence.fill_price, context)
        self._passive_matcher.record_evidence(evidence)
        self._sync_intent(intent.intent_id)
        self._sync_ledger()
        return self._lifecycle.state_of(intent.intent_id)

    def match_snapshot_interval(
        self, interval: SnapshotInterval, *, fee_rebate_per_contract: float = 0.0
    ) -> tuple[SnapshotIntervalQueueProxyEvidence, ...]:
        """Derive and record proxy maker fills from one retained snapshot interval."""
        if (
            not isinstance(interval, SnapshotInterval)
            or interval.run_id != self._run_id
            or interval.hedge_pair != self._hedge_mapping.hedge_pair
            or interval.product != self._hedge_mapping.hedge_pair.quoted_product
            or self._book_refs.get(interval.book_snapshot.snapshot_id) != interval.book_snapshot
        ):
            raise FoundationContractError("snapshot interval must use a retained quoted-leg book in this foundation run")
        tick = float(self._instrument_specs[interval.product].tick)
        if any(
            not math.isclose(bucket.price / tick, round(bucket.price / tick), rel_tol=0.0, abs_tol=1e-9)
            for bucket in interval.buckets
        ):
            raise FoundationContractError("snapshot interval bucket price is off the declared tick grid")
        self._require_snapshot_proxy_depth()
        stressed_fee_rebate = (
            fee_rebate_per_contract
            if self._stress_scenario is None
            else self._stress_scenario.adjusted_fee(fee_rebate_per_contract)
        )
        matches = self._passive_matcher.match_snapshot_interval(
            interval, fee_rebate_per_contract=stressed_fee_rebate
        )
        for evidence in matches:
            self.record_snapshot_interval_match(evidence)
        return matches

    def record_snapshot_interval_match(self, evidence: SnapshotIntervalQueueProxyEvidence) -> IntentLifecycleState:
        """Record matcher-issued snapshot-proxy evidence and its ledger effect."""
        self._passive_matcher.validate_snapshot_proxy_evidence(evidence)
        intent = self._lifecycle.registered_intent(evidence.intent_id)
        if (
            evidence.run_id != self._run_id
            or evidence.hedge_pair != self._hedge_mapping.hedge_pair
            or evidence.decision_id != intent.decision_id
            or evidence.product != intent.product
            or evidence.side is not intent.side
        ):
            raise FoundationContractError("snapshot proxy fill evidence does not match its registered maker intent")
        fee = max(0.0, -float(evidence.fee_rebate))
        rebate = max(0.0, float(evidence.fee_rebate))
        transition = self._lifecycle.record_passive_fill(
            intent.intent_id, evidence.fill_qty, occurred_at=evidence.fill_ts
        )
        if transition.event.filled_qty != evidence.cumulative_fill_qty:
            raise FoundationContractError("snapshot proxy evidence cumulative quantity does not match lifecycle state")
        self._telemetry.emit_snapshot_interval_proxy_evidence(evidence)
        ledger_event = self._ledger.record_lifecycle(
            transition.event,
            fee=fee,
            rebate=rebate,
            passive_fill_evidence_id=evidence.fill_id,
        )
        if ledger_event is not None:
            context = self._lifecycle.decision_context(intent.intent_id)
            self._capture_passive_observation(ledger_event.event_id, evidence.fill_price, context)
        self._passive_matcher.record_snapshot_proxy_evidence(evidence)
        self._sync_intent(intent.intent_id)
        self._sync_ledger()
        return self._lifecycle.state_of(intent.intent_id)

    def execute_hedge(
        self,
        intent_id: str,
        *,
        executed_at: datetime,
        decision_mid: float | None = None,
        execution_feed_seq: int | None = None,
        fee: float = 0.0,
        rebate: float = 0.0,
    ) -> ExecutionResult:
        """Run one registered hedge intent against decision-bound retained depth."""
        executed_at = self._arrival_at(executed_at)
        fee, rebate = self._stressed_costs(fee, rebate)
        self._validate_costs(fee, rebate)
        intent = self._lifecycle.registered_intent(intent_id)
        if intent.role is not OrderRole.HEDGE:
            raise FoundationContractError("only a registered hedge intent may use execute_hedge")
        effective_decision_mid = decision_mid if self._stress_scenario is None else self._stress_scenario.adjusted_decision_mid(decision_mid)
        result = self._execution.execute(
            intent_id,
            executed_at=executed_at,
            decision_mid=effective_decision_mid,
            execution_feed_seq=execution_feed_seq,
        )
        self._lifecycle.record_execution(result)
        self._execution_results_by_id[result.execution_id] = result
        ledger_event = self._ledger.record_execution(result, fee=fee, rebate=rebate)
        if ledger_event is not None:
            self._capture_execution_observation(ledger_event.event_id, result)
        self._sync_intent(intent_id)
        self._sync_ledger()
        return result

    def terminate(
        self,
        intent_id: str,
        state: IntentLifecycleState,
        *,
        occurred_at: datetime,
        disposition_reason: str,
    ) -> IntentLifecycleState:
        """Apply an explicit public terminal disposition to an open intent."""
        occurred_at = self._arrival_at(occurred_at)
        transition = self._lifecycle.terminate(
            intent_id,
            state,
            occurred_at=occurred_at,
            disposition_reason=disposition_reason,
        )
        intent = self._lifecycle.registered_intent(intent_id)
        if intent.role is OrderRole.MAKER:
            self._ledger.record_lifecycle(transition.event)
            self._passive_matcher.retire_intent(intent_id)
        self._sync_intent(intent_id)
        self._sync_ledger()
        return self._lifecycle.state_of(intent_id)

    def complete_eod(
        self,
        request: EodCloseRequest,
        *,
        executed_at: datetime,
        fees_by_product: Mapping[str, float] | None = None,
        rebates_by_product: Mapping[str, float] | None = None,
    ) -> EodCompletion:
        """Use the sole public EOD route and emit every derived effect."""
        if not isinstance(request, EodCloseRequest):
            raise FoundationContractError("request must be an EodCloseRequest")
        request = self._stress_eod_request(request)
        self._validate_context(request.context)
        self._emit_decision(request.context)
        executed_at = self._arrival_at(executed_at)
        fees_by_product = self._stressed_cost_map(fees_by_product)
        rebates_by_product = self._stressed_cost_map(rebates_by_product)
        prior_ledger_event_ids = {event.event_id for event in self._ledger.events()}
        completion = self._eod.complete(
            request,
            executed_at=executed_at,
            fees_by_product=fees_by_product,
            rebates_by_product=rebates_by_product,
        )
        for intent_id in completion.cancelled_intent_ids:
            self._passive_matcher.retire_intent(intent_id)
        for ledger_event in self._ledger.events():
            if ledger_event.event_id in prior_ledger_event_ids:
                continue
            intent_id = ledger_event.attributes.get("intent_id")
            result = self._lifecycle.attached_execution_result(str(intent_id)) if intent_id is not None else None
            if result is not None:
                self._execution_results_by_id[result.execution_id] = result
                self._capture_execution_observation(ledger_event.event_id, result)
        for intent_id in (*completion.cancelled_intent_ids, f"{request.eod_id}:quoted", f"{request.eod_id}:hedge"):
            if self._lifecycle.has_intent(intent_id):
                self._sync_intent(intent_id)
        self._sync_ledger()
        return completion

    def ledger_state(self) -> DualLegLedgerState:
        """Return the immutable ledger-derived dual-leg inventory state."""
        return self._ledger.reconcile()

    def state_of(self, intent_id: str) -> IntentLifecycleState:
        """Return an immutable lifecycle state without exposing the registry."""
        return self._lifecycle.state_of(intent_id)

    def execution_result(self, intent_id: str) -> ExecutionResult | None:
        """Return a result generated by this facade, if this intent has one."""
        return self._lifecycle.attached_execution_result(intent_id)

    def execution_results(self) -> tuple[ExecutionResult, ...]:
        """Return foundation-generated executions by immutable execution identity."""
        return tuple(self._execution_results_by_id[key] for key in sorted(self._execution_results_by_id))

    def ledger_events(self) -> tuple[LedgerEvent, ...]:
        """Return immutable, facade-generated ledger effects for research export."""
        return self._ledger.events()

    def maker_queue_evidence(self) -> tuple[MakerQueueEvidence, ...]:
        """Return immutable arrival evidence for every maker that reached the book."""
        return tuple(self._maker_queue_evidence_by_intent[key] for key in sorted(self._maker_queue_evidence_by_intent))

    def record_trigger(
        self,
        trigger_id: str,
        context: DecisionContext,
        *,
        occurred_at: datetime,
        attributes: Mapping[str, Any],
    ) -> None:
        self._validate_context(context)
        self._emit_decision(context)
        self._telemetry.emit_trigger_evaluation(trigger_id, context, occurred_at, attributes)

    def record_inventory(self, inventory_id: str, *, occurred_at: datetime) -> None:
        self._telemetry.emit_inventory(inventory_id, self.ledger_state(), occurred_at)

    def record_unattributed_outcome(self, outcome_id: str, reason: str) -> None:
        """Mark a conformance run as non-economic when no PnL attribution is supplied."""
        self._telemetry.emit_outcome_pnl(outcome_id, reason)

    def attribute_pnl(
        self,
        attribution_id: str,
        marks_by_product: Mapping[str, float],
        accounting_view: PnlAccountingView,
        cycle_view: PnlAccountingView,
        *,
        tolerance: float = 1e-9,
        eod_completion: EodCompletion | None = None,
    ) -> PnlAttributionResult:
        """Attribute all foundation-owned ledger effects and emit the canonical outcome."""
        if not self._ledger.events():
            self._telemetry.declare_empty_table("fills")
        result = PnlAttributionService().attribute(
            attribution_id,
            self._ledger,
            tuple(self._pnl_observations.values()),
            marks_by_product,
            self._instrument_specs,
            telemetry_run_dir=self._telemetry.run_dir,
            accounting_view=accounting_view,
            cycle_view=cycle_view,
            tolerance=tolerance,
            eod_completion=eod_completion,
        )
        self._telemetry.emit_pnl_attribution(result)
        return result

    def capture_provenance(self, trial: TrialDeclaration, artifacts: Mapping[str, Any]) -> RunProvenance:
        """Capture the one immutable trial provenance set for this API run."""
        captured = dict(artifacts)
        if self._stress_scenario is not None:
            scenario = dict(self._stress_scenario.as_provenance())
            existing = captured.get("stress_scenario")
            if existing is not None and existing != scenario:
                raise FoundationContractError("provenance stress_scenario must match the API stress scenario")
            captured["stress_scenario"] = scenario
        return self._telemetry.capture_provenance(trial, captured)

    def finalize(self) -> TelemetryRunResult:
        """Seal canonical telemetry, declaring genuinely empty mandatory tables."""
        self._sync_ledger()
        for table in self._telemetry.schema.tables:
            self._telemetry.declare_empty_table(table)
        return self._telemetry.finalize()

    def _validate_context(self, context: DecisionContext) -> None:
        if not isinstance(context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        if context.run_id != self._run_id or context.hedge_pair != self._hedge_mapping.hedge_pair:
            raise FoundationContractError("context must match the API run and hedge pair")
        for ref in (context.quoted_book, context.hedge_book):
            if self._book_refs.get(ref.snapshot_id) != ref:
                raise FoundationContractError("decision context book snapshots must be recorded before policy submission")
        for ref in context.consumed_signals:
            if self._signal_refs.get(ref.snapshot_id) != ref:
                raise FoundationContractError("decision context signal snapshots must be recorded before policy submission")
        for signal in context.consumed_signal_values:
            expected_payload = self._telemetry.snapshot_payload("signal", signal.ref.snapshot_hash)
            if signal.payload != expected_payload:
                raise FoundationContractError("decision context signal value does not match its retained snapshot")

    def _validate_batch(self, batch: MakerHedgeIntentBatch, context: DecisionContext) -> None:
        for intent in (batch.maker_intent, batch.hedge_intent):
            if intent is None:
                continue
            if (
                intent.run_id != context.run_id
                or intent.decision_id != context.decision_id
                or intent.hedge_pair != context.hedge_pair
            ):
                raise FoundationContractError("batch intent must match the submitted decision context")
            self._execution.resolve_execution_model(intent.execution_model_ref)
            self._validate_pricing_reference(intent, context)
        if batch.maker_intent is not None:
            envelope = self._lifecycle.capacity_envelope(batch.maker_capacity_envelope_id or "")
            if envelope.hedge_pair != batch.maker_intent.hedge_pair or envelope.product != batch.maker_intent.product:
                raise FoundationContractError("maker capacity envelope must match its declared maker intent")

    def _validate_pricing_reference(self, intent: OrderIntent, context: DecisionContext) -> None:
        """Require policy-owned prior-batch evidence for interval-fill reactions."""
        if not self._require_exchange_batch_pricing or context.exchange_batch is None:
            return
        reference = intent.pricing_reference
        if reference is None:
            if context.observed_fill_ids:
                raise FoundationContractError(
                    "an order submitted after interval fills requires a policy-owned pricing_reference"
                )
            return
        current = context.quoted_book if intent.product == context.quoted_product else context.hedge_book
        if reference.trigger_fill_id is None:
            if (
                reference.basis != "post_batch_snapshot_v1"
                or reference.pricing_batch != context.exchange_batch
                or reference.pricing_snapshot_id != current.snapshot_id
            ):
                raise FoundationContractError("post-batch order pricing must cite its current product snapshot")
            return
        previous = context.previous_quoted_book if intent.product == context.quoted_product else context.previous_hedge_book
        if (
            context.interval_id is None
            or reference.trigger_fill_id not in context.observed_fill_ids
            or previous is None
            or previous.exchange_batch is None
            or self._book_refs.get(previous.snapshot_id) != previous
            or reference.basis != "previous_batch_interval_fill_v1"
            or reference.pricing_batch != previous.exchange_batch
            or reference.pricing_snapshot_id != previous.snapshot_id
        ):
            raise FoundationContractError(
                "fill-triggered order pricing must cite the prior aligned product snapshot and an observed interval fill"
            )

    def _emit_decision(self, context: DecisionContext) -> None:
        previous = self._contexts.get(context.decision_id)
        if previous is not None and previous != context:
            raise FoundationContractError("decision_id is already bound to a different immutable context")
        if previous is None:
            self._telemetry.emit_decision(context)
            self._contexts[context.decision_id] = context
            self._emitted_decision_ids.add(context.decision_id)
            if self._stress_scenario is not None:
                self._telemetry.emit_trigger_evaluation(
                    f"{context.decision_id}:stress:{self._stress_scenario.scenario_id}",
                    context,
                    context.dec_ts,
                    {"record_type": "stress_scenario", "stress": self._stress_scenario.as_provenance()},
                )

    def _sync_intent(self, intent_id: str) -> None:
        intent = self._lifecycle.registered_intent(intent_id)
        context = self._lifecycle.decision_context(intent_id)
        if intent.intent_id not in self._emitted_order_ids:
            self._telemetry.emit_order(intent, context)
            self._emitted_order_ids.add(intent.intent_id)
        for event in self._lifecycle.intent_history(intent_id):
            if event.event_id not in self._emitted_lifecycle_ids:
                self._telemetry.emit_lifecycle(event)
                self._emitted_lifecycle_ids.add(event.event_id)
        for event in self._lifecycle.reservation_history(intent_id):
            if event.reservation_id not in self._emitted_reservation_ids:
                envelope = self._lifecycle.capacity_envelope(event.envelope_id)
                self._telemetry.emit_reservation(event, envelope.max_reserved_qty)
                self._emitted_reservation_ids.add(event.reservation_id)
        result = self._lifecycle.attached_execution_result(intent_id)
        if result is not None and result.execution_id not in self._emitted_execution_ids:
            self._telemetry.emit_execution(result)
            self._emitted_execution_ids.add(result.execution_id)

    def _sync_ledger(self) -> None:
        for event in self._ledger.events():
            if event.event_id not in self._emitted_ledger_ids:
                self._telemetry.emit_ledger_effect(event)
                self._emitted_ledger_ids.add(event.event_id)

    def _queue_ahead_at_arrival(self, intent: OrderIntent, snapshot: BookSnapshotRef) -> int:
        """Derive the conservative displayed queue ahead at a maker's price."""
        payload = self._telemetry.snapshot_payload("book", snapshot.snapshot_hash)
        side_key = "bids" if intent.side.value == "buy" else "asks"
        levels = payload.get(side_key)
        if not isinstance(levels, (list, tuple)):
            raise FoundationContractError("arrival book payload must expose bid and ask depth levels")
        tick = float(self._instrument_specs[intent.product].tick)
        queue_ahead = 0
        for level in levels:
            if not isinstance(level, Mapping):
                raise FoundationContractError("arrival book depth levels must be mappings")
            try:
                price = float(level["price"])
                quantity = int(level["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FoundationContractError("arrival book depth levels require numeric price and quantity") from exc
            if quantity < 0 or not math.isfinite(price) or price <= 0:
                raise FoundationContractError("arrival book depth levels must be finite with non-negative quantity")
            if math.isclose(price, intent.limit_price, abs_tol=tick / 10.0):
                queue_ahead += quantity
        return queue_ahead

    def _require_snapshot_proxy_depth(self) -> None:
        """Fail closed when an active proxy maker quote is outside retained depth."""
        active_states = {IntentLifecycleState.ARRIVED, IntentLifecycleState.PARTIALLY_FILLED}
        for intent_id, queue_evidence in self._maker_queue_evidence_by_intent.items():
            if self._lifecycle.state_of(intent_id) not in active_states:
                continue
            intent = self._lifecycle.registered_intent(intent_id)
            payload = self._telemetry.snapshot_payload("book", queue_evidence.book_snapshot.snapshot_hash)
            side_key = "bids" if intent.side is OrderSide.BUY else "asks"
            levels = payload.get(side_key, ())
            tick = float(self._instrument_specs[intent.product].tick)
            represented = any(
                isinstance(level, Mapping)
                and math.isclose(float(level.get("price", math.nan)), intent.limit_price, abs_tol=tick / 10.0)
                and int(level.get("quantity", 0)) > 0
                for level in levels
            )
            if not represented:
                raise FoundationContractError("snapshot proxy maker quote is outside retained arrival depth")

    def _validate_snapshot_identity(self, ref: BookSnapshotRef | SignalSnapshotRef) -> None:
        if not isinstance(ref, (BookSnapshotRef, SignalSnapshotRef)):
            raise FoundationContractError("snapshot reference must be a BookSnapshotRef or SignalSnapshotRef")
        if ref.product not in (
            self._hedge_mapping.hedge_pair.quoted_product,
            self._hedge_mapping.hedge_pair.hedge_product,
        ):
            raise FoundationContractError("snapshot product must belong to the API hedge pair")

    @staticmethod
    def _validate_costs(fee: float, rebate: float) -> None:
        try:
            numeric_fee, numeric_rebate = float(fee), float(rebate)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("fee and rebate must be finite non-negative numbers") from exc
        if not all(math.isfinite(value) and value >= 0.0 for value in (numeric_fee, numeric_rebate)):
            raise FoundationContractError("fee and rebate must be finite non-negative numbers")

    def _stress_batch(self, batch: MakerHedgeIntentBatch) -> MakerHedgeIntentBatch:
        if self._stress_scenario is None:
            return batch

        def stressed_intent(intent: OrderIntent | None) -> OrderIntent | None:
            if intent is None or intent.execution_model_ref is None:
                return intent
            stressed_ref = self._stress_model_references.get(intent.execution_model_ref)
            return intent if stressed_ref is None else replace(intent, execution_model_ref=stressed_ref)

        return MakerHedgeIntentBatch(
            stressed_intent(batch.maker_intent),
            stressed_intent(batch.hedge_intent),
            batch.maker_capacity_envelope_id,
        )

    def _submission_at(self, occurred_at: datetime) -> datetime:
        if self._stress_scenario is None or self._action_timing_managed_externally:
            return occurred_at
        return self._stress_scenario.submission_at(occurred_at)

    def _arrival_at(self, occurred_at: datetime) -> datetime:
        if self._stress_scenario is None or self._action_timing_managed_externally:
            return occurred_at
        return self._stress_scenario.arrival_at(occurred_at)

    def _stressed_costs(self, fee: float, rebate: float) -> tuple[float, float]:
        if self._stress_scenario is None:
            return fee, rebate
        return self._stress_scenario.adjusted_fee(fee), self._stress_scenario.adjusted_fee(rebate)

    def _stressed_cost_map(self, values: Mapping[str, float] | None) -> Mapping[str, float] | None:
        if values is None or self._stress_scenario is None:
            return values
        if not isinstance(values, Mapping):
            raise FoundationContractError("EOD cost map must be a mapping or None")
        return {product: self._stress_scenario.adjusted_fee(value) for product, value in values.items()}

    def _stress_eod_request(self, request: EodCloseRequest) -> EodCloseRequest:
        if self._stress_scenario is None or request.execution_model_ref is None:
            return request
        stressed_ref = self._stress_model_references.get(request.execution_model_ref)
        return request if stressed_ref is None else replace(request, execution_model_ref=stressed_ref)

    def _capture_execution_observation(self, ledger_event_id: str, result: ExecutionResult) -> None:
        if result.vwap is None:
            raise FoundationContractError("a ledger-producing execution requires a vwap for PnL attribution")
        reference = result.decision_mid
        if reference is None:
            context = self._lifecycle.decision_context(result.intent_id)
            reference = self._decision_mid(context, result.product)
        self._pnl_observations[ledger_event_id] = PnlPriceObservation(ledger_event_id, result.vwap, reference)

    def _capture_passive_observation(self, ledger_event_id: str, fill_price: float, context: DecisionContext) -> None:
        try:
            reference = self._decision_mid(context, context.quoted_product)
        except FoundationContractError:
            # The legacy compatibility route must retain its explicit
            # incomplete-liquidity behavior. A later PnL claim fails closed if
            # its required price observation was not captured.
            return
        self._pnl_observations[ledger_event_id] = PnlPriceObservation(ledger_event_id, fill_price, reference)

    def _decision_mid(self, context: DecisionContext, product: str) -> float:
        ref = context.quoted_book if product == context.quoted_product else context.hedge_book
        payload = self._telemetry.snapshot_payload("book", ref.snapshot_hash)
        try:
            bid = float(payload["bids"][0]["price"])
            ask = float(payload["asks"][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FoundationContractError("decision book lacks a usable top-of-book midpoint") from exc
        midpoint = (bid + ask) / 2.0
        if not math.isfinite(midpoint) or midpoint <= 0:
            raise FoundationContractError("decision book midpoint must be finite and positive")
        return midpoint

    @staticmethod
    def _payload_levels(payload: Mapping[str, Any], side: str) -> tuple[DepthLevel, ...]:
        try:
            raw_levels = payload[side]
        except (KeyError, TypeError) as exc:
            raise FoundationContractError(f"retained book payload requires {side} levels") from exc
        if not isinstance(raw_levels, (tuple, list)):
            raise FoundationContractError(f"retained book payload {side} must be a list of level mappings")
        levels: list[DepthLevel] = []
        for raw_level in raw_levels:
            if not isinstance(raw_level, Mapping) or set(raw_level) != {"price", "quantity"}:
                raise FoundationContractError(f"retained book payload {side} levels must contain exactly price and quantity")
            levels.append(DepthLevel(raw_level["price"], raw_level["quantity"]))
        return tuple(levels)


__all__ = [
    "DualBookFoundation",
    "FOUNDATION_API_VERSION",
    "FoundationSubmission",
    "MakerHedgePolicy",
    "PolicyProposal",
    "PolicyTrigger",
    "ProductionMakerHedgePolicy",
]
