"""Supported receive-time dual-book replay for operational S0 remediation.

This adapter is intentionally separate from the compatibility ``backtest``
path.  It wires strict, validated market data through causal ingress and the
public ``DualBookFoundation`` facade; it never imports legacy ``Market`` or
``Strategy``.  Until research telemetry and PnL attribution are connected, a
result is explicitly non-economic and cannot satisfy the final S0 gate.
"""

from __future__ import annotations

import heapq
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from common.foundation_api import DualBookFoundation, PolicyProposal, ProductionMakerHedgePolicy
from common.foundation_contracts import (
    ApprovedEvidenceAuthority,
    BookSnapshotRef,
    CapacityEnvelope,
    EodCloseRequest,
    EodCompletion,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
    MakerHedgeIntentBatch,
    OrderRole,
    OrderSide,
    PassiveTrade,
    PnlAccountingView,
    PnlAttributionResult,
    PnlViewEvidence,
    TelemetryRunResult,
    TrialDeclaration,
    ValuationMarkEvidence,
    SnapshotInterval,
    SnapshotIntervalPriceBucket,
    SnapshotIntervalQueueProxyEvidence,
)
from common.foundation_loader import RawSnapshotAdapterProvenance, ValidatedMarketData, adapted_replay_events_hash
from common.ingress import CausalIngress
from common.research_telemetry import (
    RESEARCH_TELEMETRY_SCHEMA_VERSION,
    S0_SEMANTIC_COMPLIANCE_VERSION,
    ResearchTelemetryEmitter,
    ResearchTelemetryResult,
)
from common.stress import StressScenario, apply_ingress_stress
from common.telemetry import TelemetryEmitter


@dataclass(frozen=True)
class DeploymentEvidenceAuthorityRegistry:
    """Deployment-owned trust roots for economic source-artifact verification.

    This object is installed at the replay-hosting boundary and is deliberately
    separate from ``ProductionReplayConfig``. A policy or experiment caller can
    declare evidence, but cannot extend the set of authorities that qualifies
    it as economic evidence. The registry retains secret-bearing authority
    objects in memory only; provenance exposes selectors, never keys.
    """

    authorities: tuple[ApprovedEvidenceAuthority, ...] = field(default=(), repr=False)
    _by_identity: Mapping[tuple[str, str], ApprovedEvidenceAuthority] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.authorities, tuple) or any(
            not isinstance(authority, ApprovedEvidenceAuthority) for authority in self.authorities
        ):
            raise FoundationContractError("deployment evidence authority registry must contain ApprovedEvidenceAuthority values")
        by_identity = {(authority.authority_id, authority.key_id): authority for authority in self.authorities}
        if len(by_identity) != len(self.authorities):
            raise FoundationContractError("deployment evidence authority registry must have unique authority/key identities")
        object.__setattr__(self, "_by_identity", MappingProxyType(by_identity))

    def resolve(self, authority_id: str, key_id: str) -> ApprovedEvidenceAuthority | None:
        """Return an installed authority only when its non-secret selector is known."""
        return self._by_identity.get((authority_id, key_id))

    def provenance_selectors(self) -> tuple[Mapping[str, str], ...]:
        """Return stable non-secret selectors for the registry recorded with a run."""
        return tuple(
            authority.as_provenance()
            for _, authority in sorted(self._by_identity.items(), key=lambda item: item[0])
        )


@dataclass(frozen=True)
class EconomicReplayInputs:
    """Independent accounting declarations required before a replay may attribute PnL."""

    marks_by_product: Mapping[str, float]
    accounting_view: PnlAccountingView
    cycle_view: PnlAccountingView
    tolerance: float = 1e-9
    accounting_evidence: PnlViewEvidence | None = None
    cycle_evidence: PnlViewEvidence | None = None
    mark_evidence_by_product: Mapping[str, ValuationMarkEvidence] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.marks_by_product, Mapping):
            raise FoundationContractError("marks_by_product must be a mapping")
        if not isinstance(self.accounting_view, PnlAccountingView) or not isinstance(self.cycle_view, PnlAccountingView):
            raise FoundationContractError("economic replay requires independent PnlAccountingView values")
        if self.accounting_view.view_id == self.cycle_view.view_id:
            raise FoundationContractError("economic accounting and cycle views must have distinct IDs")
        if not math.isfinite(float(self.tolerance)) or self.tolerance < 0:
            raise FoundationContractError("economic tolerance must be finite and non-negative")
        marks: dict[str, float] = {}
        for product, mark in self.marks_by_product.items():
            if not isinstance(product, str) or not product.strip():
                raise FoundationContractError("economic mark products must be non-empty strings")
            try:
                numeric_mark = float(mark)
            except (TypeError, ValueError) as exc:
                raise FoundationContractError("economic marks must be positive finite values") from exc
            if not math.isfinite(numeric_mark) or numeric_mark <= 0:
                raise FoundationContractError("economic marks must be positive finite values")
            marks[product] = numeric_mark
        if not marks:
            raise FoundationContractError("economic replay requires a valuation mark for each product")
        object.__setattr__(self, "marks_by_product", MappingProxyType(marks))
        evidence_values = (self.accounting_evidence, self.cycle_evidence)
        if any(value is not None and not isinstance(value, PnlViewEvidence) for value in evidence_values):
            raise FoundationContractError("economic view evidence must contain PnlViewEvidence values")
        if not isinstance(self.mark_evidence_by_product, Mapping):
            raise FoundationContractError("mark_evidence_by_product must be a mapping")
        mark_evidence = dict(self.mark_evidence_by_product)
        if any(not isinstance(product, str) or not isinstance(value, ValuationMarkEvidence) for product, value in mark_evidence.items()):
            raise FoundationContractError("mark_evidence_by_product must map products to ValuationMarkEvidence values")
        complete = self.accounting_evidence is not None and self.cycle_evidence is not None and bool(mark_evidence)
        partial = any(value is not None for value in evidence_values) or bool(mark_evidence)
        if partial and not complete:
            raise FoundationContractError("economic evidence must declare both PnL views and every valuation mark")
        if complete:
            assert self.accounting_evidence is not None and self.cycle_evidence is not None
            self._validate_view_evidence(self.accounting_view, self.accounting_evidence, "accounting")
            self._validate_view_evidence(self.cycle_view, self.cycle_evidence, "cycle")
            if self.accounting_evidence.evidence_id == self.cycle_evidence.evidence_id:
                raise FoundationContractError("independent PnL view evidence requires distinct evidence IDs")
            if self.accounting_evidence.source_artifact_id == self.cycle_evidence.source_artifact_id:
                raise FoundationContractError("independent PnL view evidence requires distinct source artifacts")
            if set(mark_evidence) != set(marks):
                raise FoundationContractError("valuation mark evidence must cover exactly the declared economic marks")
            for product, evidence in mark_evidence.items():
                if evidence.product != product or not math.isclose(evidence.mark, marks[product], rel_tol=0.0, abs_tol=1e-12):
                    raise FoundationContractError("valuation mark evidence must match its declared product and mark")
        object.__setattr__(self, "mark_evidence_by_product", MappingProxyType(mark_evidence))

    @staticmethod
    def _validate_view_evidence(view: PnlAccountingView, evidence: PnlViewEvidence, label: str) -> None:
        if evidence.view_id != view.view_id or not math.isclose(
            evidence.total_pnl, view.total_pnl, rel_tol=0.0, abs_tol=1e-12
        ):
            raise FoundationContractError(f"{label} PnL evidence must match its declared accounting view")

    @property
    def evidence_eligible(self) -> bool:
        """Whether this input can support an economics-eligibility verdict."""
        return self.accounting_evidence is not None and self.cycle_evidence is not None and bool(self.mark_evidence_by_product)

    def provenance_artifacts(self) -> Mapping[str, Any]:
        """Return source-bound PnL and mark artifacts for immutable run provenance."""
        if not self.evidence_eligible:
            raise FoundationContractError("economic inputs without complete evidence cannot produce provenance artifacts")
        assert self.accounting_evidence is not None and self.cycle_evidence is not None
        artifacts: dict[str, Any] = {
            "pnl_accounting_view_evidence": self.accounting_evidence.as_provenance(),
            "pnl_accounting_view_source": self.accounting_evidence.source_artifact,
            "pnl_cycle_view_evidence": self.cycle_evidence.as_provenance(),
            "pnl_cycle_view_source": self.cycle_evidence.source_artifact,
            "valuation_mark_evidence": tuple(
                self.mark_evidence_by_product[product].as_provenance()
                for product in sorted(self.mark_evidence_by_product)
            ),
        }
        for index, product in enumerate(sorted(self.mark_evidence_by_product)):
            artifacts[f"valuation_mark_source_{index}"] = self.mark_evidence_by_product[product].source_artifact
        return MappingProxyType(artifacts)

    def source_artifact_hashes(self) -> Mapping[str, str]:
        """Return the expected source-byte digests keyed by provenance artifact."""
        if not self.evidence_eligible:
            return MappingProxyType({})
        assert self.accounting_evidence is not None and self.cycle_evidence is not None
        hashes = {
            "pnl_accounting_view_source": self.accounting_evidence.source_artifact_hash,
            "pnl_cycle_view_source": self.cycle_evidence.source_artifact_hash,
        }
        for index, product in enumerate(sorted(self.mark_evidence_by_product)):
            hashes[f"valuation_mark_source_{index}"] = self.mark_evidence_by_product[product].source_artifact_hash
        return MappingProxyType(hashes)

    def verified_evidence_eligible(
        self,
        *,
        run_id: str,
        session_date: date,
        eod_at: datetime,
        authority_registry: DeploymentEvidenceAuthorityRegistry,
    ) -> bool:
        """Verify authenticated, content-to-value-bound evidence for one declared replay."""
        if not self.evidence_eligible:
            return False
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(session_date, date):
            raise FoundationContractError("economic evidence verification requires a declared run and session date")
        if not isinstance(eod_at, datetime) or eod_at.tzinfo is None or eod_at.utcoffset() is None:
            raise FoundationContractError("economic evidence verification requires a timezone-aware EOD timestamp")
        if not isinstance(authority_registry, DeploymentEvidenceAuthorityRegistry):
            raise FoundationContractError("economic evidence verification requires a deployment authority registry")
        assert self.accounting_evidence is not None and self.cycle_evidence is not None
        accounting_authority = self._verified_source_authority(
            self.accounting_evidence,
            "pnl_view",
            {
                "run_id": run_id,
                "session_date": session_date.isoformat(),
                "evidence_id": self.accounting_evidence.evidence_id,
                "view_id": self.accounting_evidence.view_id,
                "total_pnl": self.accounting_evidence.total_pnl,
                "methodology": self.accounting_evidence.methodology,
                "methodology_version": self.accounting_evidence.methodology_version,
                "source_artifact_id": self.accounting_evidence.source_artifact_id,
                "calculated_at": self.accounting_evidence.calculated_at.isoformat(),
            },
            authority_registry,
        )
        cycle_authority = self._verified_source_authority(
            self.cycle_evidence,
            "pnl_view",
            {
                "run_id": run_id,
                "session_date": session_date.isoformat(),
                "evidence_id": self.cycle_evidence.evidence_id,
                "view_id": self.cycle_evidence.view_id,
                "total_pnl": self.cycle_evidence.total_pnl,
                "methodology": self.cycle_evidence.methodology,
                "methodology_version": self.cycle_evidence.methodology_version,
                "source_artifact_id": self.cycle_evidence.source_artifact_id,
                "calculated_at": self.cycle_evidence.calculated_at.isoformat(),
            },
            authority_registry,
        )
        if accounting_authority is None or cycle_authority is None:
            return False
        if self.accounting_evidence.calculated_at < eod_at or self.cycle_evidence.calculated_at < eod_at:
            return False
        if accounting_authority == cycle_authority:
            return False
        mark_authorities: set[tuple[str, str]] = set()
        for evidence in self.mark_evidence_by_product.values():
            authority = self._verified_source_authority(
                evidence,
                "valuation_mark",
                {
                    "run_id": run_id,
                    "session_date": session_date.isoformat(),
                    "evidence_id": evidence.evidence_id,
                    "product": evidence.product,
                    "mark": evidence.mark,
                    "methodology": evidence.methodology,
                    "methodology_version": evidence.methodology_version,
                    "source_artifact_id": evidence.source_artifact_id,
                    "observed_at": evidence.observed_at.isoformat(),
                },
                authority_registry,
            )
            if authority is None or evidence.observed_at > eod_at or authority in {accounting_authority, cycle_authority}:
                return False
            mark_authorities.add(authority)
        return bool(mark_authorities)

    @staticmethod
    def _verified_source_authority(
        evidence: PnlViewEvidence | ValuationMarkEvidence,
        artifact_type: str,
        expected: Mapping[str, Any],
        authority_registry: DeploymentEvidenceAuthorityRegistry,
    ) -> tuple[str, str] | None:
        """Verify canonical signed source content and its exact declared economic fields."""
        try:
            payload = json.loads(evidence.source_artifact.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("signature"), str):
            return None
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        if evidence.source_artifact != _canonical_economic_bytes(payload):
            return None
        if unsigned.get("schema_version") != "s0-economic-evidence-v1" or unsigned.get("artifact_type") != artifact_type:
            return None
        authority_id = unsigned.get("authority_id")
        key_id = unsigned.get("key_id")
        if not isinstance(authority_id, str) or not isinstance(key_id, str):
            return None
        authority_key = (authority_id, key_id)
        authority = authority_registry.resolve(*authority_key)
        if authority is None or not authority.verifies(_canonical_economic_bytes(unsigned), payload["signature"]):
            return None
        for field_name, expected_value in expected.items():
            value = unsigned.get(field_name)
            if isinstance(expected_value, float):
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isclose(
                    float(value), expected_value, rel_tol=0.0, abs_tol=1e-12
                ):
                    return None
            elif value != expected_value:
                return None
        return authority_key


