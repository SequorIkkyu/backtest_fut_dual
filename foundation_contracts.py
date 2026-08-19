"""Versioned, implementation-neutral vocabulary for the maker-hedger foundation.

This module is Phase 0 scaffolding. It declares the objects later phases will
use at the policy/foundation boundary, but does not schedule sessions, mutate
books, submit orders, calculate a hedge target, or emit files.

The ``MAKER -> quoted product`` and ``HEDGE -> hedge product`` bindings below
are the declared S0 maker-hedger scope: passive quoted-leg making with an
aggressive correlated hedge. They are not a universal multi-instrument trading
constraint; a future passive hedge-leg policy requires a deliberate contract
revision.

Compatibility policy
--------------------
``FOUNDATION_CONTRACT_VERSION`` identifies a published vocabulary revision.
Breaking vocabulary changes advance the 0.x minor version and require migration
evidence; additive corrections advance the patch version. Existing ``common``
engine APIs are unchanged by this module. Consumers must not treat these types
as a live engine API until the corresponding implementation gate is complete;
the types exist now so tests and later implementations share stable names and
validation rules.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FOUNDATION_CONTRACT_VERSION = "0.13.0"
TELEMETRY_SCHEMA_VERSION = "0.6.0"

PROVENANCE_REQUIRED_ARTIFACTS = (
    "market_data",
    "signal_data",
    "configuration",
    "code",
    "schema",
    "fee_profile",
    "instrument_roll_mapping",
    "execution_models",
)

S0_TELEMETRY_TABLES = (
    "decisions",
    "book_events",
    "book_snapshots",
    "orders",
    "fills",
    "hedge_executions",
    "trigger_evaluations",
    "signal_snapshots",
    "outcome_pnl",
    "inventory_series",
)

S0_DUAL_BOOK_IDENTITY_FIELDS = (
    "run_id",
    "pair_id",
    "quoted_product",
    "hedge_product",
    "hedge_mapping_id",
    "hedge_mapping_version",
)


class FoundationContractError(ValueError):
    """Raised when a Phase-0 contract object is internally inconsistent."""


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FoundationContractError(f"{field_name} must be a non-empty string")
    return value


def _require_aware_timestamp(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FoundationContractError(f"{field_name} must be a timezone-aware datetime")
    return value


def _require_positive_finite(value: float, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise FoundationContractError(f"{field_name} must be a finite positive number") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise FoundationContractError(f"{field_name} must be a finite positive number")
    return numeric


def _require_nonnegative_finite(value: float, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise FoundationContractError(f"{field_name} must be a finite non-negative number") from exc
    if not math.isfinite(numeric) or numeric < 0:
        raise FoundationContractError(f"{field_name} must be a finite non-negative number")
    return numeric


def _require_time(value: time, field_name: str) -> time:
    if not isinstance(value, time):
        raise FoundationContractError(f"{field_name} must be a datetime.time")
    if value.tzinfo is not None:
        raise FoundationContractError(f"{field_name} must be timezone-naive wall-clock time")
    return value


def _freeze_value(value: Any) -> Any:
    """Recursively freeze payload data that may cross the policy boundary."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FoundationContractError(f"{field_name} must be a mapping")
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


@dataclass(frozen=True)
class SessionWindow:
    """One inclusive wall-clock trading window; it may cross midnight."""

    name: str
    start: time
    end: time

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_time(self.start, "start")
        _require_time(self.end, "end")
        if self.start == self.end:
            raise FoundationContractError("session window start and end must differ")

    def contains(self, value: time) -> bool:
        """Return whether a wall-clock timestamp falls within this inclusive window."""
        _require_time(value, "value")
        if self.start < self.end:
            return self.start <= value <= self.end
        return value >= self.start or value <= self.end


@dataclass(frozen=True)
class SessionCalendar:
    """Immutable per-product trading calendar and declared trading-day lifecycle."""

    calendar_id: str
    timezone: str
    version: str = "0.1.0"
    windows: tuple[SessionWindow, ...] = ()
    trading_day_rollover: time = time(18, 0)
    eod_time: time | None = None
    holidays: frozenset[date] = frozenset()
    early_closes: Mapping[date, time] = field(default_factory=dict)
    missing_data_disposition: str = "reject"

    def __post_init__(self) -> None:
        _require_text(self.calendar_id, "calendar_id")
        _require_text(self.timezone, "timezone")
        _require_text(self.version, "version")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise FoundationContractError(f"timezone is not available: {self.timezone}") from exc
        if not isinstance(self.windows, tuple) or any(not isinstance(window, SessionWindow) for window in self.windows):
            raise FoundationContractError("windows must be a tuple of SessionWindow values")
        names = [window.name for window in self.windows]
        if len(names) != len(set(names)):
            raise FoundationContractError("session window names must be unique")
        _require_time(self.trading_day_rollover, "trading_day_rollover")
        if self.eod_time is not None:
            _require_time(self.eod_time, "eod_time")
        if not isinstance(self.holidays, frozenset) or any(not isinstance(day, date) for day in self.holidays):
            raise FoundationContractError("holidays must be a frozenset of dates")
        early_closes = _freeze_mapping(self.early_closes, "early_closes")
        for day, close_time in early_closes.items():
            if not isinstance(day, date):
                raise FoundationContractError("early_closes keys must be dates")
            _require_time(close_time, "early_closes value")
        if self.missing_data_disposition not in {"reject", "drop"}:
            raise FoundationContractError("missing_data_disposition must be 'reject' or 'drop'")
        object.__setattr__(self, "early_closes", early_closes)

    def _local_datetime(self, value: datetime) -> datetime:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if not isinstance(value, datetime):
            raise FoundationContractError("calendar timestamp must be a datetime")
        timezone = ZoneInfo(self.timezone)
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone)
        return value.astimezone(timezone)

    def trading_day_of(self, value: datetime) -> date:
        local = self._local_datetime(value)
        wall_time = local.timetz().replace(tzinfo=None)
        return local.date() + timedelta(days=1 if wall_time >= self.trading_day_rollover else 0)

    def eod_time_for(self, trading_day: date) -> time | None:
        if not isinstance(trading_day, date):
            raise FoundationContractError("trading_day must be a date")
        return self.early_closes.get(trading_day, self.eod_time)

    def eod_at(self, trading_day: date) -> datetime | None:
        close_time = self.eod_time_for(trading_day)
        if close_time is None:
            return None
        return datetime.combine(trading_day, close_time, tzinfo=ZoneInfo(self.timezone))

    def window_at(self, value: datetime) -> SessionWindow | None:
        local = self._local_datetime(value)
        trading_day = self.trading_day_of(local)
        if trading_day in self.holidays:
            return None
        close_time = self.eod_time_for(trading_day)
        wall_time = local.timetz().replace(tzinfo=None)
        if local.date() == trading_day and close_time is not None and wall_time > close_time:
            return None
        for window in self.windows:
            if window.contains(wall_time):
                return window
        return None

    def is_trading_time(self, value: datetime) -> bool:
        return self.window_at(value) is not None

    def window_end_at(self, trading_day: date, window: SessionWindow) -> datetime:
        if not isinstance(trading_day, date):
            raise FoundationContractError("trading_day must be a date")
        if not isinstance(window, SessionWindow):
            raise FoundationContractError("window must be a SessionWindow")
        # A non-crossing window wholly after the rollover belongs to the
        # previous calendar date of its trading day. Crossing-midnight windows
        # end on the trading-day date, as do ordinary daytime windows.
        end_date = (
            trading_day - timedelta(days=1)
            if window.start < window.end and window.start >= self.trading_day_rollover
            else trading_day
        )
        return datetime.combine(end_date, window.end, tzinfo=ZoneInfo(self.timezone))


@dataclass(frozen=True)
class InstrumentSpec:
    """Immutable identity/configuration reference for one executable product."""

    product: str
    tick: float
    multiplier: float
    calendar: SessionCalendar
    fee_model_id: str
    roll_mapping_id: str

    def __post_init__(self) -> None:
        _require_text(self.product, "product")
        _require_positive_finite(self.tick, "tick")
        _require_positive_finite(self.multiplier, "multiplier")
        if not isinstance(self.calendar, SessionCalendar):
            raise FoundationContractError("calendar must be a SessionCalendar")
        _require_text(self.fee_model_id, "fee_model_id")
        _require_text(self.roll_mapping_id, "roll_mapping_id")


@dataclass(frozen=True)
class HedgePairRef:
    """Immutable identity of the quoted/hedge relationship, not its policy target."""

    pair_id: str
    quoted_product: str
    hedge_product: str
    hedge_mapping_id: str
    hedge_mapping_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "pair_id",
            "quoted_product",
            "hedge_product",
            "hedge_mapping_id",
            "hedge_mapping_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.quoted_product == self.hedge_product:
            raise FoundationContractError("quoted_product and hedge_product must be distinct")


