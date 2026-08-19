"""Phase-3 aggressive execution over snapshot-bound, consumable depth.

The service is the supported foundation path for aggressive hedge and EOD
intents.  It deliberately does not adapt the historical ``Market.fak`` or
``PairMarket`` paths: those have incompatible legacy depth semantics and remain
compatibility-only callers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Iterable, Mapping

from common.foundation_contracts import (
    BookSnapshotRef,
    DecisionContext,
    ExecutionLevel,
    ExecutionModelConfig,
    ExecutionModelRef,
    ExecutionResult,
    ExecutionStatus,
    FoundationContractError,
    InstrumentSpec,
    OrderIntent,
    OrderRole,
    OrderSide,
)


@dataclass(frozen=True)
class DepthLevel:
    """One supplied book level. Zero quantity is explicit but not executable."""

    price: float
    quantity: int

    def __post_init__(self) -> None:
        try:
            price = float(self.price)
        except (TypeError, ValueError) as exc:
            raise FoundationContractError("depth price must be a finite positive number") from exc
        if not math.isfinite(price) or price <= 0:
            raise FoundationContractError("depth price must be a finite positive number")
        if not isinstance(self.quantity, int) or self.quantity < 0:
            raise FoundationContractError("depth quantity must be a non-negative integer")


class DepthBook:
    """Mutable remaining depth derived from one immutable causal book snapshot."""

    def __init__(
        self,
        book_snapshot: BookSnapshotRef,
        bids: Iterable[DepthLevel] = (),
        asks: Iterable[DepthLevel] = (),
    ) -> None:
        if not isinstance(book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        self.book_snapshot = book_snapshot
        self._bids = self._levels(bids, "bids")
        self._asks = self._levels(asks, "asks")

    @staticmethod
    def _levels(levels: Iterable[DepthLevel], field_name: str) -> list[DepthLevel]:
        values = list(levels)
        if any(not isinstance(level, DepthLevel) for level in values):
            raise FoundationContractError(f"{field_name} must contain DepthLevel values")
        return values

    @property
    def product(self) -> str:
        return self.book_snapshot.product

    @property
    def bids(self) -> tuple[DepthLevel, ...]:
        return tuple(self._bids)

    @property
    def asks(self) -> tuple[DepthLevel, ...]:
        return tuple(self._asks)

    def _levels_for_side(self, side: OrderSide) -> list[DepthLevel]:
        return self._asks if side is OrderSide.BUY else self._bids

    def validation_error(self, tick: float) -> str | None:
        """Return a declared invalid-book reason, otherwise ``None``."""
        if not isinstance(tick, (float, int)) or not math.isfinite(float(tick)) or float(tick) <= 0:
            raise FoundationContractError("tick must be a finite positive number")
        tolerance = float(tick) / 10.0
        for levels, descending, label in ((self._bids, True, "bid"), (self._asks, False, "ask")):
            previous = None
            for level in levels:
                snapped = _snap_price(level.price, float(tick))
                if not math.isclose(level.price, snapped, abs_tol=tolerance):
                    return f"invalid_{label}_tick"
                if previous is not None:
                    if descending and not previous > level.price:
                        return "invalid_bid_order"
                    if not descending and not previous < level.price:
                        return "invalid_ask_order"
                previous = level.price
        if self._bids and self._asks and self._bids[0].price >= self._asks[0].price:
            return "crossed_book"
        return None

    def consume(
        self,
        side: OrderSide,
        limit_price: float,
        requested_qty: int,
        participation_rate: float,
    ) -> tuple[tuple[ExecutionLevel, ...], float | None]:
        """Consume reachable depth once and return levels plus executable touch."""
        levels = self._levels_for_side(side)
        touch = levels[0].price if levels else None
        if touch is None:
            return (), None
        is_buy = side is OrderSide.BUY
        marketable = limit_price >= touch if is_buy else limit_price <= touch
        if not marketable:
            return (), touch

        remaining = requested_qty
        consumed: list[ExecutionLevel] = []
        for index, level in enumerate(levels):
            within_limit = limit_price >= level.price if is_buy else limit_price <= level.price
            if not within_limit:
                break
            executable = math.floor(level.quantity * participation_rate)
            take = min(remaining, max(0, executable))
            if take:
                consumed.append(ExecutionLevel(level.price, take))
                levels[index] = DepthLevel(level.price, level.quantity - take)
                remaining -= take
            if remaining == 0:
                break
        return tuple(consumed), touch


@dataclass(frozen=True)
class _RegisteredIntent:
    intent: OrderIntent
    context: DecisionContext
    execution_model: ExecutionModelConfig
    book_snapshot: BookSnapshotRef


def _snap_price(value: float, tick: float) -> float:
    return float(
        (Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal(str(tick))
    )


class DepthExecutionService:
    """Intent-registered, deterministic aggressive execution service.

    Each accepted intent executes at most once. Depth is only replenished by
    ``ingest_book`` with a strictly newer per-product ``book_seq``; no
    unrelated-product event and no execution call can recreate consumed depth.
    """

    def __init__(
        self,
        instrument_specs: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec],
        execution_models: Iterable[ExecutionModelConfig],
        default_execution_model: ExecutionModelRef,
    ) -> None:
        self._instrument_specs = _spec_map(instrument_specs)
        self._models = _model_map(execution_models)
        if not isinstance(default_execution_model, ExecutionModelRef):
            raise FoundationContractError("default_execution_model must be an ExecutionModelRef")
        if _model_key(default_execution_model) not in self._models:
            raise FoundationContractError("default_execution_model must be configured")
        self.default_execution_model = default_execution_model
        self._books: dict[str, DepthBook] = {}
        self._registered: dict[str, _RegisteredIntent] = {}
        self._results: dict[str, ExecutionResult] = {}
        self._execution_feed_seq = 0

    @property
    def execution_feed_seq(self) -> int:
        return self._execution_feed_seq

    def remaining_depth(self, product: str, side: OrderSide) -> tuple[DepthLevel, ...]:
        """Expose an immutable diagnostic view of currently remaining depth."""
        if not isinstance(side, OrderSide):
            raise FoundationContractError("side must be an OrderSide")
        try:
            book = self._books[product]
        except KeyError as exc:
            raise FoundationContractError("no active execution book for product") from exc
        return book.asks if side is OrderSide.BUY else book.bids

    def ingest_book(self, book: DepthBook) -> None:
        """Install a genuine newer book update; synthetic refresh is prohibited."""
        if not isinstance(book, DepthBook):
            raise FoundationContractError("book must be a DepthBook")
        if book.product not in self._instrument_specs:
            raise FoundationContractError("execution book product is not configured")
        previous = self._books.get(book.product)
        if previous is not None and book.book_snapshot.book_seq <= previous.book_snapshot.book_seq:
            raise FoundationContractError("execution book update must have a strictly newer book_seq")
        self._books[book.product] = book

    def resolve_execution_model(self, model_ref: ExecutionModelRef | None) -> ExecutionModelRef:
        """Validate and resolve an explicit model reference or the declared default.

        This is read-only so coordinators can preflight a request before they
        claim an ID or mutate lifecycle state.
        """
        if model_ref is not None and not isinstance(model_ref, ExecutionModelRef):
            raise FoundationContractError("execution model reference must be an ExecutionModelRef or None")
        resolved = model_ref or self.default_execution_model
        if _model_key(resolved) not in self._models:
            raise FoundationContractError("intent references an unconfigured execution model")
        return resolved

    def register_intent(self, intent: OrderIntent, context: DecisionContext) -> ExecutionModelRef:
        """Accept an aggressive HEDGE/EOD intent after identity/model validation."""
        if not isinstance(intent, OrderIntent):
            raise FoundationContractError("intent must be an OrderIntent")
        if not isinstance(context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        if intent.intent_id in self._registered:
            raise FoundationContractError("intent_id is already registered")
        if intent.role not in {OrderRole.HEDGE, OrderRole.EOD}:
            raise FoundationContractError("only hedge and EOD intents may use aggressive execution")
        if intent.run_id != context.run_id or intent.decision_id != context.decision_id:
            raise FoundationContractError("intent run_id and decision_id must match its decision context")
        if intent.hedge_pair != context.hedge_pair:
            raise FoundationContractError("intent hedge_pair must match its decision context")
        if intent.product == context.quoted_product:
            book_snapshot = context.quoted_book
        elif intent.product == context.hedge_product:
            book_snapshot = context.hedge_book
        else:
            raise FoundationContractError("intent product must match a decision-context product")
        if intent.product not in self._instrument_specs:
            raise FoundationContractError("intent product is not configured for execution")

        model_ref = self.resolve_execution_model(intent.execution_model_ref)
        model = self._models[_model_key(model_ref)]
        self._registered[intent.intent_id] = _RegisteredIntent(intent, context, model, book_snapshot)
        return model_ref

    def execute(
        self,
        intent_id: str,
        *,
        executed_at: datetime,
        decision_mid: float | None = None,
        execution_feed_seq: int | None = None,
    ) -> ExecutionResult:
        """Execute once against the latest book causally available at arrival.

        The registered decision snapshot remains immutable attribution evidence,
        while the active book is the newest snapshot the replay has made
        available at ``executed_at``.  A caller may pass the scheduler's
        run-wide feed sequence; component clients that do not have a scheduler
        retain the conservative maximum of the decision and active-book
        sequences.
        """
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise FoundationContractError("intent_id must be a non-empty string")
        if not isinstance(executed_at, datetime) or executed_at.tzinfo is None or executed_at.utcoffset() is None:
            raise FoundationContractError("executed_at must be a timezone-aware datetime")
        if intent_id in self._results:
            raise FoundationContractError("registered intent has already been executed")
        try:
            registered = self._registered[intent_id]
        except KeyError as exc:
            raise FoundationContractError("intent must be registered before execution") from exc
        if executed_at < registered.context.dec_ts:
            raise FoundationContractError("executed_at must not precede decision time")
        if decision_mid is not None and (not math.isfinite(float(decision_mid)) or float(decision_mid) <= 0):
            raise FoundationContractError("decision_mid must be a finite positive number when supplied")
        if execution_feed_seq is not None and (
            not isinstance(execution_feed_seq, int) or execution_feed_seq < registered.context.feed_seq
        ):
            raise FoundationContractError("execution_feed_seq must be an integer no earlier than the decision feed sequence")

        tick = float(self._instrument_specs[registered.intent.product].tick)
        limit_price = _snap_price(registered.intent.limit_price, tick)
        active_book = self._books.get(registered.intent.product)
        if active_book is None:
            return self._record_result(
                registered, executed_at, ExecutionStatus.STALE, (), None, limit_price, decision_mid, "missing_book"
            )
        visible_at = (
            active_book.book_snapshot.exchange_batch.exchange_ts
            if active_book.book_snapshot.exchange_batch is not None
            else active_book.book_snapshot.available_at
        )
        if visible_at > executed_at:
            return self._record_result(
                registered,
                executed_at,
                ExecutionStatus.STALE,
                (),
                None,
                limit_price,
                decision_mid,
                "active_book_not_available_at_execution",
            )

        invalid_reason = active_book.validation_error(tick)
        if invalid_reason is not None:
            return self._record_result(
                registered,
                executed_at,
                ExecutionStatus.FAILED,
                (),
                None,
                limit_price,
                decision_mid,
                invalid_reason,
                execution_snapshot=active_book.book_snapshot,
                execution_feed_seq=execution_feed_seq,
            )

        levels, touch = active_book.consume(
            registered.intent.side,
            limit_price,
            registered.intent.requested_qty,
            float(registered.execution_model.participation_rate),
        )
        if touch is None:
            disposition = ExecutionStatus(registered.execution_model.sparse_book_disposition)
            return self._record_result(
                registered,
                executed_at,
                disposition,
                (),
                None,
                limit_price,
                decision_mid,
                "empty_book",
                execution_snapshot=active_book.book_snapshot,
                execution_feed_seq=execution_feed_seq,
            )
        marketable = limit_price >= touch if registered.intent.side is OrderSide.BUY else limit_price <= touch
        if not marketable:
            return self._record_result(
                registered,
                executed_at,
                ExecutionStatus.REJECTED,
                (),
                touch,
                limit_price,
                decision_mid,
                "limit_not_marketable",
                execution_snapshot=active_book.book_snapshot,
                execution_feed_seq=execution_feed_seq,
            )
        if not levels:
            disposition = ExecutionStatus(registered.execution_model.sparse_book_disposition)
            return self._record_result(
                registered,
                executed_at,
                disposition,
                (),
                touch,
                limit_price,
                decision_mid,
                "no_executable_depth",
                execution_snapshot=active_book.book_snapshot,
                execution_feed_seq=execution_feed_seq,
            )

        filled_qty = sum(level.quantity for level in levels)
        status = ExecutionStatus.FILLED if filled_qty == registered.intent.requested_qty else ExecutionStatus.PARTIAL
        return self._record_result(
            registered,
            executed_at,
            status,
            levels,
            touch,
            limit_price,
            decision_mid,
            None,
            execution_snapshot=active_book.book_snapshot,
            execution_feed_seq=execution_feed_seq,
        )

    def validate_result(self, result: ExecutionResult) -> None:
        """Reject externally supplied outcomes that do not match their registered intent."""
        if not isinstance(result, ExecutionResult):
            raise FoundationContractError("result must be an ExecutionResult")
        try:
            registered = self._registered[result.intent_id]
        except KeyError as exc:
            raise FoundationContractError("execution result references an unknown registered intent") from exc
        intent, context, model, book_snapshot = (
            registered.intent,
            registered.context,
            registered.execution_model,
            registered.book_snapshot,
        )
        if (
            result.run_id != intent.run_id
            or result.decision_id != intent.decision_id
            or result.hedge_pair != intent.hedge_pair
            or result.product != intent.product
            or result.side is not intent.side
            or result.requested_qty != intent.requested_qty
            or result.execution_model_ref != ExecutionModelRef(model.model_id, model.version)
            or result.participation_rate != float(model.participation_rate)
            or result.decision_feed_seq != context.feed_seq
            or result.decision_book_snapshot != book_snapshot
            or result.limit_price != _snap_price(intent.limit_price, float(self._instrument_specs[intent.product].tick))
        ):
            raise FoundationContractError("execution result does not match its registered intent/context/model")
        if result.executed_at < context.dec_ts:
            raise FoundationContractError("execution result executed_at must not precede its decision time")

    def _record_result(
        self,
        registered: _RegisteredIntent,
        executed_at: datetime,
        status: ExecutionStatus,
        levels: tuple[ExecutionLevel, ...],
        executable_touch: float | None,
        limit_price: float,
        decision_mid: float | None,
        disposition_reason: str | None,
        *,
        execution_snapshot: BookSnapshotRef | None = None,
        execution_feed_seq: int | None = None,
    ) -> ExecutionResult:
        self._execution_feed_seq += 1
        intent, context, model = registered.intent, registered.context, registered.execution_model
        execution_snapshot = execution_snapshot or registered.book_snapshot
        resolved_execution_feed_seq = (
            execution_feed_seq
            if execution_feed_seq is not None
            else max(context.feed_seq, execution_snapshot.feed_seq)
        )
        filled_qty = sum(level.quantity for level in levels)
        vwap = sum(level.price * level.quantity for level in levels) / filled_qty if filled_qty else None
        if vwap is None or decision_mid is None:
            cost_vs_decision_mid = None
        elif intent.side is OrderSide.BUY:
            cost_vs_decision_mid = vwap - decision_mid
        else:
            cost_vs_decision_mid = decision_mid - vwap
        result = ExecutionResult(
            f"{intent.run_id}:{intent.intent_id}:{self._execution_feed_seq}",
            intent.intent_id,
            intent.run_id,
            intent.decision_id,
            intent.hedge_pair,
            intent.product,
            intent.side,
            status,
            intent.requested_qty,
            filled_qty,
            intent.requested_qty - filled_qty,
            executed_at,
            ExecutionModelRef(model.model_id, model.version),
            float(model.participation_rate),
            context.feed_seq,
            resolved_execution_feed_seq,
            execution_snapshot,
            limit_price,
            levels,
            executable_touch,
            vwap,
            decision_mid,
            cost_vs_decision_mid,
            disposition_reason,
            registered.book_snapshot,
        )
        self.validate_result(result)
        self._results[intent.intent_id] = result
        return result


def _spec_map(specs: Mapping[str, InstrumentSpec] | Iterable[InstrumentSpec]) -> Mapping[str, InstrumentSpec]:
    items = specs.items() if isinstance(specs, Mapping) else ((spec.product, spec) for spec in specs)
    result: dict[str, InstrumentSpec] = {}
    for product, spec in items:
        if not isinstance(spec, InstrumentSpec) or product != spec.product:
            raise FoundationContractError("instrument specs must be keyed by their InstrumentSpec product")
        if product in result:
            raise FoundationContractError("instrument spec products must be unique")
        result[product] = spec
    if not result:
        raise FoundationContractError("at least one instrument spec is required")
    return MappingProxyType(result)


def _model_key(model_ref: ExecutionModelRef) -> tuple[str, str]:
    return model_ref.model_id, model_ref.version


def _model_map(models: Iterable[ExecutionModelConfig]) -> Mapping[tuple[str, str], ExecutionModelConfig]:
    result: dict[tuple[str, str], ExecutionModelConfig] = {}
    for model in models:
        if not isinstance(model, ExecutionModelConfig):
            raise FoundationContractError("execution_models must contain ExecutionModelConfig values")
        if model.allow_synthetic_depth_refresh:
            raise FoundationContractError("synthetic depth refresh is not supported by the Phase-3 execution service")
        key = model.model_id, model.version
        if key in result:
            raise FoundationContractError("execution model references must be unique")
        result[key] = model
    if not result:
        raise FoundationContractError("at least one execution model is required")
    return MappingProxyType(result)


__all__ = ["DepthBook", "DepthExecutionService", "DepthLevel"]