@dataclass(frozen=True)
class ProductionReplayConfig:
    """Immutable configuration for one complete, operational replay run."""

    run_id: str
    hedge_mapping: HedgeMappingSpec
    instrument_specs: tuple[InstrumentSpec, ...]
    execution_models: tuple[ExecutionModelConfig, ...]
    default_execution_model: ExecutionModelRef
    capacity_envelopes: tuple[CapacityEnvelope, ...]
    artifact_root: str | Path
    session_date: date
    trial: TrialDeclaration
    provenance_artifacts: Mapping[str, Any]
    stress_scenario: StressScenario | None = None
    passive_fee_rebate_per_contract: float = 0.0
    economic_inputs: EconomicReplayInputs | None = None
    research_export: bool = False
    registered_signal_ids: frozenset[str] = frozenset()
    max_execution_book_age_ms_by_product: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise FoundationContractError("run_id must be a non-empty string")
        if not isinstance(self.hedge_mapping, HedgeMappingSpec):
            raise FoundationContractError("hedge_mapping must be a HedgeMappingSpec")
        if not self.instrument_specs or any(not isinstance(spec, InstrumentSpec) for spec in self.instrument_specs):
            raise FoundationContractError("instrument_specs must be a non-empty tuple of InstrumentSpec values")
        if not self.execution_models or any(not isinstance(model, ExecutionModelConfig) for model in self.execution_models):
            raise FoundationContractError("execution_models must be a non-empty tuple of ExecutionModelConfig values")
        if not isinstance(self.default_execution_model, ExecutionModelRef):
            raise FoundationContractError("default_execution_model must be an ExecutionModelRef")
        if any(not isinstance(envelope, CapacityEnvelope) for envelope in self.capacity_envelopes):
            raise FoundationContractError("capacity_envelopes must contain CapacityEnvelope values")
        if not isinstance(self.session_date, date):
            raise FoundationContractError("session_date must be a date")
        if not isinstance(self.trial, TrialDeclaration) or self.trial.hedge_pair != self.hedge_mapping.hedge_pair:
            raise FoundationContractError("trial must belong to the configured hedge pair")
        if not isinstance(self.provenance_artifacts, Mapping):
            raise FoundationContractError("provenance_artifacts must be a mapping")
        if self.stress_scenario is not None and not isinstance(self.stress_scenario, StressScenario):
            raise FoundationContractError("stress_scenario must be a StressScenario or None")
        if self.economic_inputs is not None and not isinstance(self.economic_inputs, EconomicReplayInputs):
            raise FoundationContractError("economic_inputs must be EconomicReplayInputs or None")
        if not isinstance(self.research_export, bool):
            raise FoundationContractError("research_export must be a bool")
        if not isinstance(self.registered_signal_ids, frozenset) or any(
            not isinstance(value, str) or not value.strip() for value in self.registered_signal_ids
        ):
            raise FoundationContractError("registered_signal_ids must be a frozenset of non-empty strings")
        if not isinstance(self.max_execution_book_age_ms_by_product, Mapping):
            raise FoundationContractError("max_execution_book_age_ms_by_product must be a mapping")
        max_ages: dict[str, float] = {}
        for product, age in self.max_execution_book_age_ms_by_product.items():
            if not isinstance(product, str) or not product.strip():
                raise FoundationContractError("max_execution_book_age_ms_by_product keys must be non-empty strings")
            try:
                numeric_age = float(age)
            except (TypeError, ValueError) as exc:
                raise FoundationContractError("max_execution_book_age_ms values must be finite and non-negative") from exc
            if not math.isfinite(numeric_age) or numeric_age < 0:
                raise FoundationContractError("max_execution_book_age_ms values must be finite and non-negative")
            max_ages[product] = numeric_age
        object.__setattr__(self, "max_execution_book_age_ms_by_product", MappingProxyType(max_ages))
        if self.research_export and self.economic_inputs is None:
            raise FoundationContractError("research_export requires declared economic_inputs")
        try:
            fee_rebate = float(self.passive_fee_rebate_per_contract)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("passive_fee_rebate_per_contract must be finite") from exc
        if not math.isfinite(fee_rebate):
            raise FoundationContractError("passive_fee_rebate_per_contract must be finite")
        products = {spec.product for spec in self.instrument_specs}
        pair = self.hedge_mapping.hedge_pair
        if {pair.quoted_product, pair.hedge_product} - products:
            raise FoundationContractError("instrument_specs must include both hedge-pair products")


@dataclass(frozen=True)
class OperationalReplayResult:
    """Sealed operational evidence, explicitly distinct from economic evidence."""

    telemetry: TelemetryRunResult
    eod_completion: EodCompletion
    decision_ids: tuple[str, ...]
    execution_ids: tuple[str, ...]
    passive_fill_ids: tuple[str, ...]
    pnl_attribution: PnlAttributionResult | None = None
    research_telemetry: ResearchTelemetryResult | None = None
    execution_freshness_eligible: bool = False
    semantic_compliance_eligible: bool = False
    economics_eligible: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.telemetry, TelemetryRunResult):
            raise FoundationContractError("telemetry must be a TelemetryRunResult")
        if not isinstance(self.eod_completion, EodCompletion):
            raise FoundationContractError("eod_completion must be an EodCompletion")
        if not isinstance(self.execution_freshness_eligible, bool):
            raise FoundationContractError("execution_freshness_eligible must be boolean")
        if not isinstance(self.semantic_compliance_eligible, bool):
            raise FoundationContractError("semantic_compliance_eligible must be boolean")
        if self.economics_eligible:
            if (
                self.pnl_attribution is None
                or self.research_telemetry is None
                or not self.execution_freshness_eligible
                or not self.semantic_compliance_eligible
            ):
                raise FoundationContractError(
                    "economic eligibility requires PnL, research telemetry, execution freshness, and semantic compliance"
                )


@dataclass(frozen=True)
class _ScheduledAction:
    due_at: datetime
    priority: int
    sequence: int
    kind: str
    context: Any
    batch: MakerHedgeIntentBatch | None = None
    intent_id: str | None = None