@dataclass(frozen=True)
class HedgeMappingSpec:
    """Policy-declared risk mapping for one versioned quoted/hedge pair.

    ``quoted_risk_weight`` and ``hedge_risk_weight`` express risk units per
    signed contract. The neutral hedge target is therefore
    ``-q * quoted_risk_weight / hedge_risk_weight`` and the residual exposure
    is ``q * quoted_risk_weight + h * hedge_risk_weight``.
    """

    hedge_pair: HedgePairRef
    quoted_risk_weight: float
    hedge_risk_weight: float
    quantity_tolerance: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        _require_positive_finite(self.quoted_risk_weight, "quoted_risk_weight")
        _require_positive_finite(self.hedge_risk_weight, "hedge_risk_weight")
        _require_nonnegative_finite(self.quantity_tolerance, "quantity_tolerance")

    def target_hedge_position(self, quoted_position: int) -> float:
        if not isinstance(quoted_position, int):
            raise FoundationContractError("quoted_position must be an integer")
        return -float(quoted_position) * float(self.quoted_risk_weight) / float(self.hedge_risk_weight)

    def pending_hedge_quantity(self, quoted_position: int, hedge_position: int) -> float:
        if not isinstance(hedge_position, int):
            raise FoundationContractError("hedge_position must be an integer")
        return self.target_hedge_position(quoted_position) - float(hedge_position)

    def residual_risk(self, quoted_position: int, hedge_position: int) -> float:
        if not isinstance(quoted_position, int) or not isinstance(hedge_position, int):
            raise FoundationContractError("positions must be integers")
        return float(quoted_position) * float(self.quoted_risk_weight) + float(hedge_position) * float(
            self.hedge_risk_weight
        )


@dataclass(frozen=True)
class ExchangeBatchRef:
    """One ordered exchange-published market-data batch.

    The exchange timestamp identifies the market state represented by the
    batch.  ``sequence`` resolves the exceptional case where an exchange
    publishes more than one batch at the same timestamp.  Receive timestamps
    remain source provenance only; they never order a policy-visible batch.
    """

    batch_id: str
    sequence: int
    exchange_ts: datetime

    def __post_init__(self) -> None:
        _require_text(self.batch_id, "batch_id")
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise FoundationContractError("exchange batch sequence must be a non-negative integer")
        _require_aware_timestamp(self.exchange_ts, "exchange batch exchange_ts")


@dataclass(frozen=True)
class BookSnapshotRef:
    """Immutable, causally-addressable book snapshot; it never contains depth."""

    product: str
    book_seq: int
    feed_seq: int
    event_id: str
    recv_ts: datetime
    available_at: datetime
    snapshot_id: str
    snapshot_hash: str
    exchange_batch: ExchangeBatchRef | None = None

    def __post_init__(self) -> None:
        _require_text(self.product, "product")
        if not isinstance(self.book_seq, int) or self.book_seq < 0:
            raise FoundationContractError("book_seq must be a non-negative integer")
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        _require_text(self.event_id, "event_id")
        _require_aware_timestamp(self.recv_ts, "recv_ts")
        _require_aware_timestamp(self.available_at, "available_at")
        if self.available_at < self.recv_ts:
            raise FoundationContractError("book available_at must not be earlier than recv_ts")
        _require_text(self.snapshot_id, "snapshot_id")
        _require_text(self.snapshot_hash, "snapshot_hash")
        if self.exchange_batch is not None and not isinstance(self.exchange_batch, ExchangeBatchRef):
            raise FoundationContractError("exchange_batch must be an ExchangeBatchRef or None")


@dataclass(frozen=True)
class PolicyBookView:
    """Immutable top-of-book price view for policy-owned price formation.

    It deliberately exposes prices but not mutable/consumable depth.  A view
    is bound to one retained snapshot and can therefore be cited by an order's
    policy-owned pricing reference.
    """

    snapshot: BookSnapshotRef
    best_bid: float | None
    best_ask: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, BookSnapshotRef):
            raise FoundationContractError("policy book view snapshot must be a BookSnapshotRef")
        for field_name in ("best_bid", "best_ask"):
            value = getattr(self, field_name)
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise FoundationContractError(f"policy book view {field_name} must be a finite positive price or None")
        if self.best_bid is not None and self.best_ask is not None and float(self.best_bid) >= float(self.best_ask):
            raise FoundationContractError("policy book view must not be crossed")


@dataclass(frozen=True)
class SignalSnapshotRef:
    """Immutable, causally-addressable signal snapshot consumed by a decision."""

    signal_id: str
    product: str
    feed_seq: int
    event_id: str
    available_at: datetime
    snapshot_id: str
    snapshot_hash: str
    exchange_batch: ExchangeBatchRef | None = None

    def __post_init__(self) -> None:
        for field_name in ("signal_id", "product", "event_id", "snapshot_id", "snapshot_hash"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        _require_aware_timestamp(self.available_at, "available_at")
        if self.exchange_batch is not None and not isinstance(self.exchange_batch, ExchangeBatchRef):
            raise FoundationContractError("exchange_batch must be an ExchangeBatchRef or None")


def _snapshot_visible_at(snapshot: BookSnapshotRef, occurred_at: datetime) -> bool:
    """Apply exchange-batch visibility, retaining legacy behavior for old refs."""
    if snapshot.exchange_batch is not None:
        return snapshot.exchange_batch.exchange_ts <= occurred_at
    return snapshot.available_at <= occurred_at


@dataclass(frozen=True)
class CausalSignalSnapshot:
    """Immutable signal value and identity bound to one causal decision input."""

    ref: SignalSnapshotRef
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SignalSnapshotRef):
            raise FoundationContractError("ref must be a SignalSnapshotRef")
        frozen_payload = _freeze_mapping(self.payload, "payload")
        declared_signal_id = frozen_payload.get("signal_id")
        if declared_signal_id is not None and declared_signal_id != self.ref.signal_id:
            raise FoundationContractError("signal payload signal_id must match its snapshot reference")
        object.__setattr__(self, "payload", frozen_payload)


@dataclass(frozen=True)
class ExecutionModelConfig:
    """Named, versioned aggressive-execution assumptions."""

    model_id: str
    version: str
    participation_rate: float
    allow_synthetic_depth_refresh: bool = False
    sparse_book_disposition: str = "no_liquidity"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.model_id, "model_id")
        _require_text(self.version, "version")
        rate = float(self.participation_rate)
        if not math.isfinite(rate) or not 0.0 <= rate <= 1.0:
            raise FoundationContractError("participation_rate must be within [0, 1]")
        if not isinstance(self.allow_synthetic_depth_refresh, bool):
            raise FoundationContractError("allow_synthetic_depth_refresh must be a bool")
        if self.sparse_book_disposition not in {"no_liquidity", "failed"}:
            raise FoundationContractError("sparse_book_disposition must be 'no_liquidity' or 'failed'")
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))


@dataclass(frozen=True)
class ExecutionModelRef:
    """Reference to an allowed execution model; ``None`` on an intent means run default."""

    model_id: str
    version: str

    def __post_init__(self) -> None:
        _require_text(self.model_id, "model_id")
        _require_text(self.version, "version")


class IngressKind(str, Enum):
    BOOK = "book"
    SIGNAL = "signal"


@dataclass(frozen=True)
class IngressEvent:
    """One source row belonging to an ordered exchange-published batch.

    ``recv_ts`` and ``source_seq`` are preserved for source audit only.  The
    supported foundation route groups and orders events by exchange batch.
    """

    event_id: str
    product: str
    kind: IngressKind
    exchange_ts: datetime
    recv_ts: datetime
    source_seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    atomic_bundle_id: str | None = None
    bundle_recv_ts: datetime | None = None
    exchange_batch_id: str | None = None
    exchange_batch_seq: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_text(self.product, "product")
        if not isinstance(self.kind, IngressKind):
            raise FoundationContractError("kind must be an IngressKind")
        _require_aware_timestamp(self.exchange_ts, "exchange_ts")
        _require_aware_timestamp(self.recv_ts, "recv_ts")
        if self.recv_ts < self.exchange_ts:
            raise FoundationContractError("recv_ts must not be earlier than exchange_ts")
        if not isinstance(self.source_seq, int) or self.source_seq < 0:
            raise FoundationContractError("source_seq must be a non-negative integer")
        if (self.atomic_bundle_id is None) != (self.bundle_recv_ts is None):
            raise FoundationContractError("atomic_bundle_id and bundle_recv_ts must be provided together")
        if self.atomic_bundle_id is not None:
            _require_text(self.atomic_bundle_id, "atomic_bundle_id")
            _require_aware_timestamp(self.bundle_recv_ts, "bundle_recv_ts")
            if self.bundle_recv_ts < self.recv_ts:
                raise FoundationContractError("bundle_recv_ts must not be earlier than recv_ts")
        if self.exchange_batch_id is not None:
            _require_text(self.exchange_batch_id, "exchange_batch_id")
        if self.exchange_batch_seq is not None and (
            not isinstance(self.exchange_batch_seq, int) or self.exchange_batch_seq < 0
        ):
            raise FoundationContractError("exchange_batch_seq must be a non-negative integer or None")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))

    @property
    def available_at(self) -> datetime:
        """Deprecated source-availability provenance; never a decision clock."""
        return self.bundle_recv_ts if self.bundle_recv_ts is not None else self.recv_ts

    @property
    def exchange_batch_key(self) -> str:
        """Return the explicit batch ID, or the unambiguous timestamp fallback."""
        return self.exchange_batch_id or f"exchange-ts:{self.exchange_ts.isoformat()}"


