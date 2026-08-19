"""Declared, independently composable stress controls for foundation trials.

The controls are pure transformations.  A runner applies ingress delay before
``CausalIngress``; ``DualBookFoundation`` applies action timing, fee, and basis
controls where it owns those values.  Participation variants produce distinct,
versioned execution-model references.  Every non-base scenario has a canonical
provenance payload suitable for hashing and telemetry emission.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timedelta
from types import MappingProxyType

from common.foundation_contracts import (
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
)


@dataclass(frozen=True)
class StressScenario:
    """One versioned, declared set of stress dimensions; base values are neutral."""

    scenario_id: str
    version: str
    market_data_delay_ms: float = 0.0
    signal_delay_ms: float = 0.0
    action_submission_delay_ms: float = 0.0
    action_arrival_delay_ms: float = 0.0
    participation_multiplier: float = 1.0
    fee_multiplier: float = 1.0
    basis_shift: float = 0.0
    volatility_multiplier: float = 1.0
    opening_session_disposition: str = "allow"

    def __post_init__(self) -> None:
        for field_name in ("scenario_id", "version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise FoundationContractError(f"{field_name} must be a non-empty string")
        for field_name in (
            "market_data_delay_ms",
            "signal_delay_ms",
            "action_submission_delay_ms",
            "action_arrival_delay_ms",
        ):
            value = _finite(getattr(self, field_name), field_name)
            if value < 0:
                raise FoundationContractError(f"{field_name} must be non-negative")
        if self.market_data_delay_ms != 0.0 or self.signal_delay_ms != 0.0:
            raise FoundationContractError(
                "receive-time market-data and signal delays are unsupported by exchange-batch replay"
            )
        for field_name in ("participation_multiplier", "fee_multiplier", "volatility_multiplier"):
            value = _finite(getattr(self, field_name), field_name)
            if value <= 0 or value > 1:
                raise FoundationContractError(f"{field_name} must be within (0, 1]")
        _finite(self.basis_shift, "basis_shift")
        if self.opening_session_disposition not in {"allow", "skip"}:
            raise FoundationContractError("opening_session_disposition must be 'allow' or 'skip'")

    @property
    def is_base(self) -> bool:
        return self.as_provenance() == StressScenario(self.scenario_id, self.version).as_provenance()

    def as_provenance(self) -> Mapping[str, object]:
        """Return canonical, content-hashable stress configuration."""
        return MappingProxyType(
            {
                "scenario_id": self.scenario_id,
                "version": self.version,
                "market_data_delay_ms": float(self.market_data_delay_ms),
                "signal_delay_ms": float(self.signal_delay_ms),
                "action_submission_delay_ms": float(self.action_submission_delay_ms),
                "action_arrival_delay_ms": float(self.action_arrival_delay_ms),
                "participation_multiplier": float(self.participation_multiplier),
                "fee_multiplier": float(self.fee_multiplier),
                "basis_shift": float(self.basis_shift),
                "volatility_multiplier": float(self.volatility_multiplier),
                "volatility_transform": "tick_conservative_v1",
                "opening_session_disposition": self.opening_session_disposition,
            }
        )

    def submission_at(self, occurred_at: datetime) -> datetime:
        return _delay(occurred_at, self.action_submission_delay_ms, "occurred_at")

    def arrival_at(self, occurred_at: datetime) -> datetime:
        return _delay(
            occurred_at,
            float(self.action_submission_delay_ms) + float(self.action_arrival_delay_ms),
            "occurred_at",
        )

    def adjusted_fee(self, value: float) -> float:
        return _finite(value, "fee") * float(self.fee_multiplier)

    def adjusted_decision_mid(self, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, "decision_mid") + float(self.basis_shift)

    def adjusted_participation(self, value: float) -> float:
        rate = _finite(value, "participation_rate")
        if not 0.0 <= rate <= 1.0:
            raise FoundationContractError("participation_rate must be within [0, 1]")
        return rate * float(self.participation_multiplier)

    def adjusted_price(self, price: float, *, reference_price: float) -> float:
        """Scale a price deviation from a declared reference for volatility stress."""
        return _finite(reference_price, "reference_price") + (
            _finite(price, "price") - _finite(reference_price, "reference_price")
        ) * float(self.volatility_multiplier)

    def admits_opening_session(self) -> bool:
        return self.opening_session_disposition == "allow"


@dataclass(frozen=True)
class StressedExecutionModels:
    """Versioned execution configurations transformed only by participation stress."""

    models: tuple[ExecutionModelConfig, ...]
    default_execution_model: ExecutionModelRef
    references: Mapping[ExecutionModelRef, ExecutionModelRef]

    def __post_init__(self) -> None:
        if not self.models or any(not isinstance(model, ExecutionModelConfig) for model in self.models):
            raise FoundationContractError("models must be a non-empty tuple of ExecutionModelConfig values")
        if not isinstance(self.default_execution_model, ExecutionModelRef):
            raise FoundationContractError("default_execution_model must be an ExecutionModelRef")
        if not isinstance(self.references, Mapping):
            raise FoundationContractError("references must be a mapping")
        object.__setattr__(self, "references", MappingProxyType(dict(self.references)))

    def reference_for(self, original: ExecutionModelRef | None) -> ExecutionModelRef:
        if original is None:
            return self.default_execution_model
        if not isinstance(original, ExecutionModelRef):
            raise FoundationContractError("original execution-model reference must be an ExecutionModelRef or None")
        try:
            return self.references[original]
        except KeyError as exc:
            raise FoundationContractError("execution-model reference is not part of this stress configuration") from exc


def apply_ingress_stress(
    events: Iterable[IngressEvent],
    scenario: StressScenario,
    *,
    instrument_specs: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec] | None = None,
) -> tuple[IngressEvent, ...]:
    """Apply batch-safe payload stress without altering exchange-batch ordering."""
    if not isinstance(scenario, StressScenario):
        raise FoundationContractError("scenario must be a StressScenario")
    values = tuple(events)
    if any(not isinstance(event, IngressEvent) for event in values):
        raise FoundationContractError("events must contain IngressEvent values")
    specs = _instrument_specs(instrument_specs)
    if scenario.volatility_multiplier != 1.0 and not specs:
        raise FoundationContractError("volatility stress requires declared instrument_specs for tick preservation")
    adjusted: list[IngressEvent] = []
    for event in values:
        spec = specs.get(event.product)
        if event.kind is IngressKind.BOOK:
            if spec is None and specs:
                raise FoundationContractError("stress instrument_specs must cover every book product")
            payload = _volatility_stressed_payload(event.payload, scenario, spec)
        else:
            payload = event.payload
        transformed = replace(
            event,
            payload=payload,
        )
        if transformed.kind is IngressKind.BOOK and spec is not None:
            _validate_stressed_book_event(transformed, spec)
        adjusted.append(transformed)
    return tuple(adjusted)


def stressed_execution_models(
    models: Iterable[ExecutionModelConfig], default_execution_model: ExecutionModelRef, scenario: StressScenario
) -> StressedExecutionModels:
    """Derive named/versioned execution models with only participation changed."""
    if not isinstance(default_execution_model, ExecutionModelRef):
        raise FoundationContractError("default_execution_model must be an ExecutionModelRef")
    if not isinstance(scenario, StressScenario):
        raise FoundationContractError("scenario must be a StressScenario")
    values = tuple(models)
    transformed: list[ExecutionModelConfig] = []
    references: dict[ExecutionModelRef, ExecutionModelRef] = {}
    for model in values:
        if not isinstance(model, ExecutionModelConfig):
            raise FoundationContractError("models must contain ExecutionModelConfig values")
        original = ExecutionModelRef(model.model_id, model.version)
        stressed_ref = ExecutionModelRef(model.model_id, f"{model.version}+stress:{scenario.scenario_id}:{scenario.version}")
        metadata = {**model.metadata, "stress_scenario": dict(scenario.as_provenance())}
        transformed.append(
            ExecutionModelConfig(
                model.model_id,
                stressed_ref.version,
                scenario.adjusted_participation(model.participation_rate),
                model.allow_synthetic_depth_refresh,
                model.sparse_book_disposition,
                metadata,
            )
        )
        references[original] = stressed_ref
    try:
        default = references[default_execution_model]
    except KeyError as exc:
        raise FoundationContractError("default_execution_model must be included in models") from exc
    return StressedExecutionModels(tuple(transformed), default, references)


def _finite(value: object, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise FoundationContractError(f"{field_name} must be finite") from exc
    if not math.isfinite(numeric):
        raise FoundationContractError(f"{field_name} must be finite")
    return numeric


def _delay(value: datetime, milliseconds: float, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FoundationContractError(f"{field_name} must be a timezone-aware datetime")
    return value + timedelta(milliseconds=float(milliseconds))


def _volatility_stressed_payload(
    payload: Mapping[str, object],
    scenario: StressScenario,
    spec: InstrumentSpec | None,
) -> Mapping[str, object]:
    """Scale quote and embedded trade deviations around the contemporaneous mid."""
    if scenario.volatility_multiplier == 1.0:
        return payload
    try:
        bids = payload["bids"]
        asks = payload["asks"]
        reference = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2.0
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FoundationContractError("volatility stress requires usable bid and ask top levels") from exc
    if spec is None:
        raise FoundationContractError("volatility stress requires an InstrumentSpec")
    adjusted = dict(payload)
    for side, rounding in (("bids", ROUND_FLOOR), ("asks", ROUND_CEILING)):
        raw_levels = payload[side]
        if not isinstance(raw_levels, (tuple, list)):
            raise FoundationContractError("volatility stress requires list-like book levels")
        adjusted[side] = [
            {
                **dict(level),
                "price": _tick_adjusted_price(
                    float(level["price"]),
                    reference_price=reference,
                    multiplier=float(scenario.volatility_multiplier),
                    tick=float(spec.tick),
                    rounding=rounding,
                ),
            }
            for level in raw_levels
        ]
    raw_trades = payload.get("passive_trades")
    if raw_trades is not None:
        if not isinstance(raw_trades, (tuple, list)):
            raise FoundationContractError("volatility stress requires list-like passive trades")
        adjusted_trades = []
        for trade in raw_trades:
            side = dict(trade).get("taker_side")
            rounding = ROUND_FLOOR if side == "sell" else ROUND_CEILING if side == "buy" else None
            if rounding is None:
                raise FoundationContractError("volatility stress passive trades require taker_side 'buy' or 'sell'")
            adjusted_trades.append(
                {
                    **dict(trade),
                    "price": _tick_adjusted_price(
                        float(trade["price"]),
                        reference_price=reference,
                        multiplier=float(scenario.volatility_multiplier),
                        tick=float(spec.tick),
                        rounding=rounding,
                    ),
                }
            )
        adjusted["passive_trades"] = adjusted_trades
    if payload.get("snapshot_interval") is not None:
        raise FoundationContractError(
            "volatility stress is unsupported for snapshot-interval proxy evidence"
        )
    return adjusted


def _instrument_specs(
    values: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec] | None,
) -> Mapping[str, InstrumentSpec]:
    if values is None:
        return MappingProxyType({})
    if isinstance(values, Mapping):
        result = dict(values)
    else:
        result = {spec.product: spec for spec in values}
    if not result or any(not isinstance(product, str) or not isinstance(spec, InstrumentSpec) or spec.product != product for product, spec in result.items()):
        raise FoundationContractError("instrument_specs must map each product to its InstrumentSpec")
    return MappingProxyType(result)


def _tick_adjusted_price(
    price: float,
    *,
    reference_price: float,
    multiplier: float,
    tick: float,
    rounding: str,
) -> float:
    adjusted = Decimal(str(reference_price)) + (
        Decimal(str(price)) - Decimal(str(reference_price))
    ) * Decimal(str(multiplier))
    tick_decimal = Decimal(str(tick))
    snapped = (adjusted / tick_decimal).to_integral_value(rounding=rounding) * tick_decimal
    return float(snapped)


def _validate_stressed_book_event(event: IngressEvent, spec: InstrumentSpec) -> None:
    """Fail closed unless a transformed book remains valid for its instrument."""
    payload = event.payload
    try:
        bids = payload["bids"]
        asks = payload["asks"]
    except (KeyError, TypeError) as exc:
        raise FoundationContractError("stressed book payload requires bids and asks") from exc
    if not isinstance(bids, (tuple, list)) or not isinstance(asks, (tuple, list)) or not bids or not asks:
        raise FoundationContractError("stressed book payload requires non-empty bid and ask levels")

    def prices(levels: object, side: str) -> list[float]:
        result: list[float] = []
        for level in levels:
            if not isinstance(level, Mapping):
                raise FoundationContractError("stressed book level must be a mapping")
            try:
                price = float(level["price"])
                quantity = int(level["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FoundationContractError("stressed book level requires numeric price and integer quantity") from exc
            if not math.isfinite(price) or price <= 0 or quantity <= 0:
                raise FoundationContractError("stressed book levels require positive finite price and quantity")
            tick = Decimal(str(spec.tick))
            units = Decimal(str(price)) / tick
            if units != units.to_integral_value():
                raise FoundationContractError(f"stressed_{side}_price_off_tick")
            result.append(price)
        return result

    bid_prices = prices(bids, "bid")
    ask_prices = prices(asks, "ask")
    if any(first <= second for first, second in zip(bid_prices, bid_prices[1:])):
        raise FoundationContractError("stressed_bid_book_not_descending")
    if any(first >= second for first, second in zip(ask_prices, ask_prices[1:])):
        raise FoundationContractError("stressed_ask_book_not_ascending")
    if bid_prices[0] >= ask_prices[0]:
        raise FoundationContractError("stressed_crossed_book")
    for trade in payload.get("passive_trades", ()):
        if not isinstance(trade, Mapping):
            raise FoundationContractError("stressed passive trade must be a mapping")
        price = float(trade.get("price"))
        units = Decimal(str(price)) / Decimal(str(spec.tick))
        if not math.isfinite(price) or price <= 0 or units != units.to_integral_value():
            raise FoundationContractError("stressed_passive_trade_off_tick")
    interval = payload.get("snapshot_interval")
    if interval is not None:
        if not isinstance(interval, Mapping) or not isinstance(interval.get("buckets"), (tuple, list)):
            raise FoundationContractError("stressed snapshot interval requires list-like price buckets")
        for bucket in interval["buckets"]:
            if not isinstance(bucket, Mapping):
                raise FoundationContractError("stressed snapshot interval bucket must be a mapping")
            try:
                price = float(bucket["price"])
                quantity = int(bucket["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FoundationContractError("stressed snapshot interval bucket requires numeric price and integer quantity") from exc
            units = Decimal(str(price)) / Decimal(str(spec.tick))
            if not math.isfinite(price) or price <= 0 or quantity <= 0 or units != units.to_integral_value():
                raise FoundationContractError("stressed_snapshot_interval_bucket_off_tick")


__all__ = ["StressScenario", "StressedExecutionModels", "apply_ingress_stress", "stressed_execution_models"]