class ProductionReplayAdapter:
    """Run validated dual-book data through the only supported operational path.

    The replay host, rather than the per-run configuration, provides the
    deployment authority registry. Omitting it leaves economic evidence
    fail-closed while retaining normal non-economic replay behavior.
    """

    def __init__(
        self,
        config: ProductionReplayConfig,
        *,
        authority_registry: DeploymentEvidenceAuthorityRegistry | None = None,
    ) -> None:
        if not isinstance(config, ProductionReplayConfig):
            raise FoundationContractError("config must be a ProductionReplayConfig")
        if authority_registry is not None and not isinstance(authority_registry, DeploymentEvidenceAuthorityRegistry):
            raise FoundationContractError("authority_registry must be a DeploymentEvidenceAuthorityRegistry or None")
        self.config = config
        self._authority_registry = authority_registry or DeploymentEvidenceAuthorityRegistry()

    def run(
        self,
        market_data: ValidatedMarketData,
        policy: ProductionMakerHedgePolicy,
        *,
        signal_events: Iterable[IngressEvent] = (),
    ) -> OperationalReplayResult:
        """Replay one declared trading day, close it through calendar EOD, and seal telemetry."""
        if not isinstance(market_data, ValidatedMarketData):
            raise FoundationContractError("market_data must be ValidatedMarketData from the strict loader")
        book_events = market_data.to_ingress_events(event_id_prefix=f"{self.config.run_id}:book")
        self._require_snapshot_adapter_provenance(market_data, book_events=book_events)
        propose = getattr(policy, "propose", None)
        select_signal_ids = getattr(policy, "select_signal_ids", None)
        if not callable(propose) or not callable(select_signal_ids):
            raise FoundationContractError(
                "production policy must provide select_signal_ids(available_signals) and propose(context)"
            )
        signals = tuple(signal_events)
        if any(not isinstance(event, IngressEvent) or event.kind is not IngressKind.SIGNAL for event in signals):
            raise FoundationContractError("signal_events must contain only SIGNAL IngressEvent values")

        events = (*book_events, *signals)
        if self.config.stress_scenario is not None:
            events = apply_ingress_stress(
                events,
                self.config.stress_scenario,
                instrument_specs=self.config.instrument_specs,
            )
        events = self._filter_calendar_book_events(events)
        ingress = CausalIngress(
            self.config.run_id,
            events,
            required_book_products=(
                self.config.hedge_mapping.hedge_pair.quoted_product,
                self.config.hedge_mapping.hedge_pair.hedge_product,
            ),
        )
        telemetry = TelemetryEmitter(self.config.artifact_root, self.config.run_id, self.config.hedge_mapping.hedge_pair)
        api = DualBookFoundation(
            run_id=self.config.run_id,
            hedge_mapping=self.config.hedge_mapping,
            instrument_specs=self.config.instrument_specs,
            execution_models=self.config.execution_models,
            default_execution_model=self.config.default_execution_model,
            capacity_envelopes=self.config.capacity_envelopes,
            telemetry=telemetry,
            stress_scenario=self.config.stress_scenario,
            require_verified_passive_fills=True,
            action_timing_managed_externally=True,
            require_exchange_batch_pricing=True,
        )
        eod_at = self._eod_at()
        pending: list[tuple[datetime, int, int, _ScheduledAction]] = []
        action_sequence = 0
        decision_ids: list[str] = []
        execution_ids: list[str] = []
        passive_fill_ids: list[str] = []
        decision_number = 0
        research_events: list[tuple[IngressEvent, BookSnapshotRef, Mapping[str, Any]]] = []
        research_signals: list[tuple[IngressEvent, Any, Mapping[str, Any]]] = []
        research_decisions: list[tuple[Any, PolicyProposal | None, Any]] = []
        research_passive_evidence: list[Any] = []
        self._opening_decision_skipped = False

        def schedule(due_at: datetime, priority: int, kind: str, context: Any, batch: MakerHedgeIntentBatch | None = None, intent_id: str | None = None) -> None:
            nonlocal action_sequence
            action_sequence += 1
            action = _ScheduledAction(due_at, priority, action_sequence, kind, context, batch, intent_id)
            heapq.heappush(pending, (action.due_at, action.priority, action.sequence, action))

        def process_due(through: datetime, *, inclusive: bool) -> None:
            while pending and (pending[0][0] <= through if inclusive else pending[0][0] < through):
                action = heapq.heappop(pending)[3]
                if not self._pair_session_open(action.due_at):
                    raise FoundationContractError("scheduled policy action occurs during a declared session break")
                if action.kind == "submit":
                    assert action.batch is not None
                    api.submit(action.batch, action.context, occurred_at=action.due_at)
                    decision_ids.append(action.context.decision_id)
                    continue
                assert action.intent_id is not None
                intent = action.intent_id
                if action.kind == "maker_arrival":
                    api.arrive(
                        intent,
                        occurred_at=action.due_at,
                        passive_book_snapshot=ingress.latest_book_ref(action.context.quoted_product),
                    )
                elif action.kind == "hedge_arrival":
                    api.arrive(intent, occurred_at=action.due_at)
                    execution = api.execute_hedge(
                        intent,
                        executed_at=action.due_at,
                        decision_mid=self._midpoint(ingress, action.context.hedge_book),
                        execution_feed_seq=ingress.feed_seq,
                    )
                    execution_ids.append(execution.execution_id)
                else:
                    raise FoundationContractError("unknown production replay action")

        eod_completion: EodCompletion | None = None
        for ingress_batch in ingress.replay():
            if eod_completion is not None:
                raise FoundationContractError("market data arrived after the calendar-declared EOD boundary")
            if eod_at < ingress_batch.exchange_ts:
                process_due(eod_at, inclusive=True)
                eod_completion = self._complete_eod(api, ingress, eod_at, decision_ids, research_decisions)
                raise FoundationContractError("market data arrived after the calendar-declared EOD boundary")
            process_due(ingress_batch.exchange_ts, inclusive=False)
            batch_fill_ids: list[str] = []
            for event in ingress_batch.events:
                if event.kind is IngressKind.BOOK:
                    ref = ingress.book_ref_for_event(event.event_id)
                    api.record_book_event(event, ref)
                    api.record_book_snapshot(ref, ingress.book_snapshot(ref))
                    research_events.append((event, ref, ingress.book_snapshot(ref)))
                    api.ingest_depth_from_snapshot(ref)
                    for trade in self._passive_trades(event, ref, ingress.feed_seq):
                        evidence_rows = api.match_passive_trade(
                            trade, fee_rebate_per_contract=self.config.passive_fee_rebate_per_contract
                        )
                        passive_fill_ids.extend(evidence.fill_id for evidence in evidence_rows)
                        batch_fill_ids.extend(evidence.fill_id for evidence in evidence_rows)
                        research_passive_evidence.extend(evidence_rows)
                    for interval in self._snapshot_intervals(event, ref, ingress.feed_seq):
                        evidence_rows = api.match_snapshot_interval(
                            interval, fee_rebate_per_contract=self.config.passive_fee_rebate_per_contract
                        )
                        passive_fill_ids.extend(evidence.fill_id for evidence in evidence_rows)
                        batch_fill_ids.extend(evidence.fill_id for evidence in evidence_rows)
                        research_passive_evidence.extend(evidence_rows)
                else:
                    ref = ingress.signal_ref_for_event(event.event_id)
                    api.record_signal_snapshot(ref, ingress.signal_snapshot(ref))
                    research_signals.append((event, ref, ingress.signal_snapshot(ref)))
            if ingress_batch.has_book_events:
                self._schedule_decision(
                    ingress,
                    api,
                    select_signal_ids,
                    propose,
                    decision_number,
                    schedule,
                    decision_ids,
                    research_decisions,
                    observed_fill_ids=tuple(batch_fill_ids),
                )
                decision_number += 1
            process_due(ingress_batch.exchange_ts, inclusive=True)
            if ingress_batch.exchange_ts >= eod_at:
                eod_completion = self._complete_eod(api, ingress, eod_at, decision_ids, research_decisions)

        if eod_completion is None:
            process_due(eod_at, inclusive=True)
            eod_completion = self._complete_eod(api, ingress, eod_at, decision_ids, research_decisions)
        api.record_inventory(f"{self.config.run_id}:eod-inventory", occurred_at=eod_completion.completed_at)
        provenance_artifacts = dict(self.config.provenance_artifacts)
        if market_data.source_provenance is not None:
            provenance_artifacts["raw_snapshot_adapter"] = market_data.source_provenance.as_provenance()
        provenance_artifacts["configuration"] = {
            "declared": provenance_artifacts["configuration"],
            "max_execution_book_age_ms_by_product": dict(self.config.max_execution_book_age_ms_by_product),
        }
        provenance_artifacts["s0_semantic_compliance"] = {
            "version": S0_SEMANTIC_COMPLIANCE_VERSION,
            "research_schema_version": RESEARCH_TELEMETRY_SCHEMA_VERSION,
            "checks": (
                "decision_scoped_causal_book_snapshots",
                "maker_queue_submission_evidence",
                "outcome_inventory_and_route_projection",
                "causal_signal_values_bound_to_context",
                "tick_validated_volatility_stress",
                "independent_pnl_and_mark_evidence",
            ),
        }
        if self._authority_registry.provenance_selectors():
            provenance_artifacts["approved_evidence_authorities"] = tuple(
                self._authority_registry.provenance_selectors()
            )
        if self.config.stress_scenario is not None:
            provenance_artifacts["stressed_ingress"] = self._stressed_ingress_provenance(events)
        if self.config.economic_inputs is not None and self.config.economic_inputs.evidence_eligible:
            provenance_artifacts.update(self.config.economic_inputs.provenance_artifacts())
        pnl_attribution = None
        if self.config.economic_inputs is None:
            api.record_unattributed_outcome(
                f"{self.config.run_id}:operational-no-pnl",
                "operational replay is not economic evidence until PnL attribution and research export are connected",
            )
        else:
            inputs = self.config.economic_inputs
            pnl_attribution = api.attribute_pnl(
                f"{self.config.run_id}:pnl",
                inputs.marks_by_product,
                inputs.accounting_view,
                inputs.cycle_view,
                tolerance=inputs.tolerance,
                eod_completion=eod_completion,
            )
        research = None
        if self.config.research_export:
            assert pnl_attribution is not None
            research = self._export_research(
                research_events,
                research_signals,
                research_decisions,
                research_passive_evidence,
                api.execution_results(),
                api.ledger_events(),
                api.maker_queue_evidence(),
                eod_completion,
                api.ledger_state(),
                pnl_attribution,
            )
            provenance_artifacts["research_manifest"] = self._research_manifest_artifact(research)
        api.capture_provenance(self.config.trial, provenance_artifacts)
        canonical = api.finalize()
        execution_freshness_eligible = self._execution_freshness_eligible(api.execution_results())
        verified_economic_evidence = bool(
            self.config.economic_inputs is not None
            and self.config.economic_inputs.verified_evidence_eligible(
                run_id=self.config.run_id,
                session_date=self.config.session_date,
                eod_at=eod_at,
                authority_registry=self._authority_registry,
            )
        )
        semantic_compliance_eligible = bool(
            "s0_semantic_compliance" in canonical.provenance.artifact_hashes
            and self.config.economic_inputs is not None
            and verified_economic_evidence
            and "approved_evidence_authorities" in canonical.provenance.artifact_hashes
            and all(
                canonical.provenance.artifact_hashes.get(name) == digest
                for name, digest in self.config.economic_inputs.source_artifact_hashes().items()
            )
            and research is not None
            and research.semantic_compliance_version == S0_SEMANTIC_COMPLIANCE_VERSION
            and canonical.provenance.artifact_hashes.get("research_manifest") == research.manifest_hash
        )
        economics_eligible = bool(
            canonical.eligible
            and pnl_attribution is not None
            and pnl_attribution.economics_eligible
            and research is not None
            and research.eligible
            and execution_freshness_eligible
            and semantic_compliance_eligible
        )
        return OperationalReplayResult(
            canonical,
            eod_completion,
            tuple(decision_ids),
            tuple(execution_ids),
            tuple(passive_fill_ids),
            pnl_attribution,
            research,
            execution_freshness_eligible,
            semantic_compliance_eligible,
            economics_eligible,
        )

    def _schedule_decision(
        self,
        ingress,
        api,
        select_signal_ids,
        propose,
        number: int,
        schedule,
        decision_ids: list[str],
        research_decisions: list[tuple[Any, PolicyProposal | None, Any]],
        *,
        observed_fill_ids: tuple[str, ...] = (),
    ) -> None:
        """Create one causal policy decision and schedule its public lifecycle actions."""
        try:
            available_signals = ingress.available_signal_refs()
            consumed_signal_ids = select_signal_ids(available_signals)
            if (
                not isinstance(consumed_signal_ids, tuple)
                or any(not isinstance(signal_id, str) or not signal_id.strip() for signal_id in consumed_signal_ids)
                or len(consumed_signal_ids) != len(set(consumed_signal_ids))
            ):
                raise FoundationContractError("policy select_signal_ids must return unique non-empty signal IDs as a tuple")
            context = ingress.decision_context(
                f"{self.config.run_id}:decision:{number:06d}",
                self.config.hedge_mapping.hedge_pair,
                consumed_signal_ids=consumed_signal_ids,
                observed_fill_ids=observed_fill_ids,
            )
        except FoundationContractError as exc:
            if "both hedge-pair books" in str(exc):
                return
            raise
        if not self._pair_session_open(context.dec_ts):
            return
        if (
            self.config.stress_scenario is not None
            and not self.config.stress_scenario.admits_opening_session()
            and not self._opening_decision_skipped
        ):
            self._opening_decision_skipped = True
            return
        proposal = propose(context)
        if not isinstance(proposal, PolicyProposal):
            raise FoundationContractError("production policy.propose must return a PolicyProposal")
        batch = proposal.batch
        research_decisions.append((context, proposal, api.ledger_state()))
        for trigger in proposal.triggers:
            api.record_trigger(trigger.trigger_id, context, occurred_at=context.dec_ts, attributes=trigger.attributes)
        if batch.maker_intent is None and batch.hedge_intent is None:
            api.submit(batch, context, occurred_at=context.dec_ts)
            decision_ids.append(context.decision_id)
            return
        schedule(self._submission_at(context.dec_ts), 0, "submit", context, batch)
        arrival_at = self._arrival_at(context.dec_ts)
        if batch.maker_intent is not None:
            schedule(arrival_at, 1, "maker_arrival", context, intent_id=batch.maker_intent.intent_id)
        if batch.hedge_intent is not None:
            schedule(arrival_at, 2, "hedge_arrival", context, intent_id=batch.hedge_intent.intent_id)

    def _complete_eod(
        self,
        api: DualBookFoundation,
        ingress: CausalIngress,
        eod_at: datetime,
        decision_ids: list[str],
        research_decisions: list[tuple[Any, PolicyProposal | None, Any]],
    ) -> EodCompletion:
        """Apply the calendar terminal boundary through the sole public EOD route."""
        context = ingress.decision_context(
            f"{self.config.run_id}:eod:{self.config.session_date.isoformat()}",
            self.config.hedge_mapping.hedge_pair,
        )
        state = api.ledger_state()
        request = EodCloseRequest(
            f"{self.config.run_id}:eod:{self.config.session_date.isoformat()}",
            context,
            {
                context.quoted_product: self._close_limit(ingress, context.quoted_book, state.quoted_position),
                context.hedge_product: self._close_limit(ingress, context.hedge_book, state.hedge_position),
            },
            self.config.default_execution_model,
        )
        completion = api.complete_eod(request, executed_at=eod_at)
        decision_ids.append(context.decision_id)
        research_decisions.append((context, None, state))
        return completion

    def _export_research(
        self,
        research_events: list[tuple[IngressEvent, BookSnapshotRef, Mapping[str, Any]]],
        research_signals: list[tuple[IngressEvent, Any, Mapping[str, Any]]],
        research_decisions: list[tuple[Any, PolicyProposal | None, Any]],
        passive_evidence: list[Any],
        executions: tuple[Any, ...],
        ledger_events: tuple[Any, ...],
        maker_queue_evidence: tuple[Any, ...],
        eod_completion: EodCompletion,
        final_state: Any,
        pnl: PnlAttributionResult,
    ) -> ResearchTelemetryResult:
        """Emit the research-owned view from causal replay records.

        The exporter deliberately requires policy-owned decision metadata.  It
        derives only transport, book, inventory, and PnL facts; guessing an
        action, reservation price, or trigger state would make the research
        artifact look more complete than the production policy actually was.
        """
        emitter = ResearchTelemetryEmitter(
            Path(self.config.artifact_root) / "research",
            self.config.run_id,
            self.config.hedge_mapping,
            self.config.session_date,
            registered_signal_ids=self.config.registered_signal_ids,
        )
        events_by_id = {event.event_id: event for event, _, _ in research_events}
        signal_by_snapshot = {ref.snapshot_id: (event, payload) for event, ref, payload in research_signals}
        payload_by_snapshot = {ref.snapshot_id: payload for _, ref, payload in research_events}
        contexts = {context.decision_id: context for context, _, _ in research_decisions}

        self._emit_research_book_events(emitter, research_events)
        for event, ref, payload in research_events:
            emitter.emit(
                "book_snapshots",
                ref.snapshot_id,
                {
                    "snapshot_id": ref.snapshot_id,
                    "decision_id": None,
                    "product": ref.product,
                    "feed_seq": ref.feed_seq,
                    "book_seq": ref.book_seq,
                    "exchange_ts": event.exchange_ts,
                    "recv_ts": event.recv_ts,
                    "snapshot_reason": "run_start" if ref.book_seq == 0 else "recovery",
                    "top_k_levels": {"bids": list(payload["bids"]), "asks": list(payload["asks"])},
                    "book_hash": ref.snapshot_hash,
                },
            )

        for context, proposal, state in research_decisions:
            quoted_event = events_by_id.get(context.quoted_book.event_id)
            hedge_event = events_by_id.get(context.hedge_book.event_id)
            if quoted_event is None or hedge_event is None:
                raise FoundationContractError("research decision inputs must resolve to retained causal book events")
            decision_snapshot_ids: dict[str, str] = {}
            for ref, event in ((context.quoted_book, quoted_event), (context.hedge_book, hedge_event)):
                try:
                    payload = payload_by_snapshot[ref.snapshot_id]
                except KeyError as exc:
                    raise FoundationContractError("research decision snapshot payload is not retained") from exc
                decision_snapshot_id = self._decision_snapshot_id(context, ref)
                decision_snapshot_ids[ref.product] = decision_snapshot_id
                emitter.emit(
                    "book_snapshots",
                    decision_snapshot_id,
                    {
                        "snapshot_id": decision_snapshot_id,
                        "decision_id": context.decision_id,
                        "product": ref.product,
                        "feed_seq": ref.feed_seq,
                        "book_seq": ref.book_seq,
                        "exchange_ts": event.exchange_ts,
                        "recv_ts": event.recv_ts,
                        "snapshot_reason": "decision",
                        "top_k_levels": {"bids": list(payload["bids"]), "asks": list(payload["asks"])},
                        "book_hash": ref.snapshot_hash,
                    },
                )
            decision_at = eod_completion.completed_at if proposal is None else context.dec_ts
            fields = self._research_decision_fields(
                context,
                proposal,
                state,
                quoted_event,
                hedge_event,
                decision_snapshot_ids,
                decision_at,
            )
            emitter.emit("decisions", context.decision_id, fields)
            self._emit_research_inventory(
                emitter,
                f"{context.decision_id}:decision",
                context,
                state,
                "decision",
                self._context_basis(context, payload_by_snapshot),
                occurred_at=decision_at,
            )
            for signal in context.consumed_signals:
                signal_record = signal_by_snapshot.get(signal.snapshot_id)
                if signal_record is None:
                    raise FoundationContractError("consumed signal must resolve to retained causal signal evidence")
                signal_event, signal_payload = signal_record
                bound_signal = context.signal_value(signal)
                if bound_signal.payload != signal_payload:
                    raise FoundationContractError("research export signal payload differs from the policy-bound causal value")
                emitter.emit(
                    "signal_snapshots",
                    f"{context.decision_id}:{signal.snapshot_id}",
                    self._research_signal_fields(context, signal, signal_event, bound_signal.payload),
                )
            if proposal is not None:
                for trigger in proposal.triggers:
                    trigger_fields = dict(self._research_trigger_fields(context, trigger.attributes))
                    trigger_fields["trigger_id"] = trigger.trigger_id
                    emitter.emit(
                        "trigger_evaluations",
                        trigger.trigger_id,
                        trigger_fields,
                    )
            else:
                emitter.emit(
                    "trigger_evaluations",
                    f"{context.decision_id}:calendar-eod",
                    {
                        "trigger_id": f"{context.decision_id}:calendar-eod",
                        "decision_id": context.decision_id,
                        "eval_ts": decision_at,
                        "feed_seq": context.feed_seq,
                        "quoted_book_seq": context.quoted_book.book_seq,
                        "hedge_book_seq": context.hedge_book.book_seq,
                        "trigger_class": "eod",
                        "inputs": {"calendar_eod": True},
                        "fired": True,
                        "target": None,
                        "reason": "calendar_eod",
                        "hysteresis_state": "eod",
                        "cooldown_ms": 0,
                    },
                )

        source_orders = self._canonical_table_rows("orders")
        queue_by_order = {evidence.intent_id: evidence for evidence in maker_queue_evidence}
        self._emit_research_orders(emitter, source_orders, queue_by_order)
        self._emit_research_passive_fills(emitter, passive_evidence)
        self._emit_research_aggressive_rows(
            emitter,
            executions,
            ledger_events,
            contexts,
            {context.decision_id: proposal for context, proposal, _ in research_decisions},
            {context.decision_id: state for context, _, state in research_decisions},
            payload_by_snapshot,
        )
        self._emit_research_ledger_inventory(emitter, ledger_events, contexts, payload_by_snapshot)
        for table in ("orders", "fills", "hedge_executions", "trigger_evaluations", "signal_snapshots"):
            emitter.declare_empty_table(table)

        ordered_contexts = tuple(contexts.values())
        if not ordered_contexts:
            raise FoundationContractError("research export requires at least one causal decision context")
        start = ordered_contexts[0]
        terminal_context = ordered_contexts[-1]
        emitter.emit(
            "inventory_series",
            f"{self.config.run_id}:eod",
            self._research_inventory_fields(
                terminal_context,
                final_state,
                eod_completion.completed_at,
                "eod",
                None,
                None,
                None,
                None,
                self._context_basis(terminal_context, payload_by_snapshot),
            ),
        )
        outcome = self._research_outcome_projection(
            source_orders,
            ledger_events,
            executions,
            research_decisions,
            eod_completion,
        )
        emitter.emit(
            "outcome_pnl",
            f"{self.config.run_id}:pnl",
            {
                "episode_id": None,
                "start_ts": start.dec_ts,
                "end_ts": eod_completion.completed_at,
                "start_decision_id": start.decision_id,
                "end_disposition": eod_completion.disposition.value,
                "maker_capture": pnl.maker_capture,
                "quoted_leg_price_pnl": pnl.quoted_leg_price_pnl,
                "hedge_leg_price_pnl": pnl.hedge_leg_price_pnl,
                "hedge_execution_shortfall": pnl.hedge_execution_shortfall,
                "fees_rebates": pnl.rebates - pnl.fees,
                "residual_basis_attribution": pnl.residual_basis_pnl,
                "episode_total": pnl.waterfall_total,
                "inventory_time": outcome["inventory_time"],
                "route_transitions": outcome["route_transitions"],
                "eod_result": eod_completion.disposition.value,
                "reconciliation_residual": pnl.reconciliation_residual,
            },
        )
        return emitter.finalize()

    def _canonical_table_rows(self, table: str) -> tuple[Mapping[str, Any], ...]:
        path = Path(self.config.artifact_root) / self.config.run_id / "tables" / f"{table}.jsonl"
        try:
            with path.open("rb") as stream:
                return tuple(json.loads(line.decode("utf-8")) for line in stream if line.strip())
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FoundationContractError(f"canonical {table} telemetry is not readable for research export") from exc

    def _research_manifest_artifact(self, research: ResearchTelemetryResult) -> bytes:
        """Return the sealed research manifest only when its declared digest still matches."""
        path = Path(self.config.artifact_root) / "research" / self.config.run_id / "meta" / "research_manifest.json"
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise FoundationContractError("sealed research manifest is not retained") from exc
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if digest != research.manifest_hash:
            raise FoundationContractError("sealed research manifest hash does not match the research result")
        return content

    def _research_outcome_projection(
        self,
        source_orders: tuple[Mapping[str, Any], ...],
        ledger_events: tuple[Any, ...],
        executions: tuple[Any, ...],
        research_decisions: list[tuple[Any, PolicyProposal | None, Any]],
        eod_completion: EodCompletion,
    ) -> Mapping[str, Any]:
        """Derive route and unhedged-inventory duration from authoritative events."""
        if not research_decisions:
            raise FoundationContractError("outcome projection requires at least one decision")
        ordered_decisions = sorted(
            research_decisions,
            key=lambda item: eod_completion.completed_at if item[1] is None else item[0].dec_ts,
        )
        start_context, _, start_state = ordered_decisions[0]
        quote_product = self.config.hedge_mapping.hedge_pair.quoted_product
        hedge_product = self.config.hedge_mapping.hedge_pair.hedge_product
        quoted_weight = float(self.config.hedge_mapping.quoted_risk_weight)
        hedge_weight = float(self.config.hedge_mapping.hedge_risk_weight)
        quoted_position = int(start_state.quoted_position)
        hedge_position = int(start_state.hedge_position)
        last_at = start_context.dec_ts
        inventory_time = 0.0
        inventory_seen = False

        def risk_open() -> bool:
            return not math.isclose(
                quoted_position * quoted_weight + hedge_position * hedge_weight,
                0.0,
                rel_tol=0.0,
                abs_tol=1e-12,
            )

        declared_orders = {
            row["order_id"]: row
            for row in source_orders
            if row.get("record_type") == "order_declared"
        }
        route: list[str] = []
        if any(row.get("role") == "maker" for row in declared_orders.values()):
            route.append("quote")

        maker_fill_seen = False
        for event in sorted(ledger_events, key=lambda value: (value.occurred_at, value.event_id)):
            if event.occurred_at < last_at:
                raise FoundationContractError("ledger event predates outcome projection start")
            if risk_open():
                inventory_time += (event.occurred_at - last_at).total_seconds()
            order = declared_orders.get(event.attributes.get("intent_id"))
            if order is not None and order.get("role") == "maker":
                maker_fill_seen = True
            if event.product == quote_product:
                quoted_position += int(event.position_delta)
            elif event.product == hedge_product:
                hedge_position += int(event.position_delta)
            else:
                raise FoundationContractError("outcome projection received ledger event outside configured pair")
            inventory_seen = inventory_seen or risk_open()
            last_at = event.occurred_at
        if risk_open():
            inventory_time += (eod_completion.completed_at - last_at).total_seconds()

        if maker_fill_seen:
            route.append("fill")
        if inventory_seen:
            route.append("inventory")
        if any(result.filled_qty > 0 and ":eod:" not in result.decision_id for result in executions):
            route.append("hedge")
        if any(result.filled_qty > 0 and ":eod:" in result.decision_id for result in executions):
            route.append("eod")
        if not route:
            route.append("no_trade")
        if route == ["quote"] and not ledger_events:
            # A quote that never fills is a route fact, but carries no inventory.
            route.append("cancel")
        return {
            "inventory_time": inventory_time,
            "route_transitions": "->".join(route),
        }

    @staticmethod
    def _decision_snapshot_id(context, ref: BookSnapshotRef) -> str:
        return f"{context.decision_id}:snapshot:{ref.product}:{ref.snapshot_id}"

    @staticmethod
    def _stressed_ingress_provenance(events: Iterable[IngressEvent]) -> Mapping[str, Any]:
        """Retain the exact post-transform ingress used for stressed execution."""
        return {
            "transform_version": "receive-time-tick-validated-v1",
            "events": tuple(
                {
                    "event_id": event.event_id,
                    "product": event.product,
                    "kind": event.kind.value,
                    "exchange_ts": event.exchange_ts,
                    "recv_ts": event.recv_ts,
                    "source_seq": event.source_seq,
                    "payload": event.payload,
                    "atomic_bundle_id": event.atomic_bundle_id,
                    "bundle_recv_ts": event.bundle_recv_ts,
                }
                for event in events
            ),
        }

    def _emit_research_orders(self, emitter, rows, queue_by_order: Mapping[str, Any]) -> None:
        declarations = {row["order_id"]: row for row in rows if row.get("record_type") == "order_declared"}
        lifecycle: dict[str, list[Mapping[str, Any]]] = {}
        reservations: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            if row.get("record_type") == "lifecycle":
                lifecycle.setdefault(row["order_id"], []).append(row)
            elif row.get("record_type") == "capacity_reservation":
                reservations.setdefault(row["order_id"], []).append(row)
        for order_id, declared in declarations.items():
            transitions = sorted(lifecycle.get(order_id, ()), key=lambda row: row["occurred_at"])
            terminal = next(
                (row for row in reversed(transitions) if row.get("lifecycle_state") not in {"submitted", "arrived", "partially_filled"}),
                transitions[-1] if transitions else None,
            )
            state = None if terminal is None else terminal.get("lifecycle_state")
            final_status = {
                "filled": "filled",
                "partially_filled": "partial",
                "cancelled": "cancelled",
                "expired": "expired",
                "rejected": "rejected",
                "stale": "failed",
                "deadline": "failed",
                "failed": "failed",
            }.get(state, "failed")
            cancellation = None if terminal is None else terminal.get("disposition_reason")
            reservations_for_order = reservations.get(order_id, ())
            reserved = max((float(row["amount"]) for row in reservations_for_order), default=None)
            role = declared["role"]
            queue_evidence = queue_by_order.get(order_id)
            emitter.emit(
                "orders",
                order_id,
                {
                    "order_id": order_id,
                    "decision_id": declared["decision_id"],
                    "product": declared["product"],
                    "side": declared["side"],
                    "order_role": "hedge" if role == "eod" else role,
                    "submit_ts": self._parse_research_timestamp(declared["occurred_at"]),
                    "timeout_ts": (
                        self._parse_research_timestamp(terminal["occurred_at"])
                        if state in {"stale", "deadline"} else None
                    ),
                    "cancel_ts": (
                        self._parse_research_timestamp(terminal["occurred_at"])
                        if state in {"cancelled", "expired", "rejected", "failed"} else None
                    ),
                    "price": declared["limit_price"],
                    "requested_qty": declared["requested_qty"],
                    "reserved_capacity": reserved,
                    "queue_ahead_submit": (
                        None
                        if role != "maker" or queue_evidence is None
                        else queue_evidence.queue_ahead_submit
                    ),
                    "final_status": final_status,
                    "cancel_reason": cancellation if state == "cancelled" else None,
                    "expiry_reason": cancellation if state in {"expired", "stale", "deadline"} else None,
                },
            )

    @staticmethod
    def _emit_research_passive_fills(emitter, evidence_rows: list[Any]) -> None:
        for evidence in evidence_rows:
            proxy_fields: dict[str, Any]
            if isinstance(evidence, SnapshotIntervalQueueProxyEvidence):
                proxy_fields = {
                    "match_evidence_type": "snapshot_interval_queue_proxy_v1",
                    "source_interval_ref": evidence.interval_reference,
                    "source_interval_quantity": evidence.interval_quantity,
                    "source_interval_bucket_index": evidence.bucket_index,
                    "source_interval_bucket_price": evidence.bucket_price,
                    "source_interval_bucket_quantity": evidence.bucket_quantity,
                    "raw_file_id": evidence.raw_file_id,
                    "raw_file_hash": evidence.raw_file_hash,
                    "raw_row_ordinal": evidence.raw_row_ordinal,
                    "proxy_model_version": evidence.model_version,
                    "price_reach_rule": evidence.price_reach_rule,
                    "availability_convention": evidence.availability_convention,
                }
            else:
                proxy_fields = {
                    "match_evidence_type": "trade_level_v1",
                    "source_interval_ref": None,
                    "source_interval_quantity": None,
                    "source_interval_bucket_index": None,
                    "source_interval_bucket_price": None,
                    "source_interval_bucket_quantity": None,
                    "raw_file_id": None,
                    "raw_file_hash": None,
                    "raw_row_ordinal": None,
                    "proxy_model_version": None,
                    "price_reach_rule": None,
                    "availability_convention": None,
                }
            emitter.emit(
                "fills",
                evidence.fill_id,
                {
                    "fill_id": evidence.fill_id,
                    "order_id": evidence.intent_id,
                    "decision_id": evidence.decision_id,
                    "product": evidence.product,
                    "side": evidence.side.value,
                    "fill_ts": evidence.fill_ts,
                    "feed_seq": evidence.feed_seq,
                    "book_seq": evidence.book_snapshot.book_seq,
                    "fill_price": evidence.fill_price,
                    "fill_qty": evidence.fill_qty,
                    "cumulative_fill_qty": evidence.cumulative_fill_qty,
                    "queue_ahead_fill": evidence.queue_ahead_fill,
                    "liquidity_role": "maker",
                    "fee_rebate": evidence.fee_rebate,
                    **proxy_fields,
                },
            )

    def _emit_research_aggressive_rows(self, emitter, executions, ledger_events, contexts, proposals, states, payloads) -> None:
        costs = {event.source_event_id: float(event.rebate) - float(event.fee) for event in ledger_events}
        quoted_position, hedge_position = 0, 0
        residual_by_execution: dict[str, float] = {}
        for event in ledger_events:
            if event.leg.value == "quoted":
                quoted_position += event.position_delta
            else:
                hedge_position += event.position_delta
            execution_id = event.attributes.get("execution_id")
            if isinstance(execution_id, str):
                residual_by_execution[execution_id] = self.config.hedge_mapping.residual_risk(quoted_position, hedge_position)
        for result in executions:
            context = contexts.get(result.decision_id)
            if context is None:
                raise FoundationContractError("research execution must resolve to a recorded decision context")
            if result.product == context.quoted_product:
                if result.filled_qty:
                    emitter.emit(
                        "fills",
                        result.execution_id,
                        {
                            "fill_id": result.execution_id,
                            "order_id": result.intent_id,
                            "decision_id": result.decision_id,
                            "product": result.product,
                            "side": result.side.value,
                            "fill_ts": result.executed_at,
                            "feed_seq": result.execution_feed_seq,
                            "book_seq": result.book_snapshot.book_seq,
                            "fill_price": result.vwap,
                            "fill_qty": result.filled_qty,
                            "cumulative_fill_qty": result.filled_qty,
                            "queue_ahead_fill": 0.0,
                            "liquidity_role": "taker",
                            "fee_rebate": costs.get(result.execution_id, 0.0),
                            "match_evidence_type": "aggressive_execution_v1",
                            "source_interval_ref": None,
                            "source_interval_bucket_index": None,
                            "source_interval_bucket_quantity": None,
                            "raw_file_hash": None,
                            "raw_row_ordinal": None,
                            "proxy_model_version": None,
                            "availability_convention": None,
                        },
                    )
                continue
            proposal = proposals.get(result.decision_id)
            fields = self._research_hedge_fields(
                result,
                context,
                proposal,
                states[result.decision_id],
                payloads,
                residual_by_execution.get(result.execution_id, states[result.decision_id].residual_risk),
            )
            emitter.emit("hedge_executions", result.execution_id, fields)

    def _research_hedge_fields(self, result, context, proposal, state, payloads, residual_risk_after) -> Mapping[str, Any]:
        if proposal is None:
            trigger_id = f"{context.decision_id}:calendar-eod"
            trigger_class = "eod"
            target_before = state.hedge_position
            target_after = 0
            retry_count = 0
            deadline_ts = None
        else:
            attributes = proposal.decision_attributes
            required = {"hedge_trigger_class", "hedge_target_before", "hedge_target_after", "hedge_retry_count", "hedge_deadline_ts"}
            missing = required - set(attributes)
            if missing:
                raise FoundationContractError(f"research hedge decision attributes are missing fields: {sorted(missing)}")
            trigger_id = attributes.get("hedge_trigger_id")
            trigger_class = attributes["hedge_trigger_class"]
            target_before = attributes["hedge_target_before"]
            target_after = attributes["hedge_target_after"]
            retry_count = attributes["hedge_retry_count"]
            deadline_ts = attributes["hedge_deadline_ts"]
        disposition = {
            "filled": "filled", "partial": "partial", "stale": "stale", "deadline": "deadline",
        }.get(result.status.value, "failure")
        vwap = 0.0 if result.vwap is None else result.vwap
        touch = 0.0 if result.executable_touch is None else result.executable_touch
        mid = 0.0 if result.decision_mid is None else result.decision_mid
        basis_at_fill = self._context_basis(context, payloads)
        return {
            "hedge_id": result.execution_id,
            "decision_id": result.decision_id,
            "trigger_id": trigger_id,
            "product": result.product,
            "side": result.side.value,
            "submit_ts": context.dec_ts,
            "completion_ts": result.executed_at,
            "trigger_class": trigger_class,
            "target_before": target_before,
            "target_after": target_after,
            "requested_qty": result.requested_qty,
            "filled_qty": result.filled_qty,
            "depth_levels_consumed": len(result.levels),
            "vwap": vwap,
            "hedge_touch": touch,
            "mid_at_decision": mid,
            "cost_vs_mid": 0.0 if result.cost_vs_decision_mid is None else result.cost_vs_decision_mid,
            "basis_at_fill": basis_at_fill,
            "residual_risk_after": residual_risk_after,
            "retry_count": retry_count,
            "deadline_ts": deadline_ts,
            "disposition": disposition,
        }

    def _emit_research_ledger_inventory(self, emitter, ledger_events, contexts, payloads) -> None:
        quoted, hedge = 0, 0
        mapping = self.config.hedge_mapping
        for event in ledger_events:
            context = contexts.get(event.decision_id)
            if context is None:
                raise FoundationContractError("research ledger effect must resolve to a recorded decision context")
            if event.product == context.quoted_product:
                quoted += event.position_delta
            else:
                hedge += event.position_delta
            order_id = event.attributes.get("intent_id")
            execution_id = event.attributes.get("execution_id")
            role = event.attributes.get("order_role")
            source = "fill" if role == "maker" else ("eod" if role == "eod" else "hedge")
            fill_id = (
                event.attributes.get("passive_fill_evidence_id")
                if source == "fill"
                else (execution_id if event.product == context.quoted_product else None)
            )
            emitter.emit(
                "inventory_series",
                f"ledger:{event.event_id}",
                {
                    "ts": event.occurred_at,
                    "feed_seq": context.feed_seq,
                    "quoted_book_seq": context.quoted_book.book_seq,
                    "hedge_book_seq": context.hedge_book.book_seq,
                    "event_source": source,
                    "decision_id": context.decision_id,
                    "order_id": order_id,
                    "fill_id": fill_id,
                    "hedge_id": execution_id if event.product == context.hedge_product else None,
                    "q": quoted,
                    "h": hedge,
                    "beta_t": float(mapping.quoted_risk_weight) / float(mapping.hedge_risk_weight),
                    "basis": self._context_basis(context, payloads),
                    "residual_risk": mapping.residual_risk(quoted, hedge),
                    "exposure_risk_scaled": quoted * float(mapping.quoted_risk_weight) / float(mapping.hedge_risk_weight) + hedge,
                },
            )

    @staticmethod
    def _parse_research_timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise FoundationContractError("canonical timestamp must be a string for research export")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FoundationContractError("canonical timestamp must be ISO-8601 for research export") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise FoundationContractError("canonical timestamp must be timezone-aware for research export")
        return parsed

    def _emit_research_book_events(
        self,
        emitter: ResearchTelemetryEmitter,
        events: list[tuple[IngressEvent, BookSnapshotRef, Mapping[str, Any]]],
    ) -> None:
        """Convert retained snapshots into explicit level deltas without inventing feed data."""
        previous_by_product: dict[str, dict[tuple[str, float], int]] = {}
        for event, ref, payload in events:
            previous = previous_by_product.get(ref.product, {})
            current: dict[tuple[str, float], tuple[int, int]] = {}
            for source_side, side in (("bids", "bid"), ("asks", "ask")):
                for level, raw in enumerate(payload[source_side]):
                    price, quantity = float(raw["price"]), int(raw["quantity"])
                    current[(side, price)] = (level, quantity)
            for key in sorted(set(previous) | set(current)):
                side, price = key
                before = previous.get(key, 0)
                level, after = current.get(key, (-1, 0))
                if level < 0:
                    level = 0
                delta = after - before
                event_type = "add" if before == 0 and after > 0 else ("cancel" if after == 0 else "modify")
                emitter.emit(
                    "book_events",
                    f"{ref.snapshot_id}:{ref.product}:{side}:{level}:{price}",
                    {
                        "product": ref.product,
                        "feed_seq": ref.feed_seq,
                        "book_seq": ref.book_seq,
                        "exchange_ts": event.exchange_ts,
                        "recv_ts": event.recv_ts,
                        "level": level,
                        "side": side,
                        "price": price,
                        "event_type": event_type,
                        "qty_delta": delta,
                        "displayed_qty_after": after,
                        "ofi_delta": float(delta if side == "bid" else -delta),
                    },
                )
            previous_by_product[ref.product] = {
                key: quantity for key, (_, quantity) in current.items()
            }

    def _research_decision_fields(
        self,
        context,
        proposal,
        state,
        quoted_event,
        hedge_event,
        decision_snapshot_ids: Mapping[str, str],
        decision_at: datetime,
    ) -> Mapping[str, Any]:
        if (
            context.exchange_batch is None
            or quoted_event.exchange_ts != hedge_event.exchange_ts
            or quoted_event.exchange_ts != context.exchange_batch.exchange_ts
            or context.quoted_book.exchange_batch != context.exchange_batch
            or context.hedge_book.exchange_batch != context.exchange_batch
        ):
            raise FoundationContractError("research decision requires one aligned exchange-batch snapshot pair")
        if proposal is None:
            attributes: Mapping[str, Any] = {
                "side": "buy",
                "action": "no_trade",
                "quote_price": None,
                "size": None,
                "quote_age_ms": None,
                "queue_ahead": None,
                "reservation_price": self._midpoint_payload(quoted_event.payload),
                "skew": 0.0,
                "cap_state": "within_cap",
                "capacity_reserved": 0.0,
                "block_reason": "calendar_eod",
                "cancel_reason": None,
                "trigger_priority": "eod",
                "hysteresis_state": "eod",
            }
        else:
            attributes = proposal.decision_attributes
            required = {
                "side", "action", "quote_price", "size", "quote_age_ms", "queue_ahead", "reservation_price",
                "skew", "cap_state", "capacity_reserved", "block_reason", "cancel_reason", "trigger_priority",
                "hysteresis_state",
            }
            missing = required - set(attributes)
            if missing:
                raise FoundationContractError(
                    f"research export requires policy decision attributes: {sorted(missing)}"
                )
        signal_ids = sorted(signal.snapshot_id for signal in context.consumed_signals)
        signal_set_hash = "sha256:" + hashlib.sha256(
            json.dumps(signal_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "decision_id": context.decision_id,
            "exchange_ts": context.exchange_batch.exchange_ts,
            "recv_ts": max(quoted_event.recv_ts, hedge_event.recv_ts),
            "dec_ts": decision_at,
            "feed_seq": context.feed_seq,
            "quoted_book_seq": context.quoted_book.book_seq,
            "hedge_book_seq": context.hedge_book.book_seq,
            "quoted_snapshot_id": decision_snapshot_ids[context.quoted_product],
            "hedge_snapshot_id": decision_snapshot_ids[context.hedge_product],
            "side": attributes["side"],
            "action": attributes["action"],
            "quote_price": attributes["quote_price"],
            "size": attributes["size"],
            "quote_age_ms": attributes["quote_age_ms"],
            "queue_ahead": attributes["queue_ahead"],
            "reservation_price": attributes["reservation_price"],
            "skew": attributes["skew"],
            "inventory_q": float(state.quoted_position),
            "inventory_h": float(state.hedge_position),
            "residual_risk": state.residual_risk,
            "cap_state": attributes["cap_state"],
            "capacity_reserved": attributes["capacity_reserved"],
            "signal_set_hash": signal_set_hash,
            "block_reason": attributes["block_reason"],
            "cancel_reason": attributes["cancel_reason"],
            "trigger_priority": attributes["trigger_priority"],
            "hysteresis_state": attributes["hysteresis_state"],
        }

    def _research_signal_fields(self, context, signal, event, payload) -> Mapping[str, Any]:
        required = {
            "model_version", "feature_version", "source", "score", "regime", "calibration_bucket", "feature_coverage"
        }
        missing = required - set(payload)
        if missing:
            raise FoundationContractError(f"research signal payload is missing fields: {sorted(missing)}")
        return {
            "signal_id": signal.signal_id,
            "signal_snapshot_id": signal.snapshot_id,
            "decision_id": context.decision_id,
            "model_version": payload["model_version"],
            "feature_version": payload["feature_version"],
            "source": payload["source"],
            "score": payload["score"],
            "regime": payload["regime"],
            "calibration_bucket": payload["calibration_bucket"],
            "available_at": (
                signal.exchange_batch.exchange_ts if signal.exchange_batch is not None else signal.available_at
            ),
            "age_ms": int(
                (
                    context.dec_ts
                    - (signal.exchange_batch.exchange_ts if signal.exchange_batch is not None else signal.available_at)
                ).total_seconds()
                * 1000
            ),
            "feature_snapshot_hash": signal.snapshot_hash,
            "feature_coverage": payload["feature_coverage"],
        }

    @staticmethod
    def _research_trigger_fields(context, attributes: Mapping[str, Any]) -> Mapping[str, Any]:
        required = {"trigger_class", "inputs", "fired", "target", "reason", "hysteresis_state", "cooldown_ms"}
        missing = required - set(attributes)
        if missing:
            raise FoundationContractError(f"research trigger attributes are missing fields: {sorted(missing)}")
        return {
            "decision_id": context.decision_id,
            "eval_ts": context.dec_ts,
            "feed_seq": context.feed_seq,
            "quoted_book_seq": context.quoted_book.book_seq,
            "hedge_book_seq": context.hedge_book.book_seq,
            **{name: attributes[name] for name in required},
        }

    def _emit_research_inventory(
        self,
        emitter,
        entity_id,
        context,
        state,
        source: str,
        basis: float,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        emitter.emit(
            "inventory_series",
            entity_id,
            self._research_inventory_fields(
                context,
                state,
                context.dec_ts if occurred_at is None else occurred_at,
                source,
                context.decision_id,
                None,
                None,
                None,
                basis,
            ),
        )

    def _research_inventory_fields(
        self, context, state, timestamp, source, decision_id, order_id, fill_id, hedge_id, basis
    ) -> Mapping[str, Any]:
        mapping = self.config.hedge_mapping
        beta = float(mapping.quoted_risk_weight) / float(mapping.hedge_risk_weight)
        return {
            "ts": timestamp,
            "feed_seq": context.feed_seq,
            "quoted_book_seq": context.quoted_book.book_seq,
            "hedge_book_seq": context.hedge_book.book_seq,
            "event_source": source,
            "decision_id": decision_id,
            "order_id": order_id,
            "fill_id": fill_id,
            "hedge_id": hedge_id,
            "q": state.quoted_position,
            "h": state.hedge_position,
            "beta_t": beta,
            "basis": basis,
            "residual_risk": state.residual_risk,
            "exposure_risk_scaled": state.quoted_position * beta + state.hedge_position,
        }

    def _context_basis(self, context, payloads: Mapping[str, Mapping[str, Any]]) -> float:
        try:
            return self._midpoint_payload(payloads[context.quoted_book.snapshot_id]) - self._midpoint_payload(
                payloads[context.hedge_book.snapshot_id]
            )
        except KeyError as exc:
            raise FoundationContractError("research inventory requires both decision book payloads") from exc

    def _execution_freshness_eligible(self, executions: tuple[Any, ...]) -> bool:
        """Check the declared per-product arrival-book age gate without inference."""
        required_products = {
            self.config.hedge_mapping.hedge_pair.quoted_product,
            self.config.hedge_mapping.hedge_pair.hedge_product,
        }
        thresholds = self.config.max_execution_book_age_ms_by_product
        if set(thresholds) != required_products:
            return False
        for result in executions:
            visible_at = (
                result.book_snapshot.exchange_batch.exchange_ts
                if result.book_snapshot.exchange_batch is not None
                else result.book_snapshot.available_at
            )
            age_ms = (result.executed_at - visible_at).total_seconds() * 1000.0
            if age_ms < 0 or age_ms > thresholds[result.product]:
                return False
        return True

    @staticmethod
    def _midpoint_payload(payload: Mapping[str, Any]) -> float:
        return (ProductionReplayAdapter._best_price(payload, "bids") + ProductionReplayAdapter._best_price(payload, "asks")) / 2.0

    def _eod_at(self) -> datetime:
        pair = self.config.hedge_mapping.hedge_pair
        calendars = {
            spec.calendar.eod_at(self.config.session_date)
            for spec in self.config.instrument_specs
            if spec.product in {pair.quoted_product, pair.hedge_product}
        }
        if None in calendars or len(calendars) != 1:
            raise FoundationContractError("both hedge-pair instruments must declare one identical terminal EOD timestamp")
        return next(iter(calendars))

    def _filter_calendar_book_events(self, events: Iterable[IngressEvent]) -> tuple[IngressEvent, ...]:
        """Reject or explicitly drop book rows outside their declared product session.

        Exchange time determines whether the market observation belongs to the
        session and is the only replay clock. Signals are retained during a
        break so a post-break exchange batch can consume them, but no policy
        decision is made until both pair calendars are open again.
        """
        specifications = {spec.product: spec for spec in self.config.instrument_specs}
        accepted: list[IngressEvent] = []
        for event in events:
            if event.kind is not IngressKind.BOOK:
                accepted.append(event)
                continue
            calendar = specifications[event.product].calendar
            if calendar.trading_day_of(event.exchange_ts) != self.config.session_date:
                raise FoundationContractError("market data event does not belong to the configured trading day")
            if not calendar.windows or calendar.is_trading_time(event.exchange_ts):
                accepted.append(event)
            elif calendar.missing_data_disposition == "drop":
                continue
            else:
                raise FoundationContractError("market data event occurs outside the declared product session")
        return tuple(accepted)

    def _pair_session_open(self, value: datetime) -> bool:
        pair = self.config.hedge_mapping.hedge_pair
        specifications = {spec.product: spec for spec in self.config.instrument_specs}
        return all(
            specifications[product].calendar.trading_day_of(value) == self.config.session_date
            and (
                not specifications[product].calendar.windows
                or specifications[product].calendar.is_trading_time(value)
            )
            for product in (pair.quoted_product, pair.hedge_product)
        )

    def _submission_at(self, value: datetime) -> datetime:
        scenario = self.config.stress_scenario
        return value if scenario is None else scenario.submission_at(value)

    def _arrival_at(self, value: datetime) -> datetime:
        scenario = self.config.stress_scenario
        return value if scenario is None else scenario.arrival_at(value)

    @staticmethod
    def _midpoint(ingress: CausalIngress, ref: BookSnapshotRef) -> float:
        payload = ingress.book_snapshot(ref)
        return (ProductionReplayAdapter._best_price(payload, "bids") + ProductionReplayAdapter._best_price(payload, "asks")) / 2.0

    @staticmethod
    def _close_limit(ingress: CausalIngress, ref: BookSnapshotRef, position: int) -> float:
        payload = ingress.book_snapshot(ref)
        side = "bids" if position > 0 else "asks"
        return ProductionReplayAdapter._best_price(payload, side)

    @staticmethod
    def _best_price(payload: Mapping[str, Any], side: str) -> float:
        try:
            levels = payload[side]
            price = float(levels[0]["price"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FoundationContractError(f"retained book has no usable {side} top-of-book price") from exc
        if not math.isfinite(price) or price <= 0:
            raise FoundationContractError("retained top-of-book price must be finite and positive")
        return price

    def _passive_trades(self, event: IngressEvent, ref: BookSnapshotRef, feed_seq: int) -> tuple[PassiveTrade, ...]:
        """Decode optional, causally co-received aggressor trades from strict ingress."""
        raw_trades = event.payload.get("passive_trades", ())
        if raw_trades is None:
            return ()
        if not isinstance(raw_trades, (tuple, list)):
            raise FoundationContractError("book payload passive_trades must be a list of trade mappings")
        if event.product != self.config.hedge_mapping.hedge_pair.quoted_product and raw_trades:
            raise FoundationContractError("passive trades may be carried only on the quoted product book")
        trades: list[PassiveTrade] = []
        for index, raw_trade in enumerate(raw_trades):
            if not isinstance(raw_trade, Mapping):
                raise FoundationContractError("passive trade records must be mappings")
            try:
                side = OrderSide(str(raw_trade["taker_side"]))
                price = float(raw_trade["price"])
                quantity = int(raw_trade["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FoundationContractError("passive trade requires taker_side, price, and quantity") from exc
            trade_id = raw_trade.get("trade_id", f"{event.event_id}:trade:{index}")
            if not isinstance(trade_id, str) or not trade_id.strip():
                raise FoundationContractError("passive trade_id must be a non-empty string")
            trades.append(
                PassiveTrade(
                    trade_id,
                    self.config.run_id,
                    self.config.hedge_mapping.hedge_pair,
                    event.product,
                    side,
                    event.exchange_ts,
                    feed_seq,
                    ref,
                    price,
                    quantity,
                    event.event_id,
                )
            )
        return tuple(trades)

    def _snapshot_intervals(
        self, event: IngressEvent, ref: BookSnapshotRef, feed_seq: int
    ) -> tuple[SnapshotInterval, ...]:
        """Decode adapter-owned snapshot intervals without inventing trades."""
        raw_interval = event.payload.get("snapshot_interval")
        if raw_interval is None:
            return ()
        if not isinstance(raw_interval, Mapping):
            raise FoundationContractError("snapshot_interval must be a mapping")
        if event.product != self.config.hedge_mapping.hedge_pair.quoted_product:
            raise FoundationContractError("snapshot intervals may be carried only on the quoted product book")
        try:
            buckets = tuple(
                SnapshotIntervalPriceBucket(float(bucket["price"]), int(bucket["quantity"]))
                for bucket in raw_interval["buckets"]
            )
            return (
                SnapshotInterval(
                    str(raw_interval["interval_id"]),
                    str(raw_interval["raw_file_id"]),
                    str(raw_interval["raw_file_hash"]),
                    int(raw_interval["raw_row_ordinal"]),
                    self.config.run_id,
                    self.config.hedge_mapping.hedge_pair,
                    event.product,
                    event.exchange_ts,
                    feed_seq,
                    ref,
                    str(raw_interval["model_version"]),
                    str(raw_interval["price_reach_rule"]),
                    int(raw_interval["quantity"]),
                    buckets,
                    str(raw_interval["availability_convention"]),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FoundationContractError("snapshot_interval has invalid required fields") from exc

    def _require_snapshot_adapter_provenance(
        self,
        market_data: ValidatedMarketData,
        *,
        book_events: tuple[IngressEvent, ...] | None = None,
    ) -> None:
        """Bind proxy intervals and immutable replay payload to authenticated provenance."""
        provenance = market_data.source_provenance
        replay_events = book_events if book_events is not None else market_data.to_ingress_events(
            event_id_prefix=f"{self.config.run_id}:book"
        )
        raw_intervals = tuple(event.payload.get("snapshot_interval") for event in replay_events)
        has_snapshot_intervals = any(isinstance(value, Mapping) for value in raw_intervals)
        if not isinstance(provenance, RawSnapshotAdapterProvenance):
            if not has_snapshot_intervals:
                return
            raise FoundationContractError(
                "snapshot intervals require authenticated raw_snapshot_adapter_v1 provenance from declared raw file bytes"
            )
        if not provenance.source_bytes_authenticated:
            if not has_snapshot_intervals:
                return
            raise FoundationContractError(
                "snapshot intervals require authenticated raw_snapshot_adapter_v1 provenance from declared raw file bytes"
            )
        if provenance.adapted_replay_hash != adapted_replay_events_hash(replay_events):
            raise FoundationContractError(
                "adapted raw snapshot replay payload does not match authenticated provenance"
            )
        adapter_config = provenance.config
        expected_proxy_contracts = (self.config.hedge_mapping.hedge_pair.quoted_product,)
        if adapter_config.proxy_interval_contracts != expected_proxy_contracts:
            raise FoundationContractError(
                "raw snapshot adapter proxy_interval_contracts must exactly match the replay quoted product"
            )
        if not has_snapshot_intervals:
            return
        specifications = {spec.product: spec for spec in self.config.instrument_specs}
        for contract in adapter_config.declared_contract_universe:
            specification = specifications.get(contract)
            if specification is None:
                raise FoundationContractError("raw snapshot adapter contract has no configured InstrumentSpec")
            if not math.isclose(
                float(adapter_config.tick_by_contract[contract]), float(specification.tick), rel_tol=0.0, abs_tol=1e-12
            ) or not math.isclose(
                float(adapter_config.multiplier_by_contract[contract]),
                float(specification.multiplier),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise FoundationContractError("raw snapshot adapter tick/multiplier must match configured InstrumentSpec")

        sources = {(source.source_id, source.raw_file_hash): source for source in provenance.sources}
        for event in replay_events:
            raw_interval = event.payload.get("snapshot_interval")
            if raw_interval is None:
                continue
            if not isinstance(raw_interval, Mapping):
                raise FoundationContractError("snapshot_interval must be a mapping or None")
            raw_file_id = raw_interval.get("raw_file_id")
            raw_file_hash = raw_interval.get("raw_file_hash")
            raw_row_ordinal = raw_interval.get("raw_row_ordinal")
            if (
                not isinstance(raw_file_id, str)
                or not raw_file_id
                or not isinstance(raw_file_hash, str)
                or not isinstance(raw_row_ordinal, int)
                or isinstance(raw_row_ordinal, bool)
            ):
                raise FoundationContractError("snapshot interval source identity is invalid")
            source = sources.get((raw_file_id, raw_file_hash))
            if source is None:
                raise FoundationContractError("snapshot interval source identity is absent from declared provenance")
            if source.contract != event.product:
                raise FoundationContractError("snapshot interval source contract must match the interval row contract")
            if raw_row_ordinal < 0 or raw_row_ordinal >= source.rows:
                raise FoundationContractError("snapshot interval raw_row_ordinal is outside the declared source extent")
            if (
                raw_interval.get("model_version") != adapter_config.model_version
                or raw_interval.get("price_reach_rule") != adapter_config.price_reach_rule
                or raw_interval.get("availability_convention") != adapter_config.availability_convention
            ):
                raise FoundationContractError("snapshot interval model fields must match raw snapshot adapter provenance")


def _canonical_economic_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode a signed external economic artifact with one deterministic representation."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FoundationContractError("economic evidence source must be canonical JSON") from exc


__all__ = [
    "DeploymentEvidenceAuthorityRegistry",
    "EconomicReplayInputs",
    "OperationalReplayResult",
    "ProductionReplayAdapter",
    "ProductionReplayConfig",
]