@dataclass(frozen=True)
class DecisionContext:
    """Immutable post-batch state handed to a policy adapter.

    A context represents one sealed exchange batch.  When a preceding batch is
    available, ``previous_*`` snapshots are the only valid price basis for an
    order triggered by interval fills observed in this batch.
    """

    run_id: str
    decision_id: str
    dec_ts: datetime
    feed_seq: int
    quoted_product: str
    hedge_product: str
    quoted_book: BookSnapshotRef
    hedge_book: BookSnapshotRef
    hedge_pair: HedgePairRef
    consumed_signals: tuple[SignalSnapshotRef, ...] = ()
    input_ages_ms: Mapping[str, float] = field(default_factory=dict)
    consumed_signal_values: tuple[CausalSignalSnapshot, ...] = ()
    exchange_batch: ExchangeBatchRef | None = None
    previous_quoted_book: BookSnapshotRef | None = None
    previous_hedge_book: BookSnapshotRef | None = None
    interval_id: str | None = None
    observed_fill_ids: tuple[str, ...] = ()
    quoted_book_view: PolicyBookView | None = None
    hedge_book_view: PolicyBookView | None = None
    previous_quoted_book_view: PolicyBookView | None = None
    previous_hedge_book_view: PolicyBookView | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "decision_id",
            "quoted_product",
            "hedge_product",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware_timestamp(self.dec_ts, "dec_ts")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.quoted_product != self.hedge_pair.quoted_product:
            raise FoundationContractError("quoted_product must match hedge_pair.quoted_product")
        if self.hedge_product != self.hedge_pair.hedge_product:
            raise FoundationContractError("hedge_product must match hedge_pair.hedge_product")
        if not isinstance(self.quoted_book, BookSnapshotRef):
            raise FoundationContractError("quoted_book must be a BookSnapshotRef")
        if not isinstance(self.hedge_book, BookSnapshotRef):
            raise FoundationContractError("hedge_book must be a BookSnapshotRef")
        if self.quoted_book.product != self.quoted_product:
            raise FoundationContractError("quoted_book product must match quoted_product")
        if self.hedge_book.product != self.hedge_product:
            raise FoundationContractError("hedge_book product must match hedge_product")
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        for book in (self.quoted_book, self.hedge_book):
            if book.feed_seq > self.feed_seq:
                raise FoundationContractError("book feed_seq must not exceed decision feed_seq")
            if self.exchange_batch is None and book.available_at > self.dec_ts:
                raise FoundationContractError("book available_at must not be later than dec_ts")
        if not isinstance(self.consumed_signals, tuple) or any(
            not isinstance(signal, SignalSnapshotRef) for signal in self.consumed_signals
        ):
            raise FoundationContractError("consumed_signals must be a tuple of SignalSnapshotRef values")
        for signal in self.consumed_signals:
            if signal.feed_seq > self.feed_seq:
                raise FoundationContractError("signal feed_seq must not exceed decision feed_seq")
            if self.exchange_batch is None and signal.available_at > self.dec_ts:
                raise FoundationContractError("signal available_at must not be later than dec_ts")
        ages = _freeze_mapping(self.input_ages_ms, "input_ages_ms")
        for key, value in ages.items():
            _require_text(key, "input_ages_ms key")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise FoundationContractError("input_ages_ms values must be finite and non-negative")
        object.__setattr__(self, "input_ages_ms", ages)
        if not isinstance(self.consumed_signal_values, tuple) or any(
            not isinstance(signal, CausalSignalSnapshot) for signal in self.consumed_signal_values
        ):
            raise FoundationContractError("consumed_signal_values must be a tuple of CausalSignalSnapshot values")
        if self.consumed_signal_values and tuple(signal.ref for signal in self.consumed_signal_values) != self.consumed_signals:
            raise FoundationContractError("consumed_signal_values must exactly bind consumed_signals")
        if self.exchange_batch is None:
            if any(value is not None for value in (self.previous_quoted_book, self.previous_hedge_book, self.interval_id)):
                raise FoundationContractError("previous-batch fields require an exchange_batch")
        else:
            if not isinstance(self.exchange_batch, ExchangeBatchRef):
                raise FoundationContractError("exchange_batch must be an ExchangeBatchRef or None")
            if self.dec_ts != self.exchange_batch.exchange_ts:
                raise FoundationContractError("exchange-batch decision dec_ts must equal exchange_batch.exchange_ts")
            for book in (self.quoted_book, self.hedge_book):
                if book.exchange_batch != self.exchange_batch:
                    raise FoundationContractError("decision books must belong to the declared exchange batch")
            previous = (self.previous_quoted_book, self.previous_hedge_book)
            if (previous[0] is None) != (previous[1] is None):
                raise FoundationContractError("previous quoted and hedge books must be supplied together")
            if previous[0] is None:
                if self.interval_id is not None:
                    raise FoundationContractError("the initial exchange batch has no prior interval")
            else:
                assert previous[0] is not None and previous[1] is not None
                if previous[0].product != self.quoted_product or previous[1].product != self.hedge_product:
                    raise FoundationContractError("previous books must match the decision products")
                if previous[0].exchange_batch is None or previous[1].exchange_batch is None:
                    raise FoundationContractError("previous books must carry exchange-batch identity")
                if previous[0].exchange_batch != previous[1].exchange_batch:
                    raise FoundationContractError("previous books must be aligned to one exchange batch")
                if previous[0].exchange_batch.sequence >= self.exchange_batch.sequence:
                    raise FoundationContractError("previous exchange batch must precede the decision batch")
                _require_text(self.interval_id, "interval_id")
            for signal in self.consumed_signals:
                if signal.exchange_batch is not None and signal.exchange_batch.sequence > self.exchange_batch.sequence:
                    raise FoundationContractError("signal batch cannot follow its decision batch")
        if not isinstance(self.observed_fill_ids, tuple) or any(
            not isinstance(fill_id, str) or not fill_id.strip() for fill_id in self.observed_fill_ids
        ):
            raise FoundationContractError("observed_fill_ids must be a tuple of non-empty strings")
        if len(set(self.observed_fill_ids)) != len(self.observed_fill_ids):
            raise FoundationContractError("observed_fill_ids must be unique")
        for field_name, expected_snapshot in (
            ("quoted_book_view", self.quoted_book),
            ("hedge_book_view", self.hedge_book),
            ("previous_quoted_book_view", self.previous_quoted_book),
            ("previous_hedge_book_view", self.previous_hedge_book),
        ):
            view = getattr(self, field_name)
            if view is not None and not isinstance(view, PolicyBookView):
                raise FoundationContractError(f"{field_name} must be a PolicyBookView or None")
            if view is not None and view.snapshot != expected_snapshot:
                raise FoundationContractError(f"{field_name} must bind its matching decision snapshot")

    def signal_value(self, ref: SignalSnapshotRef) -> CausalSignalSnapshot:
        """Return the value-bearing snapshot for one declared consumed signal."""
        if not isinstance(ref, SignalSnapshotRef):
            raise FoundationContractError("ref must be a SignalSnapshotRef")
        for signal in self.consumed_signal_values:
            if signal.ref == ref:
                return signal
        raise FoundationContractError("signal value is not declared in this decision context")


@dataclass(frozen=True)
class OrderPricingReference:
    """Policy-owned evidence for the snapshots used to derive an order price."""

    pricing_batch: ExchangeBatchRef
    pricing_snapshot_id: str
    basis: str
    trigger_fill_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pricing_batch, ExchangeBatchRef):
            raise FoundationContractError("pricing_batch must be an ExchangeBatchRef")
        _require_text(self.pricing_snapshot_id, "pricing_snapshot_id")
        if self.basis not in {"post_batch_snapshot_v1", "previous_batch_interval_fill_v1"}:
            raise FoundationContractError("unsupported order pricing basis")
        if self.trigger_fill_id is not None:
            _require_text(self.trigger_fill_id, "trigger_fill_id")
            if self.basis != "previous_batch_interval_fill_v1":
                raise FoundationContractError("fill-triggered pricing must use previous_batch_interval_fill_v1")
        elif self.basis == "previous_batch_interval_fill_v1":
            raise FoundationContractError("previous_batch_interval_fill_v1 requires trigger_fill_id")




