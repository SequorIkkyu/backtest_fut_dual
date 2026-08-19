"""Deterministic dual-book fixtures for the foundation acceptance suite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping

from common.foundation_contracts import (
    BookSnapshotRef,
    DecisionContext,
    ExecutionModelConfig,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
    SessionCalendar,
)


UTC = timezone.utc
BASE_TS = datetime(2025, 1, 2, 9, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class SyntheticDualBookFixture:
    quoted_spec: InstrumentSpec
    hedge_spec: InstrumentSpec
    hedge_pair: HedgePairRef
    quoted_book: BookSnapshotRef
    hedge_book: BookSnapshotRef
    execution_model: ExecutionModelConfig
    events: tuple[IngressEvent, ...]
    decision_context: DecisionContext
    depth_by_product: Mapping[str, tuple[tuple[float, int], ...]]
    signal_available_at: datetime
    action_arrival_at: datetime


def make_dual_book_fixture() -> SyntheticDualBookFixture:
    """Return independently timestamped books, signal, depth, and action arrival."""
    calendar = SessionCalendar("shanghai-futures-v1", "Asia/Shanghai")
    quoted = InstrumentSpec("ZN-main", 5.0, 5.0, calendar, "shfe-zn-v1", "zn-roll-v1")
    hedge = InstrumentSpec("ZN-next", 5.0, 5.0, calendar, "shfe-zn-v1", "zn-roll-v1")
    hedge_pair = HedgePairRef("zn-calendar-v1", quoted.product, hedge.product, "calendar-spread", "1.0.0")
    model = ExecutionModelConfig("depth-participation", "0.1.0", participation_rate=0.5)
    events = (
        IngressEvent(
            "quoted-book-1",
            quoted.product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=2),
            11,
            {"bid": 78000.0, "ask": 78005.0},
        ),
        IngressEvent(
            "hedge-book-1",
            hedge.product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=3),
            7,
            {"bid": 77980.0, "ask": 77985.0},
        ),
        IngressEvent(
            "signal-1",
            quoted.product,
            IngressKind.SIGNAL,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=4),
            3,
            {"signal_id": "inventory-estimator", "score": 0.25},
        ),
    )
    quoted_book = BookSnapshotRef(
        quoted.product,
        1,
        1,
        events[0].event_id,
        events[0].recv_ts,
        events[0].available_at,
        "quoted-snapshot-1",
        "sha256:quoted-1",
    )
    hedge_book = BookSnapshotRef(
        hedge.product,
        1,
        2,
        events[1].event_id,
        events[1].recv_ts,
        events[1].available_at,
        "hedge-snapshot-1",
        "sha256:hedge-1",
    )
    decision_at = BASE_TS + timedelta(milliseconds=5)
    context = DecisionContext(
        "fixture-run",
        "decision-1",
        decision_at,
        feed_seq=3,
        quoted_product=quoted.product,
        hedge_product=hedge.product,
        quoted_book=quoted_book,
        hedge_book=hedge_book,
        hedge_pair=hedge_pair,
        input_ages_ms={"quoted_book": 3.0, "hedge_book": 2.0, "signal": 1.0},
    )
    return SyntheticDualBookFixture(
        quoted,
        hedge,
        hedge_pair,
        quoted_book,
        hedge_book,
        model,
        events,
        context,
        MappingProxyType(
            {
                quoted.product: ((78000.0, 10), (77995.0, 20)),
                hedge.product: ((77985.0, 5), (77990.0, 15)),
            }
        ),
        signal_available_at=events[-1].recv_ts,
        action_arrival_at=decision_at + timedelta(milliseconds=2),
    )
