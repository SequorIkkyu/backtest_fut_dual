"""Exchange-batch ingress acceptance tests."""

from __future__ import annotations

from datetime import timedelta

from common.foundation_contracts import FoundationContractError, HedgePairRef, IngressEvent, IngressKind
from common.ingress import CausalIngress
from common.tests.foundation.fixtures import BASE_TS


PAIR = HedgePairRef("batch-pair-v1", "Q", "H", "ratio-v1", "1.0.0")


def _event(
    event_id: str,
    product: str,
    kind: IngressKind,
    exchange_ms: int,
    recv_ms: int,
    source_seq: int,
    payload: dict,
    *,
    batch_id: str | None = None,
    batch_seq: int | None = None,
) -> IngressEvent:
    return IngressEvent(
        event_id,
        product,
        kind,
        BASE_TS + timedelta(milliseconds=exchange_ms),
        BASE_TS + timedelta(milliseconds=recv_ms),
        source_seq,
        payload,
        exchange_batch_id=batch_id,
        exchange_batch_seq=batch_seq,
    )


def _pair_batch(batch: str, sequence: int, exchange_ms: int, *, recv_q: int, recv_h: int):
    return (
        _event(f"{batch}-q", "Q", IngressKind.BOOK, exchange_ms, recv_q, 99, {"bids": [{"price": 100, "quantity": 2}], "asks": [{"price": 101, "quantity": 2}]}, batch_id=batch, batch_seq=sequence),
        _event(f"{batch}-h", "H", IngressKind.BOOK, exchange_ms, recv_h, 1, {"bids": [{"price": 90, "quantity": 2}], "asks": [{"price": 91, "quantity": 2}]}, batch_id=batch, batch_seq=sequence),
    )


def test_exchange_batch_is_atomic_and_independent_of_receive_and_source_order():
    quote, hedge = _pair_batch("batch-1", 7, 0, recv_q=9, recv_h=1)
    ingress = CausalIngress("batch-order-run", (quote, hedge), required_book_products=("Q", "H"))

    batch = ingress.next_batch()
    assert batch is not None
    assert batch.exchange_batch.batch_id == "batch-1"
    assert batch.exchange_batch.sequence == 0
    assert {event.event_id for event in batch.events} == {"batch-1-q", "batch-1-h"}
    context = ingress.decision_context("decision-1", PAIR)
    assert context.quoted_book.exchange_batch == context.hedge_book.exchange_batch == batch.exchange_batch
    assert context.dec_ts == BASE_TS
    assert context.input_ages_ms == {"quoted_book": 0.0, "hedge_book": 0.0}


def test_context_exposes_the_prior_aligned_batch_and_interval_identity():
    ingress = CausalIngress(
        "sequential-batch-run",
        (*_pair_batch("batch-1", 10, 0, recv_q=9, recv_h=1), *_pair_batch("batch-2", 11, 5, recv_q=12, recv_h=6)),
        required_book_products=("Q", "H"),
    )
    first = ingress.next_batch()
    assert first is not None
    first_context = ingress.decision_context("decision-1", PAIR)
    assert first_context.previous_quoted_book is None
    second = ingress.next_batch()
    assert second is not None
    context = ingress.decision_context("decision-2", PAIR, observed_fill_ids=("fill-1",))
    assert context.previous_quoted_book is not None
    assert context.previous_hedge_book is not None
    assert context.previous_quoted_book.exchange_batch == first.exchange_batch
    assert context.interval_id == "batch-1->batch-2"
    assert context.observed_fill_ids == ("fill-1",)
    assert context.hedge_book_view is not None and context.hedge_book_view.best_ask == 91.0
    assert context.previous_hedge_book_view is not None
    assert context.previous_hedge_book_view.snapshot == context.previous_hedge_book


def test_partial_pair_batch_cannot_form_a_dual_book_decision():
    ingress = CausalIngress("partial-batch-run", (_event("q", "Q", IngressKind.BOOK, 0, 1, 1, {"bid": 100}, batch_id="b", batch_seq=0),))
    ingress.next_batch()
    try:
        ingress.decision_context("decision", PAIR)
    except FoundationContractError as exc:
        assert "both hedge-pair books" in str(exc)
    else:
        raise AssertionError("a partial exchange batch must not create a pair decision")


def test_required_book_products_fail_closed_for_an_incomplete_batch():
    try:
        CausalIngress(
            "required-pair-run",
            (_event("q", "Q", IngressKind.BOOK, 0, 1, 1, {"bid": 100}, batch_id="b", batch_seq=0),),
            required_book_products=("Q", "H"),
        )
    except FoundationContractError as exc:
        assert "exactly the declared book products" in str(exc)
    else:
        raise AssertionError("production pair ingress must reject an incomplete exchange batch")


def test_same_timestamp_distinct_batches_require_explicit_sequence():
    first = _event("q-1", "Q", IngressKind.BOOK, 0, 1, 1, {"bid": 100}, batch_id="one")
    second = _event("q-2", "Q", IngressKind.BOOK, 0, 2, 2, {"bid": 101}, batch_id="two")
    try:
        CausalIngress("ambiguous-run", (first, second))
    except FoundationContractError as exc:
        assert "require unique explicit exchange_batch_seq" in str(exc)
    else:
        raise AssertionError("equal exchange timestamps cannot use receive/source ordering")


def test_exchange_batch_rejects_duplicate_product_snapshot():
    first = _event("q-1", "Q", IngressKind.BOOK, 0, 1, 1, {"bid": 100}, batch_id="one", batch_seq=0)
    second = _event("q-2", "Q", IngressKind.BOOK, 0, 2, 2, {"bid": 101}, batch_id="one", batch_seq=0)
    try:
        CausalIngress("duplicate-product-run", (first, second))
    except FoundationContractError as exc:
        assert "more than one book snapshot" in str(exc)
    else:
        raise AssertionError("an aligned exchange batch has at most one book per product")


def test_ingress_rejects_receive_timestamp_before_exchange_timestamp():
    try:
        _event("impossible", "Q", IngressKind.BOOK, 2, 1, 1, {"bid": 100})
    except FoundationContractError as exc:
        assert "recv_ts" in str(exc)
    else:
        raise AssertionError("source timestamps must remain physically non-impossible provenance")