class OrderRole(str, Enum):
    MAKER = "maker"
    HEDGE = "hedge"
    EOD = "eod"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderIntent:
    """Policy-declared order request; the foundation owns its eventual lifecycle."""

    intent_id: str
    run_id: str
    decision_id: str
    hedge_pair: HedgePairRef
    product: str
    role: OrderRole
    side: OrderSide
    requested_qty: int
    limit_price: float
    execution_model_ref: ExecutionModelRef | None = None
    strategy_metadata: Mapping[str, Any] = field(default_factory=dict)
    pricing_reference: OrderPricingReference | None = None

    def __post_init__(self) -> None:
        for field_name in ("intent_id", "run_id", "decision_id", "product"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(self.role, OrderRole):
            raise FoundationContractError("role must be an OrderRole")
        if not isinstance(self.side, OrderSide):
            raise FoundationContractError("side must be an OrderSide")
        if self.role is OrderRole.MAKER and self.product != self.hedge_pair.quoted_product:
            raise FoundationContractError("maker intent product must be the hedge pair's quoted product")
        if self.role is OrderRole.HEDGE and self.product != self.hedge_pair.hedge_product:
            raise FoundationContractError("hedge intent product must be the hedge pair's hedge product")
        if self.role is OrderRole.EOD and self.product not in (
            self.hedge_pair.quoted_product,
            self.hedge_pair.hedge_product,
        ):
            raise FoundationContractError("EOD intent product must belong to the hedge pair")
        if not isinstance(self.requested_qty, int) or self.requested_qty <= 0:
            raise FoundationContractError("requested_qty must be a positive integer")
        _require_positive_finite(self.limit_price, "limit_price")
        if self.execution_model_ref is not None and not isinstance(self.execution_model_ref, ExecutionModelRef):
            raise FoundationContractError("execution_model_ref must be an ExecutionModelRef or None for run default")
        object.__setattr__(self, "strategy_metadata", _freeze_mapping(self.strategy_metadata, "strategy_metadata"))
        if self.pricing_reference is not None and not isinstance(self.pricing_reference, OrderPricingReference):
            raise FoundationContractError("pricing_reference must be an OrderPricingReference or None")


@dataclass(frozen=True)
class MakerHedgeIntentBatch:
    """One policy response for the declared S0 passive-maker/aggressive-hedge scope.

    A batch is intentionally an immutable declaration, not an execution request.
    The versioned foundation API owns registration, capacity reservation, depth
    execution, lifecycle attachment, ledger effects, and telemetry.  Empty
    batches are valid deliberate no-action decisions.
    """

    maker_intent: OrderIntent | None = None
    hedge_intent: OrderIntent | None = None
    maker_capacity_envelope_id: str | None = None

    def __post_init__(self) -> None:
        if self.maker_intent is not None:
            if not isinstance(self.maker_intent, OrderIntent) or self.maker_intent.role is not OrderRole.MAKER:
                raise FoundationContractError("maker_intent must be a maker OrderIntent or None")
            _require_text(self.maker_capacity_envelope_id, "maker_capacity_envelope_id")
        elif self.maker_capacity_envelope_id is not None:
            raise FoundationContractError("maker_capacity_envelope_id requires maker_intent")
        if self.hedge_intent is not None:
            if not isinstance(self.hedge_intent, OrderIntent) or self.hedge_intent.role is not OrderRole.HEDGE:
                raise FoundationContractError("hedge_intent must be a hedge OrderIntent or None")
        if self.maker_intent is not None and self.hedge_intent is not None:
            maker, hedge = self.maker_intent, self.hedge_intent
            if maker.run_id != hedge.run_id or maker.decision_id != hedge.decision_id or maker.hedge_pair != hedge.hedge_pair:
                raise FoundationContractError("maker and hedge intents in a batch must share run, decision, and hedge pair")
            if maker.intent_id == hedge.intent_id:
                raise FoundationContractError("maker and hedge intents in a batch must have distinct intent IDs")


class ExecutionStatus(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    NO_LIQUIDITY = "no_liquidity"
    STALE = "stale"
    DEADLINE = "deadline"
    FAILED = "failed"


@dataclass(frozen=True)
class ExecutionLevel:
    price: float
    quantity: int

    def __post_init__(self) -> None:
        _require_positive_finite(self.price, "price")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise FoundationContractError("quantity must be a positive integer")


@dataclass(frozen=True)
class ExecutionResult:
    """Immutable aggressive-execution outcome from a registered intent."""

    execution_id: str
    intent_id: str
    run_id: str
    decision_id: str
    hedge_pair: HedgePairRef
    product: str
    side: OrderSide
    status: ExecutionStatus
    requested_qty: int
    filled_qty: int
    residual_qty: int
    executed_at: datetime
    execution_model_ref: ExecutionModelRef
    participation_rate: float
    decision_feed_seq: int
    execution_feed_seq: int
    book_snapshot: BookSnapshotRef
    limit_price: float
    levels: tuple[ExecutionLevel, ...] = ()
    executable_touch: float | None = None
    vwap: float | None = None
    decision_mid: float | None = None
    cost_vs_decision_mid: float | None = None
    disposition_reason: str | None = None
    decision_book_snapshot: BookSnapshotRef | None = None

    def __post_init__(self) -> None:
        for field_name in ("execution_id", "intent_id", "run_id", "decision_id", "product"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("execution product must belong to the hedge pair")
        if not isinstance(self.side, OrderSide):
            raise FoundationContractError("side must be an OrderSide")
        if not isinstance(self.status, ExecutionStatus):
            raise FoundationContractError("status must be an ExecutionStatus")
        if not isinstance(self.requested_qty, int) or self.requested_qty <= 0:
            raise FoundationContractError("requested_qty must be a positive integer")
        if not isinstance(self.filled_qty, int) or not 0 <= self.filled_qty <= self.requested_qty:
            raise FoundationContractError("filled_qty must be within [0, requested_qty]")
        if self.residual_qty != self.requested_qty - self.filled_qty:
            raise FoundationContractError("residual_qty must equal requested_qty - filled_qty")
        _require_aware_timestamp(self.executed_at, "executed_at")
        if not isinstance(self.execution_model_ref, ExecutionModelRef):
            raise FoundationContractError("execution_model_ref must be an ExecutionModelRef")
        rate = float(self.participation_rate)
        if not math.isfinite(rate) or not 0.0 <= rate <= 1.0:
            raise FoundationContractError("participation_rate must be within [0, 1]")
        for field_name in ("decision_feed_seq", "execution_feed_seq"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise FoundationContractError(f"{field_name} must be a non-negative integer")
        if not isinstance(self.book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if self.book_snapshot.product != self.product:
            raise FoundationContractError("book_snapshot product must match execution product")
        if self.execution_feed_seq < self.decision_feed_seq:
            raise FoundationContractError("execution_feed_seq must not precede decision_feed_seq")
        if self.book_snapshot.feed_seq > self.execution_feed_seq:
            raise FoundationContractError("execution book snapshot feed_seq must not exceed execution_feed_seq")
        if not _snapshot_visible_at(self.book_snapshot, self.executed_at):
            raise FoundationContractError("execution book snapshot is not visible at executed_at")
        if self.decision_book_snapshot is None:
            object.__setattr__(self, "decision_book_snapshot", self.book_snapshot)
        if not isinstance(self.decision_book_snapshot, BookSnapshotRef):
            raise FoundationContractError("decision_book_snapshot must be a BookSnapshotRef")
        if self.decision_book_snapshot.product != self.product:
            raise FoundationContractError("decision book snapshot product must match execution product")
        if self.decision_book_snapshot.feed_seq > self.decision_feed_seq:
            raise FoundationContractError("decision book snapshot feed_seq must not exceed decision_feed_seq")
        _require_positive_finite(self.limit_price, "limit_price")
        if not isinstance(self.levels, tuple) or any(not isinstance(level, ExecutionLevel) for level in self.levels):
            raise FoundationContractError("levels must be a tuple of ExecutionLevel values")
        if sum(level.quantity for level in self.levels) != self.filled_qty:
            raise FoundationContractError("execution-level quantity must equal filled_qty")
        for field_name in ("executable_touch", "vwap", "decision_mid"):
            value = getattr(self, field_name)
            if value is not None:
                _require_positive_finite(value, field_name)
        if self.cost_vs_decision_mid is not None and not math.isfinite(float(self.cost_vs_decision_mid)):
            raise FoundationContractError("cost_vs_decision_mid must be finite when supplied")
        if self.filled_qty and (self.executable_touch is None or self.vwap is None):
            raise FoundationContractError("filled executions require executable_touch and vwap")
        if self.status is ExecutionStatus.FILLED:
            if self.filled_qty != self.requested_qty or self.residual_qty != 0:
                raise FoundationContractError("filled status requires all requested quantity and no residual")
        elif self.status is ExecutionStatus.PARTIAL:
            if not 0 < self.filled_qty < self.requested_qty:
                raise FoundationContractError("partial status requires a strict partial fill")
        elif self.filled_qty != 0 or self.residual_qty != self.requested_qty or self.levels:
            raise FoundationContractError("zero-fill terminal status requires no fills and full residual")
        if self.filled_qty == 0 and self.vwap is not None:
            raise FoundationContractError("zero-fill execution must not report vwap")
        if self.disposition_reason is not None:
            _require_text(self.disposition_reason, "disposition_reason")


@dataclass(frozen=True)
class PassiveTrade:
    """One causally replayed aggressor trade eligible to advance maker queues."""

    trade_id: str
    run_id: str
    hedge_pair: HedgePairRef
    product: str
    taker_side: OrderSide
    trade_ts: datetime
    feed_seq: int
    book_snapshot: BookSnapshotRef
    price: float
    quantity: int
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("trade_id", "run_id", "product"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("trade product must belong to the hedge pair")
        if not isinstance(self.taker_side, OrderSide):
            raise FoundationContractError("taker_side must be an OrderSide")
        _require_aware_timestamp(self.trade_ts, "trade_ts")
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        if not isinstance(self.book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if self.book_snapshot.product != self.product:
            raise FoundationContractError("trade book snapshot product must match trade product")
        if self.book_snapshot.feed_seq > self.feed_seq or not _snapshot_visible_at(self.book_snapshot, self.trade_ts):
            raise FoundationContractError("trade book snapshot must be visible at trade_ts")
        _require_positive_finite(self.price, "price")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise FoundationContractError("trade quantity must be a positive integer")
        if self.source_event_id is not None:
            _require_text(self.source_event_id, "source_event_id")

    @property
    def source_event_key(self) -> str:
        """Return the run-wide source event identity for deduplication.

        Older callers that have not yet supplied a transport event ID remain
        uniquely scoped to the retained book snapshot. Production ingress must
        provide the event ID explicitly.
        """
        return self.book_snapshot.snapshot_id if self.source_event_id is None else self.source_event_id

    @property
    def trade_reference(self) -> str:
        """Return the immutable, source-event-qualified identity of this trade."""
        return f"{self.source_event_key}:{self.trade_id}"


@dataclass(frozen=True)
class PassiveFillEvidence:
    """Matcher-derived maker fill with queue and causal book evidence.

    ``_authority_token`` is deliberately opaque.  It is bound by the
    foundation-owned matcher and prevents a public caller from presenting a
    hand-constructed object as a verified passive fill.
    """

    fill_id: str
    trade_id: str
    trade_source_event_id: str
    trade_quantity: int
    intent_id: str
    run_id: str
    decision_id: str
    hedge_pair: HedgePairRef
    product: str
    side: OrderSide
    fill_ts: datetime
    feed_seq: int
    book_snapshot: BookSnapshotRef
    fill_price: float
    fill_qty: int
    cumulative_fill_qty: int
    queue_ahead_submit: int
    queue_ahead_fill: int
    fee_rebate: float
    _authority_token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "fill_id",
            "trade_id",
            "trade_source_event_id",
            "intent_id",
            "run_id",
            "decision_id",
            "product",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product != self.hedge_pair.quoted_product:
            raise FoundationContractError("a passive maker fill must use the quoted product")
        if not isinstance(self.side, OrderSide):
            raise FoundationContractError("side must be an OrderSide")
        _require_aware_timestamp(self.fill_ts, "fill_ts")
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        if not isinstance(self.book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if self.book_snapshot.product != self.product:
            raise FoundationContractError("passive fill book snapshot product must match fill product")
        if self.book_snapshot.feed_seq > self.feed_seq or not _snapshot_visible_at(self.book_snapshot, self.fill_ts):
            raise FoundationContractError("passive fill book snapshot must be visible at fill_ts")
        _require_positive_finite(self.fill_price, "fill_price")
        if not isinstance(self.fill_qty, int) or self.fill_qty <= 0:
            raise FoundationContractError("fill_qty must be a positive integer")
        if not isinstance(self.trade_quantity, int) or self.trade_quantity <= 0:
            raise FoundationContractError("trade_quantity must be a positive integer")
        if self.fill_qty > self.trade_quantity:
            raise FoundationContractError("fill_qty must not exceed trade_quantity")
        if not isinstance(self.cumulative_fill_qty, int) or self.cumulative_fill_qty < self.fill_qty:
            raise FoundationContractError("cumulative_fill_qty must be an integer no smaller than fill_qty")
        for field_name in ("queue_ahead_submit", "queue_ahead_fill"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise FoundationContractError(f"{field_name} must be a non-negative integer")
        if not math.isfinite(float(self.fee_rebate)):
            raise FoundationContractError("fee_rebate must be finite")

    @property
    def trade_reference(self) -> str:
        """Return the durable source-event-qualified identity of the trade."""
        return f"{self.trade_source_event_id}:{self.trade_id}"


@dataclass(frozen=True)
class SnapshotIntervalPriceBucket:
    """One model-derived valid-tick quantity bucket from a raw snapshot interval."""

    price: float
    quantity: int

    def __post_init__(self) -> None:
        _require_positive_finite(self.price, "price")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise FoundationContractError("snapshot interval bucket quantity must be a positive integer")


@dataclass(frozen=True)
class SnapshotInterval:
    """Causally available, model-derived queue-consumption interval.

    This is deliberately not an observed trade.  It captures the immutable raw
    file/row identity, a declared snapshot-proxy model, and quantity-conserving
    valid-tick buckets derived from cumulative volume and turnover.
    """

    interval_id: str
    raw_file_id: str
    raw_file_hash: str
    raw_row_ordinal: int
    run_id: str
    hedge_pair: HedgePairRef
    product: str
    interval_ts: datetime
    feed_seq: int
    book_snapshot: BookSnapshotRef
    model_version: str
    price_reach_rule: str
    quantity: int
    buckets: tuple[SnapshotIntervalPriceBucket, ...]
    availability_convention: str = "max_exchange_timestamp_v1"

    def __post_init__(self) -> None:
        for field_name in (
            "interval_id",
            "raw_file_id",
            "raw_file_hash",
            "run_id",
            "product",
            "model_version",
            "price_reach_rule",
            "availability_convention",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not self.raw_file_hash.startswith("sha256:") or len(self.raw_file_hash) != 71:
            raise FoundationContractError("raw_file_hash must be a sha256 digest")
        if not isinstance(self.raw_row_ordinal, int) or self.raw_row_ordinal < 0:
            raise FoundationContractError("raw_row_ordinal must be a non-negative integer")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product != self.hedge_pair.quoted_product:
            raise FoundationContractError("snapshot interval must use the quoted product")
        _require_aware_timestamp(self.interval_ts, "interval_ts")
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        if not isinstance(self.book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if self.book_snapshot.product != self.product:
            raise FoundationContractError("snapshot interval book snapshot product must match product")
        if self.book_snapshot.feed_seq > self.feed_seq or not _snapshot_visible_at(self.book_snapshot, self.interval_ts):
            raise FoundationContractError("snapshot interval book snapshot must be visible at interval_ts")
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            raise FoundationContractError("snapshot interval quantity must be a positive integer")
        if not isinstance(self.buckets, tuple) or not self.buckets or any(
            not isinstance(bucket, SnapshotIntervalPriceBucket) for bucket in self.buckets
        ):
            raise FoundationContractError("snapshot interval buckets must be a non-empty tuple of SnapshotIntervalPriceBucket")
        if sum(bucket.quantity for bucket in self.buckets) != self.quantity:
            raise FoundationContractError("snapshot interval bucket quantities must conserve interval quantity")


@dataclass(frozen=True)
class SnapshotIntervalQueueProxyEvidence:
    """Matcher-issued fill evidence for a snapshot-interval queue proxy."""

    fill_id: str
    interval_id: str
    raw_file_id: str
    raw_file_hash: str
    raw_row_ordinal: int
    model_version: str
    price_reach_rule: str
    availability_convention: str
    interval_quantity: int
    bucket_index: int
    bucket_price: float
    bucket_quantity: int
    intent_id: str
    run_id: str
    decision_id: str
    hedge_pair: HedgePairRef
    product: str
    side: OrderSide
    fill_ts: datetime
    feed_seq: int
    book_snapshot: BookSnapshotRef
    fill_price: float
    fill_qty: int
    cumulative_fill_qty: int
    queue_ahead_submit: int
    queue_ahead_fill: int
    fee_rebate: float
    _authority_token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "fill_id",
            "interval_id",
            "raw_file_id",
            "raw_file_hash",
            "model_version",
            "price_reach_rule",
            "availability_convention",
            "intent_id",
            "run_id",
            "decision_id",
            "product",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not self.raw_file_hash.startswith("sha256:") or len(self.raw_file_hash) != 71:
            raise FoundationContractError("raw_file_hash must be a sha256 digest")
        if not isinstance(self.raw_row_ordinal, int) or self.raw_row_ordinal < 0:
            raise FoundationContractError("raw_row_ordinal must be a non-negative integer")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product != self.hedge_pair.quoted_product:
            raise FoundationContractError("a snapshot proxy fill must use the quoted product")
        if not isinstance(self.side, OrderSide):
            raise FoundationContractError("side must be an OrderSide")
        _require_aware_timestamp(self.fill_ts, "fill_ts")
        if not isinstance(self.feed_seq, int) or self.feed_seq < 0:
            raise FoundationContractError("feed_seq must be a non-negative integer")
        if not isinstance(self.book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if self.book_snapshot.product != self.product:
            raise FoundationContractError("snapshot proxy fill book snapshot product must match fill product")
        if self.book_snapshot.feed_seq > self.feed_seq or not _snapshot_visible_at(self.book_snapshot, self.fill_ts):
            raise FoundationContractError("snapshot proxy fill book snapshot must be visible at fill_ts")
        if not isinstance(self.interval_quantity, int) or self.interval_quantity <= 0:
            raise FoundationContractError("interval_quantity must be a positive integer")
        if not isinstance(self.bucket_index, int) or self.bucket_index < 0:
            raise FoundationContractError("bucket_index must be a non-negative integer")
        _require_positive_finite(self.bucket_price, "bucket_price")
        if not isinstance(self.bucket_quantity, int) or self.bucket_quantity <= 0:
            raise FoundationContractError("bucket_quantity must be a positive integer")
        _require_positive_finite(self.fill_price, "fill_price")
        if not isinstance(self.fill_qty, int) or self.fill_qty <= 0 or self.fill_qty > self.bucket_quantity:
            raise FoundationContractError("fill_qty must be positive and no larger than bucket_quantity")
        if not isinstance(self.cumulative_fill_qty, int) or self.cumulative_fill_qty < self.fill_qty:
            raise FoundationContractError("cumulative_fill_qty must be an integer no smaller than fill_qty")
        for field_name in ("queue_ahead_submit", "queue_ahead_fill"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise FoundationContractError(f"{field_name} must be a non-negative integer")
        if not math.isfinite(float(self.fee_rebate)):
            raise FoundationContractError("fee_rebate must be finite")

    @property
    def interval_reference(self) -> str:
        """Return the durable raw-file-qualified interval identity."""
        return f"{self.raw_file_hash}:{self.interval_id}"


@dataclass(frozen=True)
class MakerQueueEvidence:
    """Immutable queue proxy captured when a production maker order arrives."""

    intent_id: str
    run_id: str
    decision_id: str
    hedge_pair: HedgePairRef
    product: str
    arrived_at: datetime
    book_snapshot: BookSnapshotRef
    queue_ahead_submit: int
    estimator_version: str = "displayed_back_of_queue_v1"

    def __post_init__(self) -> None:
        for field_name in ("intent_id", "run_id", "decision_id", "product", "estimator_version"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("queue evidence product must belong to hedge_pair")
        _require_aware_timestamp(self.arrived_at, "arrived_at")
        if not isinstance(self.book_snapshot, BookSnapshotRef):
            raise FoundationContractError("book_snapshot must be a BookSnapshotRef")
        if self.book_snapshot.product != self.product:
            raise FoundationContractError("queue evidence book_snapshot product must match product")
        if not _snapshot_visible_at(self.book_snapshot, self.arrived_at):
            raise FoundationContractError("queue evidence book_snapshot must be visible at arrival")
        if not isinstance(self.queue_ahead_submit, int) or self.queue_ahead_submit < 0:
            raise FoundationContractError("queue_ahead_submit must be a non-negative integer")


class ReservationAction(str, Enum):
    RESERVE = "reserve"
    RELEASE = "release"


@dataclass(frozen=True)
class CapacityEnvelope:
    """Policy-declared worst-case live-order capacity for one pair/product."""

    envelope_id: str
    hedge_pair: HedgePairRef
    product: str
    max_reserved_qty: int

    def __post_init__(self) -> None:
        _require_text(self.envelope_id, "envelope_id")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("capacity envelope product must belong to the hedge pair")
        if not isinstance(self.max_reserved_qty, int) or self.max_reserved_qty <= 0:
            raise FoundationContractError("max_reserved_qty must be a positive integer")


@dataclass(frozen=True)
class CapacityReservationEvent:
    reservation_id: str
    run_id: str
    decision_id: str
    intent_id: str
    hedge_pair: HedgePairRef
    product: str
    envelope_id: str
    action: ReservationAction
    amount: float
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("reservation_id", "run_id", "decision_id", "intent_id", "product"):
            _require_text(getattr(self, field_name), field_name)
        _require_text(self.envelope_id, "envelope_id")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("reservation product must belong to the hedge pair")
        if not isinstance(self.action, ReservationAction):
            raise FoundationContractError("action must be a ReservationAction")
        _require_positive_finite(self.amount, "amount")
        _require_aware_timestamp(self.occurred_at, "occurred_at")


class IntentLifecycleState(str, Enum):
    SUBMITTED = "submitted"
    ARRIVED = "arrived"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    STALE = "stale"
    DEADLINE = "deadline"
    FAILED = "failed"


_TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        IntentLifecycleState.FILLED,
        IntentLifecycleState.CANCELLED,
        IntentLifecycleState.EXPIRED,
        IntentLifecycleState.REJECTED,
        IntentLifecycleState.STALE,
        IntentLifecycleState.DEADLINE,
        IntentLifecycleState.FAILED,
    }
)


@dataclass(frozen=True)
class IntentLifecycleEvent:
    """Immutable state transition emitted only by the intent lifecycle service."""

    event_id: str
    run_id: str
    decision_id: str
    intent_id: str
    hedge_pair: HedgePairRef
    product: str
    state: IntentLifecycleState
    occurred_at: datetime
    execution_model_ref: ExecutionModelRef
    filled_qty: int
    residual_qty: int
    execution_id: str | None = None
    disposition_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "run_id", "decision_id", "intent_id", "product"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if self.product not in (self.hedge_pair.quoted_product, self.hedge_pair.hedge_product):
            raise FoundationContractError("lifecycle product must belong to the hedge pair")
        if not isinstance(self.state, IntentLifecycleState):
            raise FoundationContractError("state must be an IntentLifecycleState")
        _require_aware_timestamp(self.occurred_at, "occurred_at")
        if not isinstance(self.execution_model_ref, ExecutionModelRef):
            raise FoundationContractError("execution_model_ref must be an ExecutionModelRef")
        for field_name in ("filled_qty", "residual_qty"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise FoundationContractError(f"{field_name} must be a non-negative integer")
        if self.execution_id is not None:
            _require_text(self.execution_id, "execution_id")
        if self.disposition_reason is not None:
            _require_text(self.disposition_reason, "disposition_reason")
        if self.state in _TERMINAL_LIFECYCLE_STATES and self.state is not IntentLifecycleState.FILLED:
            if self.disposition_reason is None:
                raise FoundationContractError("terminal non-fill lifecycle state requires a disposition_reason")


class LedgerLeg(str, Enum):
    QUOTED = "quoted"
    HEDGE = "hedge"


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    run_id: str
    decision_id: str
    source_event_id: str
    hedge_pair: HedgePairRef
    leg: LedgerLeg
    product: str
    position_delta: int
    occurred_at: datetime
    attributes: Mapping[str, Any] = field(default_factory=dict)
    fee: float = 0.0
    rebate: float = 0.0

    def __post_init__(self) -> None:
        for field_name in ("event_id", "run_id", "decision_id", "source_event_id", "product"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(self.leg, LedgerLeg):
            raise FoundationContractError("leg must be a LedgerLeg")
        expected_product = (
            self.hedge_pair.quoted_product if self.leg is LedgerLeg.QUOTED else self.hedge_pair.hedge_product
        )
        if self.product != expected_product:
            raise FoundationContractError("ledger product must match its hedge-pair leg")
        if not isinstance(self.position_delta, int):
            raise FoundationContractError("position_delta must be an integer")
        _require_aware_timestamp(self.occurred_at, "occurred_at")
        _require_nonnegative_finite(self.fee, "fee")
        _require_nonnegative_finite(self.rebate, "rebate")
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes, "attributes"))


@dataclass(frozen=True)
class DualLegLedgerState:
    """Immutable reconstructed dual-leg inventory, exposure, and fill-cost state."""

    run_id: str
    hedge_pair: HedgePairRef
    hedge_mapping: HedgeMappingSpec
    quoted_position: int
    hedge_position: int
    pending_hedge_quantity: float
    residual_risk: float
    total_fees: float
    total_rebates: float
    ledger_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(self.hedge_mapping, HedgeMappingSpec) or self.hedge_mapping.hedge_pair != self.hedge_pair:
            raise FoundationContractError("hedge_mapping must bind the ledger hedge pair")
        if not isinstance(self.quoted_position, int) or not isinstance(self.hedge_position, int):
            raise FoundationContractError("ledger positions must be integers")
        expected_pending = self.hedge_mapping.pending_hedge_quantity(self.quoted_position, self.hedge_position)
        expected_risk = self.hedge_mapping.residual_risk(self.quoted_position, self.hedge_position)
        if not math.isclose(float(self.pending_hedge_quantity), expected_pending, rel_tol=0.0, abs_tol=1e-12):
            raise FoundationContractError("pending_hedge_quantity must match the declared hedge mapping")
        if not math.isclose(float(self.residual_risk), expected_risk, rel_tol=0.0, abs_tol=1e-12):
            raise FoundationContractError("residual_risk must match the declared hedge mapping")
        _require_nonnegative_finite(self.total_fees, "total_fees")
        _require_nonnegative_finite(self.total_rebates, "total_rebates")
        if not isinstance(self.ledger_event_ids, tuple):
            raise FoundationContractError("ledger_event_ids must be a tuple")
        for event_id in self.ledger_event_ids:
            _require_text(event_id, "ledger_event_ids value")


class EodDisposition(str, Enum):
    FLAT = "flat"
    INCOMPLETE_LIQUIDITY = "incomplete_liquidity"


@dataclass(frozen=True)
class EodCloseRequest:
    """Declared dual-book close-out plan; it never implies a synthetic touch fill."""

    eod_id: str
    context: DecisionContext
    limit_prices: Mapping[str, float]
    execution_model_ref: ExecutionModelRef | None = None

    def __post_init__(self) -> None:
        _require_text(self.eod_id, "eod_id")
        if not isinstance(self.context, DecisionContext):
            raise FoundationContractError("context must be a DecisionContext")
        prices = _freeze_mapping(self.limit_prices, "limit_prices")
        expected_products = {self.context.quoted_product, self.context.hedge_product}
        if set(prices) != expected_products:
            raise FoundationContractError("limit_prices must declare exactly the context quoted and hedge products")
        for product, price in prices.items():
            _require_text(product, "limit_prices key")
            _require_positive_finite(price, "limit_prices value")
        if self.execution_model_ref is not None and not isinstance(self.execution_model_ref, ExecutionModelRef):
            raise FoundationContractError("execution_model_ref must be an ExecutionModelRef or None for run default")
        object.__setattr__(self, "limit_prices", prices)


@dataclass(frozen=True)
class EodCompletion:
    """Reconstructible EOD outcome, including any residual dual-leg risk."""

    eod_id: str
    run_id: str
    decision_id: str
    hedge_pair: HedgePairRef
    completed_at: datetime
    disposition: EodDisposition
    cancelled_intent_ids: tuple[str, ...]
    execution_ids: tuple[str, ...]
    residual_quoted_position: int
    residual_hedge_position: int
    residual_risk: float

    def __post_init__(self) -> None:
        for field_name in ("eod_id", "run_id", "decision_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        _require_aware_timestamp(self.completed_at, "completed_at")
        if not isinstance(self.disposition, EodDisposition):
            raise FoundationContractError("disposition must be an EodDisposition")
        for field_name in ("cancelled_intent_ids", "execution_ids"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple):
                raise FoundationContractError(f"{field_name} must be a tuple")
            for value in values:
                _require_text(value, f"{field_name} value")
        if not isinstance(self.residual_quoted_position, int) or not isinstance(self.residual_hedge_position, int):
            raise FoundationContractError("EOD residual positions must be integers")
        if not math.isfinite(float(self.residual_risk)):
            raise FoundationContractError("residual_risk must be finite")
        if self.disposition is EodDisposition.FLAT and (
            self.residual_quoted_position != 0 or self.residual_hedge_position != 0
        ):
            raise FoundationContractError("flat EOD disposition requires zero residual positions")
        if self.disposition is EodDisposition.INCOMPLETE_LIQUIDITY and (
            self.residual_quoted_position == 0 and self.residual_hedge_position == 0
        ):
            raise FoundationContractError("incomplete-liquidity EOD disposition requires residual position")


@dataclass(frozen=True)
class PnlPriceObservation:
    """Priced, immutable valuation input for exactly one ledger fill effect."""

    ledger_event_id: str
    fill_price: float
    decision_reference_price: float

    def __post_init__(self) -> None:
        _require_text(self.ledger_event_id, "ledger_event_id")
        _require_positive_finite(self.fill_price, "fill_price")
        _require_positive_finite(self.decision_reference_price, "decision_reference_price")


@dataclass(frozen=True)
class PnlAccountingView:
    """One independently calculated total used to reconcile a canonical waterfall."""

    view_id: str
    total_pnl: float

    def __post_init__(self) -> None:
        _require_text(self.view_id, "view_id")
        if not math.isfinite(float(self.total_pnl)):
            raise FoundationContractError("total_pnl must be finite")


@dataclass(frozen=True)
class ApprovedEvidenceAuthority:
    """One deployment-approved authority permitted to authenticate economic artifacts.

    The authentication key belongs to the trusted replay deployment, not a
    policy, PnL scalar, market-data fixture, or ``ProductionReplayConfig``.
    Deployments install these values in their authority registry before a
    replay adapter is constructed. Only its identifier is retained in run
    provenance; the key itself is never emitted as an artifact.
    """

    authority_id: str
    key_id: str
    authentication_key: bytes

    def __post_init__(self) -> None:
        _require_text(self.authority_id, "authority_id")
        _require_text(self.key_id, "key_id")
        if not isinstance(self.authentication_key, bytes) or len(self.authentication_key) < 16:
            raise FoundationContractError("evidence authority authentication_key must contain at least 16 bytes")

    def sign(self, canonical_payload: bytes) -> str:
        """Return the supported detached signature for canonical external evidence."""
        if not isinstance(canonical_payload, bytes) or not canonical_payload:
            raise FoundationContractError("evidence signature payload must be non-empty immutable bytes")
        return "hmac-sha256:" + hmac.new(self.authentication_key, canonical_payload, hashlib.sha256).hexdigest()

    def verifies(self, canonical_payload: bytes, signature: str) -> bool:
        if not isinstance(signature, str) or not signature.startswith("hmac-sha256:"):
            return False
        try:
            expected = self.sign(canonical_payload)
        except FoundationContractError:
            return False
        return hmac.compare_digest(expected, signature)

    def as_provenance(self) -> Mapping[str, str]:
        """Return the non-secret authority selector retained with the trial."""
        return {
            "authority_id": self.authority_id,
            "key_id": self.key_id,
            "algorithm": "hmac-sha256",
        }


@dataclass(frozen=True)
class PnlViewEvidence:
    """Versioned source evidence for an independently calculated PnL view.

    The evidence is deliberately distinct from ``PnlAccountingView``: the
    latter is the scalar consumed by attribution, while this object declares
    its independently reproducible source and is retained in run provenance.
    """

    evidence_id: str
    view_id: str
    total_pnl: float
    methodology: str
    methodology_version: str
    source_artifact_id: str
    calculated_at: datetime
    source_artifact: bytes
    source_artifact_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "view_id", "methodology", "methodology_version", "source_artifact_id"):
            _require_text(getattr(self, field_name), field_name)
        if not math.isfinite(float(self.total_pnl)):
            raise FoundationContractError("PnlViewEvidence.total_pnl must be finite")
        _require_aware_timestamp(self.calculated_at, "PnlViewEvidence.calculated_at")
        if not isinstance(self.source_artifact, bytes) or not self.source_artifact:
            raise FoundationContractError("PnlViewEvidence.source_artifact must be non-empty immutable bytes")
        object.__setattr__(
            self,
            "source_artifact_hash",
            f"sha256:{hashlib.sha256(self.source_artifact).hexdigest()}",
        )

    def as_provenance(self) -> Mapping[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "view_id": self.view_id,
            "total_pnl": self.total_pnl,
            "methodology": self.methodology,
            "methodology_version": self.methodology_version,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_hash": self.source_artifact_hash,
            "calculated_at": self.calculated_at,
        }


@dataclass(frozen=True)
class ValuationMarkEvidence:
    """Versioned and time-qualified provenance for one product valuation mark."""

    evidence_id: str
    product: str
    mark: float
    methodology: str
    methodology_version: str
    source_artifact_id: str
    observed_at: datetime
    source_artifact: bytes
    source_artifact_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "product", "methodology", "methodology_version", "source_artifact_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_positive_finite(self.mark, "ValuationMarkEvidence.mark")
        _require_aware_timestamp(self.observed_at, "ValuationMarkEvidence.observed_at")
        if not isinstance(self.source_artifact, bytes) or not self.source_artifact:
            raise FoundationContractError("ValuationMarkEvidence.source_artifact must be non-empty immutable bytes")
        object.__setattr__(
            self,
            "source_artifact_hash",
            f"sha256:{hashlib.sha256(self.source_artifact).hexdigest()}",
        )

    def as_provenance(self) -> Mapping[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "product": self.product,
            "mark": self.mark,
            "methodology": self.methodology,
            "methodology_version": self.methodology_version,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_hash": self.source_artifact_hash,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class PnlAttributionEffect:
    """One non-overlapping PnL allocation for a ledger fill effect."""

    ledger_event_id: str
    leg: LedgerLeg
    maker_capture: float
    quoted_leg_price_pnl: float
    hedge_leg_price_pnl: float
    hedge_execution_shortfall: float
    fees: float
    rebates: float
    net_pnl: float

    def __post_init__(self) -> None:
        _require_text(self.ledger_event_id, "ledger_event_id")
        if not isinstance(self.leg, LedgerLeg):
            raise FoundationContractError("leg must be a LedgerLeg")
        for field_name in (
            "maker_capture",
            "quoted_leg_price_pnl",
            "hedge_leg_price_pnl",
            "hedge_execution_shortfall",
            "fees",
            "rebates",
            "net_pnl",
        ):
            if not math.isfinite(float(getattr(self, field_name))):
                raise FoundationContractError(f"{field_name} must be finite")
        _require_nonnegative_finite(self.fees, "fees")
        _require_nonnegative_finite(self.rebates, "rebates")
        tolerance = 1e-12
        if self.leg is LedgerLeg.QUOTED and (
            abs(float(self.hedge_leg_price_pnl)) > tolerance or abs(float(self.hedge_execution_shortfall)) > tolerance
        ):
            raise FoundationContractError("quoted attribution effects cannot contain hedge PnL categories")
        if self.leg is LedgerLeg.HEDGE and (
            abs(float(self.maker_capture)) > tolerance or abs(float(self.quoted_leg_price_pnl)) > tolerance
        ):
            raise FoundationContractError("hedge attribution effects cannot contain quoted PnL categories")
        expected_net = (
            float(self.maker_capture)
            + float(self.quoted_leg_price_pnl)
            + float(self.hedge_leg_price_pnl)
            - float(self.hedge_execution_shortfall)
            - float(self.fees)
            + float(self.rebates)
        )
        if not math.isclose(float(self.net_pnl), expected_net, rel_tol=0.0, abs_tol=tolerance):
            raise FoundationContractError("net_pnl must equal the non-overlapping attribution categories")


@dataclass(frozen=True)
class PnlAttributionResult:
    """Canonical waterfall and reconciliation verdict for one dual-leg trial run."""

    attribution_id: str
    run_id: str
    hedge_pair: HedgePairRef
    effects: tuple[PnlAttributionEffect, ...]
    maker_capture: float
    quoted_leg_price_pnl: float
    hedge_leg_price_pnl: float
    hedge_execution_shortfall: float
    fees: float
    rebates: float
    waterfall_total: float
    residual_basis_pnl: float
    accounting_total_pnl: float
    cycle_total_pnl: float
    reconciliation_residual: float
    cycle_reconciliation_residual: float
    telemetry_reconciled: bool
    eod_reconciled: bool
    reconciliation_failures: tuple[str, ...]
    tolerance: float
    economics_eligible: bool

    def __post_init__(self) -> None:
        _require_text(self.attribution_id, "attribution_id")
        _require_text(self.run_id, "run_id")
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(self.effects, tuple):
            raise FoundationContractError("effects must be a tuple")
        if any(not isinstance(effect, PnlAttributionEffect) for effect in self.effects):
            raise FoundationContractError("effects must contain PnlAttributionEffect values")
        event_ids = tuple(effect.ledger_event_id for effect in self.effects)
        if len(set(event_ids)) != len(event_ids):
            raise FoundationContractError("each ledger event may have only one PnL attribution effect")
        for field_name in (
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
        ):
            if not math.isfinite(float(getattr(self, field_name))):
                raise FoundationContractError(f"{field_name} must be finite")
        _require_nonnegative_finite(self.fees, "fees")
        _require_nonnegative_finite(self.rebates, "rebates")
        _require_nonnegative_finite(self.tolerance, "tolerance")
        for field_name in ("telemetry_reconciled", "eod_reconciled", "economics_eligible"):
            if not isinstance(getattr(self, field_name), bool):
                raise FoundationContractError(f"{field_name} must be boolean")
        if not isinstance(self.reconciliation_failures, tuple):
            raise FoundationContractError("reconciliation_failures must be a tuple")
        for value in self.reconciliation_failures:
            _require_text(value, "reconciliation_failures value")
        aggregate = {
            "maker_capture": sum(effect.maker_capture for effect in self.effects),
            "quoted_leg_price_pnl": sum(effect.quoted_leg_price_pnl for effect in self.effects),
            "hedge_leg_price_pnl": sum(effect.hedge_leg_price_pnl for effect in self.effects),
            "hedge_execution_shortfall": sum(effect.hedge_execution_shortfall for effect in self.effects),
            "fees": sum(effect.fees for effect in self.effects),
            "rebates": sum(effect.rebates for effect in self.effects),
        }
        for field_name, expected in aggregate.items():
            if not math.isclose(float(getattr(self, field_name)), expected, rel_tol=0.0, abs_tol=1e-12):
                raise FoundationContractError(f"{field_name} must equal the sum of attribution effects")
        expected_total = (
            float(self.maker_capture)
            + float(self.quoted_leg_price_pnl)
            + float(self.hedge_leg_price_pnl)
            - float(self.hedge_execution_shortfall)
            - float(self.fees)
            + float(self.rebates)
        )
        if not math.isclose(float(self.waterfall_total), expected_total, rel_tol=0.0, abs_tol=1e-12):
            raise FoundationContractError("waterfall_total must equal its non-overlapping PnL categories")
        if not math.isclose(
            float(self.residual_basis_pnl),
            float(self.quoted_leg_price_pnl) + float(self.hedge_leg_price_pnl),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise FoundationContractError("residual_basis_pnl must be a derived combined-leg price attribution")
        if not math.isclose(
            float(self.reconciliation_residual),
            float(self.accounting_total_pnl) - float(self.waterfall_total),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise FoundationContractError("reconciliation_residual must reconcile accounting total to the waterfall")
        if not math.isclose(
            float(self.cycle_reconciliation_residual),
            float(self.cycle_total_pnl) - float(self.waterfall_total),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise FoundationContractError("cycle_reconciliation_residual must reconcile cycle total to the waterfall")
        expected_eligible = (
            not self.reconciliation_failures
            and self.telemetry_reconciled
            and self.eod_reconciled
            and abs(float(self.reconciliation_residual)) <= float(self.tolerance)
            and abs(float(self.cycle_reconciliation_residual)) <= float(self.tolerance)
        )
        if self.economics_eligible != expected_eligible:
            raise FoundationContractError("economics_eligible must match the reconciliation verdict")


class InvariantSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class InvariantResult:
    invariant_id: str
    passed: bool
    severity: InvariantSeverity
    message: str
    related_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.invariant_id, "invariant_id")
        if not isinstance(self.passed, bool):
            raise FoundationContractError("passed must be boolean")
        if not isinstance(self.severity, InvariantSeverity):
            raise FoundationContractError("severity must be an InvariantSeverity")
        _require_text(self.message, "message")
        if not isinstance(self.related_ids, tuple):
            raise FoundationContractError("related_ids must be a tuple")
        for related_id in self.related_ids:
            _require_text(related_id, "related_ids value")


@dataclass(frozen=True)
class TelemetrySchema:
    version: str = TELEMETRY_SCHEMA_VERSION
    tables: tuple[str, ...] = S0_TELEMETRY_TABLES
    dual_book_identity_fields: tuple[str, ...] = S0_DUAL_BOOK_IDENTITY_FIELDS

    def __post_init__(self) -> None:
        _require_text(self.version, "version")
        if not isinstance(self.tables, tuple) or set(self.tables) != set(S0_TELEMETRY_TABLES):
            raise FoundationContractError("tables must contain exactly the S0 telemetry table names")
        if (
            not isinstance(self.dual_book_identity_fields, tuple)
            or self.dual_book_identity_fields != S0_DUAL_BOOK_IDENTITY_FIELDS
        ):
            raise FoundationContractError("dual_book_identity_fields must contain the canonical dual-book identities")


@dataclass(frozen=True)
class TrialDeclaration:
    """Immutable development-trial declaration required before economics eligibility."""

    trial_id: str
    development_window: str
    calibration_window: str
    holdout_window: str
    candidate_freeze_decision: str
    policy_version: str
    hedge_pair: HedgePairRef
    execution_models: tuple[ExecutionModelRef, ...]
    data_cleaning_transforms: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "trial_id",
            "development_window",
            "calibration_window",
            "holdout_window",
            "candidate_freeze_decision",
            "policy_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(self.execution_models, tuple) or not self.execution_models:
            raise FoundationContractError("execution_models must be a non-empty tuple")
        for model in self.execution_models:
            if not isinstance(model, ExecutionModelRef):
                raise FoundationContractError("execution_models must contain ExecutionModelRef values")
        if not isinstance(self.data_cleaning_transforms, tuple):
            raise FoundationContractError("data_cleaning_transforms must be a tuple")
        for transform in self.data_cleaning_transforms:
            _require_text(transform, "data_cleaning_transforms value")


@dataclass(frozen=True)
class RunProvenance:
    """Content-hashed immutable artifact set retained for one trial run."""

    run_id: str
    trial: TrialDeclaration
    schema_version: str
    artifact_hashes: Mapping[str, str]
    provenance_hash: str

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        if not isinstance(self.trial, TrialDeclaration):
            raise FoundationContractError("trial must be a TrialDeclaration")
        _require_text(self.schema_version, "schema_version")
        hashes = _freeze_mapping(self.artifact_hashes, "artifact_hashes")
        missing = set(PROVENANCE_REQUIRED_ARTIFACTS) - set(hashes)
        if missing:
            raise FoundationContractError("artifact_hashes must contain every required provenance artifact")
        for artifact_name, digest in hashes.items():
            _require_text(artifact_name, "artifact_hashes key")
            _require_text(digest, "artifact_hashes value")
            if not digest.startswith("sha256:"):
                raise FoundationContractError("artifact_hashes values must be sha256 digests")
        _require_text(self.provenance_hash, "provenance_hash")
        if not self.provenance_hash.startswith("sha256:"):
            raise FoundationContractError("provenance_hash must be a sha256 digest")
        object.__setattr__(self, "artifact_hashes", hashes)


@dataclass(frozen=True)
class TelemetryRunResult:
    """Final run eligibility; any error-severity failed invariant is ineligible."""

    run_id: str
    eligible: bool
    provenance: RunProvenance
    invariants: tuple[InvariantResult, ...]

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        if not isinstance(self.eligible, bool):
            raise FoundationContractError("eligible must be boolean")
        if not isinstance(self.provenance, RunProvenance) or self.provenance.run_id != self.run_id:
            raise FoundationContractError("provenance must belong to the telemetry run")
        if not isinstance(self.invariants, tuple) or not self.invariants:
            raise FoundationContractError("invariants must be a non-empty tuple of InvariantResult values")
        if any(not isinstance(result, InvariantResult) for result in self.invariants):
            raise FoundationContractError("invariants must contain InvariantResult values")
        expected_eligible = not any(
            not result.passed and result.severity is InvariantSeverity.ERROR for result in self.invariants
        )
        if self.eligible != expected_eligible:
            raise FoundationContractError("eligible must match error-severity invariant results")


__all__ = [
    "ApprovedEvidenceAuthority",
    "BookSnapshotRef",
    "CapacityEnvelope",
    "CapacityReservationEvent",
    "CausalSignalSnapshot",
    "DecisionContext",
    "DualLegLedgerState",
    "EodCloseRequest",
    "EodCompletion",
    "EodDisposition",
    "ExchangeBatchRef",
    "ExecutionLevel",
    "ExecutionModelConfig",
    "ExecutionModelRef",
    "ExecutionResult",
    "ExecutionStatus",
    "FOUNDATION_CONTRACT_VERSION",
    "FoundationContractError",
    "HedgePairRef",
    "HedgeMappingSpec",
    "IngressEvent",
    "IngressKind",
    "InstrumentSpec",
    "IntentLifecycleEvent",
    "IntentLifecycleState",
    "InvariantResult",
    "InvariantSeverity",
    "LedgerEvent",
    "LedgerLeg",
    "MakerQueueEvidence",
    "MakerHedgeIntentBatch",
    "OrderIntent",
    "OrderPricingReference",
    "PolicyBookView",
    "OrderRole",
    "OrderSide",
    "PassiveFillEvidence",
    "PassiveTrade",
    "PnlAccountingView",
    "PnlAttributionEffect",
    "PnlAttributionResult",
    "PnlPriceObservation",
    "PnlViewEvidence",
    "PROVENANCE_REQUIRED_ARTIFACTS",
    "ReservationAction",
    "S0_TELEMETRY_TABLES",
    "S0_DUAL_BOOK_IDENTITY_FIELDS",
    "SessionCalendar",
    "SessionWindow",
    "SignalSnapshotRef",
    "SnapshotInterval",
    "SnapshotIntervalPriceBucket",
    "SnapshotIntervalQueueProxyEvidence",
    "TELEMETRY_SCHEMA_VERSION",
    "TelemetrySchema",
    "TelemetryRunResult",
    "TrialDeclaration",
    "ValuationMarkEvidence",
    "RunProvenance",
]
