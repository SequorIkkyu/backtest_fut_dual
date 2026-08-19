"""Exchange-batch ingress for the maker-hedger foundation.

The supported replay boundary is an exchange-published snapshot batch, not an
arrival-ordered stream.  Rows in one batch are atomic: their source/receive
order is retained for provenance but cannot leak an intermediate pair state to
a policy.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from common.foundation_contracts import (
    BookSnapshotRef,
    CausalSignalSnapshot,
    DecisionContext,
    ExchangeBatchRef,
    FoundationContractError,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    PolicyBookView,
    SignalSnapshotRef,
)


def _canonical_value(value: Any) -> Any:
    """Return a JSON-safe immutable representation or fail closed."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FoundationContractError("ingress payload mapping keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical_value(item) for item in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise FoundationContractError(f"ingress payload value is not canonicalizable: {type(value).__name__}")


def _snapshot_hash(payload: Mapping[str, Any]) -> str:
    try:
        serialized = json.dumps(
            _canonical_value(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FoundationContractError("ingress payload must be canonical JSON data") from exc
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _freeze_snapshot(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Retain a recursively detached, read-only copy of ingress payload data."""
    value = _canonical_value(payload)

    def freeze(item: Any) -> Any:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(value)


@dataclass(frozen=True)
class IngressBatch:
    """One sealed exchange-published batch and its deterministic feed span."""

    exchange_batch: ExchangeBatchRef
    events: tuple[IngressEvent, ...]
    feed_seq_start: int
    feed_seq_end: int

    @property
    def exchange_ts(self) -> datetime:
        return self.exchange_batch.exchange_ts

    @property
    def available_at(self) -> datetime:
        """Compatibility alias; exchange time is the sole replay clock."""
        return self.exchange_batch.exchange_ts

    @property
    def has_book_events(self) -> bool:
        return any(event.kind is IngressKind.BOOK for event in self.events)


class CausalIngress:
    """Apply complete exchange batches and construct aligned decision contexts.

    The historical class name is retained at the public import boundary, but
    receive-time ordering and receive-time atomic bundles are deliberately not
    implemented.  An explicit batch ID/sequence is required whenever a source
    publishes distinct batches at one exchange timestamp; otherwise timestamp
    equality is the one batch identity.
    """

    def __init__(
        self,
        run_id: str,
        events: Iterable[IngressEvent],
        *,
        required_book_products: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise FoundationContractError("run_id must be a non-empty string")
        values = tuple(events)
        if any(not isinstance(event, IngressEvent) for event in values):
            raise FoundationContractError("events must contain only IngressEvent values")
        event_ids = [event.event_id for event in values]
        if len(event_ids) != len(set(event_ids)):
            raise FoundationContractError("ingress event_id values must be unique per run")
        if not isinstance(required_book_products, tuple) or any(
            not isinstance(product, str) or not product.strip() for product in required_book_products
        ):
            raise FoundationContractError("required_book_products must be a tuple of non-empty strings")
        if len(set(required_book_products)) != len(required_book_products):
            raise FoundationContractError("required_book_products must not contain duplicates")

        grouped: dict[str, list[IngressEvent]] = {}
        for event in values:
            grouped.setdefault(event.exchange_batch_key, []).append(event)
        groups = self._resolve_batches(grouped)
        expected = frozenset(required_book_products)
        for batch, batch_events in groups:
            book_events = tuple(event for event in batch_events if event.kind is IngressKind.BOOK)
            products = [event.product for event in book_events]
            if len(products) != len(set(products)):
                raise FoundationContractError(
                    f"exchange batch {batch.batch_id!r} has more than one book snapshot for a product"
                )
            if book_events and expected and frozenset(products) != expected:
                raise FoundationContractError(
                    f"exchange batch {batch.batch_id!r} must contain exactly the declared book products"
                )

        self.run_id = run_id
        self._batches = tuple(groups)
        self._batch_index = 0
        self._feed_seq = 0
        self._book_seq: dict[str, int] = {}
        self._books: dict[str, BookSnapshotRef] = {}
        self._signals: dict[str, SignalSnapshotRef] = {}
        self._book_payloads: dict[str, Mapping[str, Any]] = {}
        self._signal_payloads: dict[str, Mapping[str, Any]] = {}
        self._book_event_refs: dict[str, BookSnapshotRef] = {}
        self._signal_event_refs: dict[str, SignalSnapshotRef] = {}
        self._last_exchange_batch: ExchangeBatchRef | None = None
        self._last_batch_books: Mapping[str, BookSnapshotRef] = MappingProxyType({})
        self._prior_books: Mapping[str, BookSnapshotRef] = MappingProxyType({})

    @staticmethod
    def _resolve_batches(
        grouped: Mapping[str, list[IngressEvent]],
    ) -> tuple[tuple[ExchangeBatchRef, tuple[IngressEvent, ...]], ...]:
        provisional: list[tuple[str, datetime, int | None, tuple[IngressEvent, ...]]] = []
        for batch_id, values in grouped.items():
            exchange_times = {event.exchange_ts for event in values}
            if len(exchange_times) != 1:
                raise FoundationContractError("one exchange_batch_id cannot span more than one exchange timestamp")
            declared_sequences = {event.exchange_batch_seq for event in values if event.exchange_batch_seq is not None}
            if len(declared_sequences) > 1:
                raise FoundationContractError("all events in an explicit exchange batch must share exchange_batch_seq")
            provisional.append((batch_id, next(iter(exchange_times)), next(iter(declared_sequences), None), tuple(values)))

        same_time: dict[datetime, list[tuple[str, datetime, int | None, tuple[IngressEvent, ...]]]] = {}
        for item in provisional:
            same_time.setdefault(item[1], []).append(item)
        for exchange_ts, items in same_time.items():
            if len(items) <= 1:
                continue
            sequences = [item[2] for item in items]
            if any(sequence is None for sequence in sequences) or len(set(sequences)) != len(sequences):
                raise FoundationContractError(
                    "distinct exchange batches at one exchange timestamp require unique explicit exchange_batch_seq values"
                )

        # ``ExchangeBatchRef.sequence`` is the resolved replay sequence.  A
        # source sequence only disambiguates equal exchange timestamps; it is
        # not reused as a clock because signals may be published between book
        # batches without carrying a book-batch sequence themselves.
        ordered = sorted(
            provisional,
            key=lambda item: (item[1], -1 if item[2] is None else int(item[2]), item[0]),
        )
        resolved = [(ExchangeBatchRef(item[0], index, item[1]), item[3]) for index, item in enumerate(ordered)]

        return tuple(
            (
                batch,
                tuple(sorted(events, key=lambda event: (event.kind.value, event.product, event.event_id))),
            )
            for batch, events in resolved
        )

    @property
    def feed_seq(self) -> int:
        return self._feed_seq

    @property
    def last_available_at(self) -> datetime | None:
        """Deprecated compatibility alias for the latest exchange batch time."""
        return None if self._last_exchange_batch is None else self._last_exchange_batch.exchange_ts

    @property
    def last_exchange_batch(self) -> ExchangeBatchRef | None:
        return self._last_exchange_batch

    def next_batch(self) -> IngressBatch | None:
        """Apply the next complete exchange batch, or return ``None`` at EOF."""
        if self._batch_index >= len(self._batches):
            return None
        batch, events = self._batches[self._batch_index]
        self._batch_index += 1
        prior_books = dict(self._books)
        feed_seq_start = self._feed_seq + 1
        current_batch_books: dict[str, BookSnapshotRef] = {}
        for event in events:
            ref = self._apply(event, batch)
            if isinstance(ref, BookSnapshotRef):
                current_batch_books[ref.product] = ref
        self._prior_books = MappingProxyType(prior_books)
        self._last_batch_books = MappingProxyType(current_batch_books)
        self._last_exchange_batch = batch
        return IngressBatch(batch, events, feed_seq_start, self._feed_seq)

    def replay(self) -> Iterable[IngressBatch]:
        """Yield every complete exchange batch in declared sequence."""
        while (batch := self.next_batch()) is not None:
            yield batch

    def _apply(self, event: IngressEvent, batch: ExchangeBatchRef) -> BookSnapshotRef | SignalSnapshotRef:
        self._feed_seq += 1
        payload = _freeze_snapshot(event.payload)
        payload_hash = _snapshot_hash(event.payload)
        if event.kind is IngressKind.BOOK:
            book_seq = self._book_seq.get(event.product, 0) + 1
            self._book_seq[event.product] = book_seq
            snapshot_id = f"{event.event_id}:book:{book_seq}"
            snapshot = BookSnapshotRef(
                event.product,
                book_seq,
                self._feed_seq,
                event.event_id,
                event.recv_ts,
                event.available_at,
                snapshot_id,
                payload_hash,
                batch,
            )
            self._books[event.product] = snapshot
            self._book_payloads[snapshot.snapshot_id] = payload
            self._book_event_refs[event.event_id] = snapshot
            return snapshot

        signal_id = event.payload.get("signal_id", event.event_id)
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise FoundationContractError("signal ingress payload signal_id must be a non-empty string")
        snapshot_id = f"{event.event_id}:signal:{signal_id}"
        snapshot = SignalSnapshotRef(
            signal_id,
            event.product,
            self._feed_seq,
            event.event_id,
            event.available_at,
            snapshot_id,
            payload_hash,
            batch,
        )
        self._signals[signal_id] = snapshot
        self._signal_payloads[snapshot.snapshot_id] = payload
        self._signal_event_refs[event.event_id] = snapshot
        return snapshot

    def book_snapshot(self, ref: BookSnapshotRef) -> Mapping[str, Any]:
        if not isinstance(ref, BookSnapshotRef):
            raise FoundationContractError("ref must be a BookSnapshotRef")
        try:
            return self._book_payloads[ref.snapshot_id]
        except KeyError as exc:
            raise FoundationContractError("book snapshot is not retained by this ingress run") from exc

    def latest_book_ref(self, product: str) -> BookSnapshotRef:
        try:
            return self._books[product]
        except KeyError as exc:
            raise FoundationContractError("no book is available for product") from exc

    def book_ref_for_event(self, event_id: str) -> BookSnapshotRef:
        try:
            return self._book_event_refs[event_id]
        except KeyError as exc:
            raise FoundationContractError("event has not produced a retained book snapshot") from exc

    def signal_snapshot(self, ref: SignalSnapshotRef) -> Mapping[str, Any]:
        if not isinstance(ref, SignalSnapshotRef):
            raise FoundationContractError("ref must be a SignalSnapshotRef")
        try:
            return self._signal_payloads[ref.snapshot_id]
        except KeyError as exc:
            raise FoundationContractError("signal snapshot is not retained by this ingress run") from exc

    def signal_ref_for_event(self, event_id: str) -> SignalSnapshotRef:
        try:
            return self._signal_event_refs[event_id]
        except KeyError as exc:
            raise FoundationContractError("event has not produced a retained signal snapshot") from exc

    def available_signal_refs(self) -> tuple[SignalSnapshotRef, ...]:
        return tuple(
            sorted(
                self._signals.values(),
                key=lambda ref: (
                    -1 if ref.exchange_batch is None else ref.exchange_batch.sequence,
                    ref.feed_seq,
                    ref.signal_id,
                    ref.snapshot_id,
                ),
            )
        )

    def decision_context(
        self,
        decision_id: str,
        hedge_pair: HedgePairRef,
        *,
        consumed_signal_ids: tuple[str, ...] = (),
        dec_ts: datetime | None = None,
        observed_fill_ids: tuple[str, ...] = (),
    ) -> DecisionContext:
        """Return an aligned post-batch context; partial pair states fail closed."""
        if not isinstance(hedge_pair, HedgePairRef):
            raise FoundationContractError("hedge_pair must be a HedgePairRef")
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise FoundationContractError("decision_id must be a non-empty string")
        batch = self._last_exchange_batch
        if batch is None:
            raise FoundationContractError("cannot create a decision context before an exchange batch")
        if dec_ts is None:
            dec_ts = batch.exchange_ts
        if not isinstance(dec_ts, datetime) or dec_ts.tzinfo is None or dec_ts.utcoffset() is None:
            raise FoundationContractError("dec_ts must be a timezone-aware datetime")
        if dec_ts != batch.exchange_ts:
            raise FoundationContractError("decision timestamp must equal the sealed exchange batch timestamp")
        try:
            quoted_book = self._last_batch_books[hedge_pair.quoted_product]
            hedge_book = self._last_batch_books[hedge_pair.hedge_product]
        except KeyError as exc:
            raise FoundationContractError("both hedge-pair books must be present in the sealed exchange batch") from exc
        if not isinstance(consumed_signal_ids, tuple):
            raise FoundationContractError("consumed_signal_ids must be a tuple")
        try:
            signals = tuple(self._signals[signal_id] for signal_id in consumed_signal_ids)
        except KeyError as exc:
            raise FoundationContractError("a consumed signal is not yet available in this ingress run") from exc
        if not isinstance(observed_fill_ids, tuple):
            raise FoundationContractError("observed_fill_ids must be a tuple")
        try:
            previous_quoted = self._prior_books[hedge_pair.quoted_product]
            previous_hedge = self._prior_books[hedge_pair.hedge_product]
        except KeyError:
            previous_quoted = None
            previous_hedge = None
        if (previous_quoted is None) != (previous_hedge is None):
            raise FoundationContractError("previous pair snapshots are incomplete; cannot form a decision context")
        interval_id = (
            None
            if previous_quoted is None
            else f"{previous_quoted.exchange_batch.batch_id}->{batch.batch_id}"
        )
        signal_values = tuple(CausalSignalSnapshot(signal, self.signal_snapshot(signal)) for signal in signals)
        return DecisionContext(
            self.run_id,
            decision_id,
            dec_ts,
            self._feed_seq,
            hedge_pair.quoted_product,
            hedge_pair.hedge_product,
            quoted_book,
            hedge_book,
            hedge_pair,
            signals,
            {"quoted_book": 0.0, "hedge_book": 0.0, **{f"signal:{signal.signal_id}": 0.0 for signal in signals}},
            signal_values,
            batch,
            previous_quoted,
            previous_hedge,
            interval_id,
            observed_fill_ids,
            self._policy_book_view(quoted_book),
            self._policy_book_view(hedge_book),
            None if previous_quoted is None else self._policy_book_view(previous_quoted),
            None if previous_hedge is None else self._policy_book_view(previous_hedge),
        )

    def _policy_book_view(self, snapshot: BookSnapshotRef) -> PolicyBookView:
        """Expose a narrow immutable price view, never executable depth."""
        try:
            payload = self._book_payloads[snapshot.snapshot_id]
        except KeyError as exc:
            raise FoundationContractError("book snapshot is not retained by this ingress run") from exc
        return PolicyBookView(
            snapshot,
            self._best_policy_price(payload, "bids"),
            self._best_policy_price(payload, "asks"),
        )

    @staticmethod
    def _best_policy_price(payload: Mapping[str, Any], side: str) -> float | None:
        levels = payload.get(side)
        if levels is None:
            return None
        if not isinstance(levels, tuple):
            raise FoundationContractError("policy book view requires canonical bid/ask level tuples")
        if not levels:
            return None
        first = levels[0]
        if not isinstance(first, Mapping):
            raise FoundationContractError("policy book view level must be a mapping")
        try:
            price = float(first["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FoundationContractError("policy book view level must contain a numeric price") from exc
        if not math.isfinite(price) or price <= 0:
            raise FoundationContractError("policy book view price must be finite and positive")
        return price
