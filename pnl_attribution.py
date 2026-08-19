"""Phase-4c canonical dual-leg PnL attribution and reconciliation.

This module consumes immutable ``DualLegLedger`` effects and durable canonical
telemetry.  It deliberately does not inspect legacy strategy records or infer
prices from mutable positions.  Every ledger fill must have exactly one priced
observation, and every priced effect belongs to exactly one waterfall row.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path

from common.foundation_contracts import (
    EodCompletion,
    FoundationContractError,
    InstrumentSpec,
    LedgerEvent,
    LedgerLeg,
    PnlAccountingView,
    PnlAttributionEffect,
    PnlAttributionResult,
    PnlPriceObservation,
)
from common.ledger import DualLegLedger
from common.telemetry import load_canonical_table


class PnlAttributionService:
    """Derive a non-overlapping waterfall and reconcile all canonical effects."""

    def attribute(
        self,
        attribution_id: str,
        ledger: DualLegLedger,
        price_observations: Iterable[PnlPriceObservation],
        marks_by_product: Mapping[str, float],
        instrument_specs: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec],
        *,
        telemetry_run_dir: str | Path,
        accounting_view: PnlAccountingView,
        cycle_view: PnlAccountingView,
        tolerance: float = 1e-9,
        eod_completion: EodCompletion | None = None,
    ) -> PnlAttributionResult:
        """Build a run verdict from ledger, canonical telemetry, and independent views.

        ``accounting_view`` and ``cycle_view`` are independent declared totals;
        neither is used to alter the waterfall.  Their residuals only determine
        eligibility.  ``marks_by_product`` is the declared common valuation
        mark for this accounting cut and must cover exactly the hedge pair.
        """
        if not isinstance(attribution_id, str) or not attribution_id.strip():
            raise FoundationContractError("attribution_id must be a non-empty string")
        if not isinstance(ledger, DualLegLedger):
            raise FoundationContractError("ledger must be a DualLegLedger")
        if not isinstance(accounting_view, PnlAccountingView) or not isinstance(cycle_view, PnlAccountingView):
            raise FoundationContractError("accounting_view and cycle_view must be PnlAccountingView values")
        if accounting_view.view_id == cycle_view.view_id:
            raise FoundationContractError("accounting_view and cycle_view must have distinct view IDs")
        try:
            numeric_tolerance = float(tolerance)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("tolerance must be a finite non-negative number") from exc
        if not math.isfinite(numeric_tolerance) or numeric_tolerance < 0:
            raise FoundationContractError("tolerance must be a finite non-negative number")

        state = ledger.reconcile()
        events = ledger.events()
        observations = self._observation_map(price_observations, events)
        marks = self._marks(marks_by_product, state.hedge_pair)
        specs = self._specs(instrument_specs, state.hedge_pair)
        effects = tuple(self._effect(event, observations[event.event_id], marks[event.product], specs[event.product]) for event in events)

        telemetry_reconciled, telemetry_failures = self._reconcile_telemetry(state, events, telemetry_run_dir)
        eod_reconciled, eod_failures = self._reconcile_eod(state, events, eod_completion)
        maker_capture = sum(effect.maker_capture for effect in effects)
        quoted_leg_price_pnl = sum(effect.quoted_leg_price_pnl for effect in effects)
        hedge_leg_price_pnl = sum(effect.hedge_leg_price_pnl for effect in effects)
        hedge_execution_shortfall = sum(effect.hedge_execution_shortfall for effect in effects)
        fees = sum(effect.fees for effect in effects)
        rebates = sum(effect.rebates for effect in effects)
        waterfall_total = maker_capture + quoted_leg_price_pnl + hedge_leg_price_pnl - hedge_execution_shortfall - fees + rebates
        accounting_residual = float(accounting_view.total_pnl) - waterfall_total
        cycle_residual = float(cycle_view.total_pnl) - waterfall_total
        failures = list(telemetry_failures) + list(eod_failures)
        if abs(accounting_residual) > numeric_tolerance:
            failures.append(f"accounting:{accounting_view.view_id}")
        if abs(cycle_residual) > numeric_tolerance:
            failures.append(f"cycle:{cycle_view.view_id}")

        return PnlAttributionResult(
            attribution_id,
            state.run_id,
            state.hedge_pair,
            effects,
            maker_capture,
            quoted_leg_price_pnl,
            hedge_leg_price_pnl,
            hedge_execution_shortfall,
            fees,
            rebates,
            waterfall_total,
            quoted_leg_price_pnl + hedge_leg_price_pnl,
            float(accounting_view.total_pnl),
            float(cycle_view.total_pnl),
            accounting_residual,
            cycle_residual,
            telemetry_reconciled,
            eod_reconciled,
            tuple(failures),
            numeric_tolerance,
            not failures and telemetry_reconciled and eod_reconciled,
        )

    @staticmethod
    def _observation_map(
        observations: Iterable[PnlPriceObservation], events: tuple[LedgerEvent, ...]
    ) -> Mapping[str, PnlPriceObservation]:
        if isinstance(observations, (str, bytes)):
            raise FoundationContractError("price_observations must contain PnlPriceObservation values")
        result: dict[str, PnlPriceObservation] = {}
        for observation in observations:
            if not isinstance(observation, PnlPriceObservation):
                raise FoundationContractError("price_observations must contain PnlPriceObservation values")
            if observation.ledger_event_id in result:
                raise FoundationContractError("price observations must have unique ledger_event_id values")
            result[observation.ledger_event_id] = observation
        event_ids = {event.event_id for event in events}
        if set(result) != event_ids:
            raise FoundationContractError("price observations must cover exactly the ledger events")
        return result

    @staticmethod
    def _marks(marks: Mapping[str, float], hedge_pair) -> Mapping[str, float]:
        if not isinstance(marks, Mapping):
            raise FoundationContractError("marks_by_product must be a mapping")
        expected_products = {hedge_pair.quoted_product, hedge_pair.hedge_product}
        if set(marks) != expected_products:
            raise FoundationContractError("marks_by_product must cover exactly the hedge pair products")
        normalized: dict[str, float] = {}
        for product, mark in marks.items():
            try:
                numeric = float(mark)
            except (TypeError, ValueError) as exc:
                raise FoundationContractError("mark prices must be finite positive numbers") from exc
            if not math.isfinite(numeric) or numeric <= 0:
                raise FoundationContractError("mark prices must be finite positive numbers")
            normalized[product] = numeric
        return normalized

    @staticmethod
    def _specs(specs: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec], hedge_pair) -> Mapping[str, InstrumentSpec]:
        items = specs.items() if isinstance(specs, Mapping) else ((spec.product, spec) for spec in specs)
        result: dict[str, InstrumentSpec] = {}
        for product, spec in items:
            if not isinstance(spec, InstrumentSpec) or product != spec.product:
                raise FoundationContractError("instrument specs must be keyed by their product")
            if product in result:
                raise FoundationContractError("instrument specs must have unique products")
            result[product] = spec
        expected_products = {hedge_pair.quoted_product, hedge_pair.hedge_product}
        if set(result) != expected_products:
            raise FoundationContractError("instrument specs must cover exactly the hedge pair products")
        return result

    @staticmethod
    def _effect(
        event: LedgerEvent,
        observation: PnlPriceObservation,
        mark: float,
        spec: InstrumentSpec,
    ) -> PnlAttributionEffect:
        signed_quantity = event.position_delta
        multiplier = float(spec.multiplier)
        fill_price = float(observation.fill_price)
        reference_price = float(observation.decision_reference_price)
        maker_capture = 0.0
        quoted_leg_price_pnl = 0.0
        hedge_leg_price_pnl = 0.0
        hedge_execution_shortfall = 0.0
        if event.leg is LedgerLeg.QUOTED:
            if event.attributes.get("effect_source") == "maker_lifecycle_fill":
                maker_capture = signed_quantity * (reference_price - fill_price) * multiplier
                quoted_leg_price_pnl = signed_quantity * (mark - reference_price) * multiplier
            else:
                # Aggressive quoted-leg EOD fills have no passive-capture claim.
                quoted_leg_price_pnl = signed_quantity * (mark - fill_price) * multiplier
        else:
            hedge_leg_price_pnl = signed_quantity * (mark - reference_price) * multiplier
            hedge_execution_shortfall = signed_quantity * (fill_price - reference_price) * multiplier
        fees, rebates = float(event.fee), float(event.rebate)
        return PnlAttributionEffect(
            event.event_id,
            event.leg,
            maker_capture,
            quoted_leg_price_pnl,
            hedge_leg_price_pnl,
            hedge_execution_shortfall,
            fees,
            rebates,
            maker_capture + quoted_leg_price_pnl + hedge_leg_price_pnl - hedge_execution_shortfall - fees + rebates,
        )

    @staticmethod
    def _reconcile_telemetry(state, events: tuple[LedgerEvent, ...], run_dir: str | Path) -> tuple[bool, tuple[str, ...]]:
        expected = {event.event_id: event for event in events}
        failures: list[str] = []
        try:
            rows = tuple(load_canonical_table(run_dir, "fills"))
        except FoundationContractError:
            return False, ("telemetry:fills_unavailable",)
        actual: dict[str, Mapping[str, object]] = {}
        for row in rows:
            # Passive matcher evidence is independently retained in the same
            # canonical table, but is not itself a ledger effect.  PnL must
            # reconcile the immutable ledger one-for-one against only the
            # rows emitted by ``DualLegLedger``.
            if row.get("record_type") != "ledger_effect":
                continue
            fill_id = row.get("fill_id")
            if not isinstance(fill_id, str) or fill_id in actual:
                failures.append(f"telemetry:fill_id:{fill_id}")
                continue
            actual[fill_id] = row
        if set(actual) != set(expected):
            failures.append("telemetry:fill_set")
        for event_id, event in expected.items():
            row = actual.get(event_id)
            if row is None:
                continue
            try:
                matches = (
                    row.get("run_id") == state.run_id
                    and row.get("pair_id") == state.hedge_pair.pair_id
                    and row.get("quoted_product") == state.hedge_pair.quoted_product
                    and row.get("hedge_product") == state.hedge_pair.hedge_product
                    and row.get("hedge_mapping_id") == state.hedge_pair.hedge_mapping_id
                    and row.get("hedge_mapping_version") == state.hedge_pair.hedge_mapping_version
                    and row.get("order_id") == event.attributes.get("intent_id")
                    and row.get("decision_id") == event.decision_id
                    and row.get("product") == event.product
                    and int(row.get("position_delta")) == event.position_delta
                    and int(row.get("quantity")) == abs(event.position_delta)
                    and row.get("source_event_id") == event.source_event_id
                    and math.isclose(float(row.get("fee", 0.0)), float(event.fee), rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(row.get("rebate", 0.0)), float(event.rebate), rel_tol=0.0, abs_tol=1e-12)
                )
            except (TypeError, ValueError):
                matches = False
            if not matches:
                failures.append(f"telemetry:{event_id}")
        return not failures, tuple(dict.fromkeys(failures))

    @staticmethod
    def _reconcile_eod(state, events: tuple[LedgerEvent, ...], completion: EodCompletion | None) -> tuple[bool, tuple[str, ...]]:
        requires_eod_completion = any(event.attributes.get("order_role") == "eod" for event in events)
        if completion is None:
            return (False, ("eod:missing_completion",)) if requires_eod_completion else (True, ())
        if not isinstance(completion, EodCompletion):
            raise FoundationContractError("eod_completion must be an EodCompletion or None")
        failures: list[str] = []
        if completion.run_id != state.run_id or completion.hedge_pair != state.hedge_pair:
            failures.append("eod:identity")
        if (
            completion.residual_quoted_position != state.quoted_position
            or completion.residual_hedge_position != state.hedge_position
        ):
            failures.append("eod:residual_positions")
        eod_event_sources = {
            event.source_event_id for event in events if event.attributes.get("order_role") == "eod"
        }
        # A zero-fill EOD result deliberately creates no ledger/PnL effect. It
        # remains visible in EodCompletion and its residual inventory is marked,
        # but only fill-producing EOD results must have a ledger source.
        if not eod_event_sources.issubset(set(completion.execution_ids)):
            failures.append("eod:execution_ids")
        return not failures, tuple(failures)


__all__ = ["PnlAttributionService"]
