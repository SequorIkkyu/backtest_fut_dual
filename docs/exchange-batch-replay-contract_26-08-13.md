# Exchange-batch replay contract

## Purpose

The supported `ProductionReplayAdapter` route models an exchange-published
dual-book snapshot batch. It does not infer market-event order from local
receive timestamps. `recv_ts` and source sequence remain retained provenance,
but neither is a policy-decision clock.

This contract is intentionally for the existing strict dual-contract
maker-hedger foundation. It does not add a generic single-contract route.

## Accepted market-data shape

Each `BOOK` event belongs to one exchange batch:

- `exchange_batch_id` identifies the published batch. If omitted, the exchange
  timestamp is the batch identity.
- `exchange_batch_seq` disambiguates multiple batches with the same exchange
  timestamp. Such batches require distinct explicit sequences.
- A batch cannot contain two snapshots for one product.
- Production replay requires every non-empty book batch to contain exactly the
  quoted and hedge products. A partial pair update fails closed.

The strict loader records the resolved batch ID and sequence in
`ValidatedMarketData`; raw adapters also include them in their replay hash.
If validation drops one member of a published batch, the resulting partial
batch is intentionally not replayable as production evidence.

## Deterministic sequential semantics

For `B(i-1) -> B(i)`, replay has four phases:

1. Execute actions scheduled strictly before `B(i)`.
2. Atomically retain both books of `B(i)` and simulate passive fills for the
   interval ending at `B(i)`.
3. Give the policy one immutable post-batch `DecisionContext`.
4. Submit/arrive actions due at `B(i)` only after matching that interval.

Consequently, an order produced by a fill observed in `B(i)` cannot be matched
inside the same interval. The context provides `exchange_batch`, aligned
`previous_quoted_book` / `previous_hedge_book`, `interval_id`, and the
matcher-issued `observed_fill_ids` so policy code can make this distinction
without inspecting mutable engine state.

`quoted_book_view`, `hedge_book_view`, and their `previous_*` counterparts
provide an immutable best-bid/best-ask view bound to those same snapshots. They
let policy code form a predecessor-batch price without receiving mutable or
consumable execution depth.

## Order-price evidence

When exchange-batch pricing enforcement is enabled (as it is in production
replay), a policy-owned `OrderPricingReference` states the snapshot basis:

- `post_batch_snapshot_v1` cites the current aligned book for an ordinary
  post-batch order.
- `previous_batch_interval_fill_v1` cites the same-product snapshot in
  `B(i-1)` and one ID from `observed_fill_ids`. This is mandatory for an order
  triggered by a fill observed in `B(i)`.

The foundation verifies these references; it does not manufacture a price,
trigger, or reservation value for the policy.

## Receive-time removal

`recv_ts` is still stored for transport diagnostics and source provenance, and
the loader still rejects an impossible `recv_ts < exchange_ts`. It has no
effect on batch order, context availability, passive matching, or aggressive
execution.

`StressScenario.market_data_delay_ms` and `signal_delay_ms` are therefore
unsupported and rejected. Scheduler-owned submission and arrival delays remain
valid because they model policy/order timing after a sealed exchange batch.
Participation, fee, basis, volatility, and opening-session stress remain
separate declared controls.

## Evidence and verification

Canonical telemetry uses `exchange_batch_id` and `exchange_batch_seq` as the
resolved batch ID and monotone replay ordinal in book events, snapshots, and
decisions. `book_events.source_exchange_batch_seq` retains the optional
source-declared same-timestamp disambiguator separately. Research validation
uses exchange event time for snapshot-proxy causality, not local receive time.
Calendar EOD retains the latest aligned market context but records the terminal
EOD action at the calendar timestamp.

The full acceptance suite covers atomic order independence, incomplete-batch
rejection, same-timestamp sequencing, previous-batch context, fill-trigger
pricing constraints, production replay, raw snapshots, and the foundation
taker example.
