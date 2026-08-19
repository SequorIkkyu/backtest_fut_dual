# Pairs hedge design — existing-order implementation

**Status:** common-market support implemented; pairs strategy migration pending  
**Scope:** replacement for, and removal of, the interval-ticket /
`SnapshotLiquidity` design introduced by common commit `d7b3ad5`.

## Decision

Pairs hedges use the existing `Market` execution and order mechanisms.  They
do not use a second ticket lifecycle, raw-snapshot cache, immutable wrapper,
or liquidity-cursor class.

When a first-leg passive fill requires a hedge, the strategy places an
**aggressive limit order in that same aligned interval**, provided that the
hedge leg has an already-supplied `curr` snapshot.  It then explicitly invokes a
targeted immediate sweep for that exact order ID; it does not wait for the
normal `step` / `match` cycle.  The sweep walks the available opposite-side
book up to the limit.  A fill is priced at the actual available book prices (one VWAP
fill event when more than one level is used), **not at the submitted limit
price**.

If that immediate attempt is partial, the *same order* is switched to a normal
non-aggressive limit with its reduced remaining quantity.  It may then fill
during subsequent market-data intervals under the ordinary limit-order
lifecycle.  It is not automatically cancelled and reposted every interval.

This deliberately replaces the current prior-touch/current-depth interval
ticket policy.  A genuine interval ticket needs independent order state and a
lifecycle; retaining that model while forbidding new structures would only
hide the same structure in ad-hoc dictionaries.

## Market-data convention

Market-data rows are provided snapshots.  At every aligned bundle the engine
materializes a mutable `curr` row for **both** pair legs:

- a leg with an input row uses a shallow copy of that supplied row; and
- a missing leg forward-fills a shallow copy of its last supplied quote/book
  snapshot, with its processing `datetime` advanced to the aligned bundle time.

Orders, cancels, and fills do not alter the next supplied or forward-filled
snapshot.  `curr` is therefore the interval-local executable book.  It may be
reduced by an immediate aggressive sweep during the current interval so a later
sweep cannot consume the same displayed lots, but the next aligned interval is
materialized again from supplied market data rather than that mutated book.

### Forward-fill trade clearing

A missing input row means there were **no trades and no market-data change** on
that leg during the aligned interval.  The forward-filled row must preserve
quote state—bid/ask prices and displayed `bidvol*` / `askvol*` depth—but clear
all transaction-flow fields before `_apply_tick` / queue decay runs.

At minimum, clear:

- `traded`, `traded_p1`, and `traded_p2` to missing/`NaN`;
- `traded_v1` and `traded_v2` to zero (or missing where the shared matcher
  treats missing as zero);
- interval trade volume and value/turnover fields, including `totalvol`,
  `totalvalue`, `volume`, `turnover`, and any source-specific aliases or
  derived trade-count fields.

Do **not** clear `bidvol*` or `askvol*`: those are displayed quote depth, not
executed trade volume.  Clearing the trade-flow fields prevents a forward-filled
leg from replaying the prior row's prints and depleting a passive queue twice.

`updated` continues to identify only legs with a genuine input row; it remains
the signal-provenance indicator.  It is not an execution-eligibility gate: a
materialized forward-filled `curr` book is executable.  The only invalid case
is a hedge leg that has not yet received any snapshot and therefore has no
quote/book to forward-fill.

## Hedge lifecycle

```text
aligned bundle T
  -> install supplied rows and zero-trade forward fills in curr
  -> cancel risk-incompatible basis exits if an old hedge residual exists
  -> match pre-existing passive / residual orders
  -> route all passive fills together
  -> if the hedge leg has a current supplied book:
         strategy places aggressive order and immediately sweeps that order ID
         full fill: no resting hedge order
         partial fill: same order becomes a normal limit with its residual qty
     otherwise:
         retain only the existing logical _unhedged reservation
         wait for the hedge leg's first supplied snapshot
  -> maintain/requote ordinary maker quotes for later snapshots
```

The initial hedge attempt uses the current hedge-leg touch plus
`HEDGE_OFFSET_TICKS` as its limit:

- buy: `askpx0 + offset * tick`;
- sell: `bidpx0 - offset * tick`.

`offset == 0` can take only the touch; a positive offset permits a sweep of
the reachable deeper levels.  The sweep stops at the first level outside the
limit, on invalid depth, or once the requested quantity is complete.

For example, a buy limit of 101.00 can fill `2 @ 100.75` and `3 @ 101.00`.
Its reported price is 100.90, not 101.00.  An unfilled residual retains the
101.00 limit.

### Sparse bundles and forward-filled books

If a P fill occurs in a P-only bundle and S already has a supplied snapshot,
the aligned market forward-fills an S `curr` row with unchanged quotes/depth and
cleared trade flow.  The strategy may immediately place and sweep the S
aggressive hedge against that carried S book.  For this simulator, the absent S
row means “unchanged since its last supplied snapshot,” so the carried depth is
the current executable depth for the interval.

If S has not yet supplied any snapshot, the strategy keeps its existing
`_unhedged` reservation until it can create the initial S order.  Once an
initial aggressive attempt has left a normal S limit resting, that normal order
participates in subsequent aligned intervals through its forward-filled or
fresh S `curr` row.  A forward-filled S row has no trade flow, so it cannot
create a passive queue fill; a marketable normal limit may still use the
depth-limited crossing-limit path against the executable carried S book.  It is
not repriced merely because P updates.  This avoids unnecessary cross-leg
cancel/repost churn while preserving the supplied-book convention.

## Matching rules

### Initial aggressive hedge attempt

The strategy creates the hedge through the existing
`place_order(..., aggressive=True)` mechanism.  Immediately after placement,
it invokes `match(contract, order_id=order["order_id"])`, the targeted
immediate-match path for **that order ID only**.  This is not the usual
end-of-step `match(contract)` pass: it must neither wait for the next snapshot
nor match unrelated resting orders on the same contract.  The broad match path
does not execute an `aggressive=True` order.

The targeted aggressive path must be depth-aware:

1. validate and tick-normalize the submitted limit;
2. consume only opposite-side levels within that limit;
3. use the configured participation assumption for each attempted level;
4. decrement the consumed volume in the current `curr` level;
5. emit actual filled quantity, VWAP, and per-level consumption;
6. remove the order if fully filled; or
7. on a partial fill, retain the same order ID and metadata, reduce its
   remaining quantity, rebase its queue behind the remaining current depth and
   earlier normal orders at that price, and set `aggressive=False`.

The strategy owns the immediate-attempt invocation, event accounting, and
position update.  The common market owns the targeted depth walk and the
existing placement message count.  This separation is intentional: the hedge
is immediately driven by the strategy, while only a partial residual becomes a
queued order for the normal matching pass.

The configured participation remains explicit.  `FAK_AVAIL` is the default
conservative assumption; a pairs configuration may deliberately select a
different value, including `1.0` as an explicitly optimistic sensitivity case.
The reported fill must include that assumption.

When several strategy-driven aggressive sweeps act on one contract in a
bundle, each sees the remaining `curr` depth after earlier consumption.  There
is no double claim of displayed lots and no independent cursor to pass
incorrectly.
Participation is interpreted per attempted order against the then-remaining
displayed depth; it is not a hidden, bundle-wide depth reservation.

### Carried normal limit

After a partial initial sweep, the original aggressive order becomes an
ordinary normal limit.  It preserves its order ID, creation time, role metadata
and original limit, but carries only its remaining quantity.  It belongs in the
normal side book.  The partial execution itself has no synthetic cancel
message.

It must not receive another fabricated fill from bundle T.  On later snapshots
it follows normal matching rules:

- if it is non-marketable, it advances and fills through the passive FIFO queue
  model and recorded trades;
- if a supplied or forward-filled current book makes its limit marketable,
  execution is a depth-limited crossing-limit fill at current available prices,
  never an unconditional fill at its limit; and
- any remaining quantity stays as the same normal limit until it fills or an
  explicit strategy/risk/EOD cancellation removes it.

Fill events must identify the actual execution mode (`aggressive_sweep`,
`crossing_limit`, or `passive_queue`) so fees, taker-cost telemetry, and audit
reports do not treat every residual fill as a taker fill.

## Risk and position invariants

- Before routing an entry fill, cancel its paired entry sibling.
- While `_unhedged` is non-empty, block incompatible new entries.
- At the start of a bundle with an unresolved hedge residual, synchronously
  cancel every live `basis_exit` role before ordinary matching.  A second
  passive convergence exit must not enlarge a naked tail.
- A normal hedge residual is keyed to its own leg.  A wrong-leg update neither
  cancels nor reprices it, and its zero-trade forward fill cannot create a
  passive queue fill.  If its limit is marketable against that executable
  carried book, it may still receive the depth-limited crossing-limit execution
  defined above.  A newly created initial aggressive hedge may likewise sweep
  the target leg's carried `curr` book when that leg did not update.
- A risk or EOD action may cancel the normal residual through the existing
  role/order cancellation path; that is a real cancel message.  A partial
  aggressive sweep alone is not a cancel and does not generate synthetic
  cancel/repost traffic.
- Route decisions still use the previous observable signal for a filled fresh
  leg.  The current book may be used for execution, but the contemporaneous
  signal may not explain the fill that caused the decision.

## Required common-market changes

1. Keep the exchange-time pair merge and the sort/deduplicate correction from
   `2a9d7b5`.  Do not revert those chronology safeguards.
2. At every aligned time, forward-fill each missing leg's last supplied
   quote/book snapshot into `curr`, advance its processing timestamp to the
   bundle time, and clear its transaction-flow fields before queue decay.  Keep
   `updated` limited to genuine source rows.  Retain the meaningful per-raw-row
   `PairMarket.itr` advancement.
3. The `d7b3ad5` interval-ticket API, raw snapshot bundle fields, and
   `SnapshotLiquidity` cursor are removed.  Strategies must migrate to the
   ordinary-order path described here before their next run.
4. Make `place_order` calculate FIFO position from external `curr` depth plus
   existing earlier same-price orders, rather than adding strategy quantity to
   `curr` volume fields.
5. Extend the existing cancel functions with a role/predicate selection path.
   On a cancel, remove the cancelled remaining quantity from queues of later
   surviving orders at that level.  Pairs role cancellation must use this common
   path instead of directly deleting book entries.
6. Extend `Market.match` with a targeted immediate path, selected by order ID,
   for an existing `aggressive=True` order.  It must consume current depth and
   return only that order's depth-limited fill; it must not process unrelated
   normal orders in the book.
7. Replace full-size crossing behaviour in `Market.match` with a depth-limited
   crossing-limit routine for later *normal* limits that become marketable.
8. Preserve ordinary passive queue matching for non-marketable orders.  A new
   passive order still cannot fill from the snapshot that created it.

## Required pairs changes

1. Remove `SnapshotLiquidity` imports, `_IntervalHedge`, interval context/raw
   snapshot helpers, ticket submit/settle/cancel helpers, and ticket-only event
   fields.
2. Reuse the existing `_unhedged` logical reservation to represent only
   exposure that has not yet been completed by the ordinary order mechanism.
3. From the route handler, place `aggressive=True` whenever the hedge leg has
   an existing supplied `curr` book, whether or not that leg is in the current
   bundle's `updated` list.  Immediately invoke the targeted order-ID match
   path and apply its returned fill to the hedge ledger at once; do not use the
   broad normal `match(contract)` cycle for this attempt.
4. If that sweep is partial, preserve the same order as a normal limit with a
   `pending_hedge` role.  Route later fills from that normal order back to
   `_unhedged` and apply maker/taker accounting from the actual execution mode.
5. Do not automatically cancel/repost a pending hedge.  Reprice only under an
   explicitly approved policy, not because the other leg ticked.
6. Define entry gates over the raw-row span of an aligned bundle.  With
   `before = processed rows` and `after = before + len(updated)`, block an
   entry when `before < no_entry_first_steps` or
   `after > session_total_steps - no_entry_last_steps`.

## Acceptance tests

Common tests must cover:

- partial touch fills, offset-gated sweeps, actual per-level VWAP, and the
  distinction between limit price and fill price;
- two same-bundle strategy-driven aggressive sweeps consuming remaining `curr`
  depth, followed by reset from the next supplied snapshot;
- a P-only aligned bundle forward-filling S quote/depth while clearing
  `traded*`, trade volume, value/turnover, and related transaction-flow fields;
- no passive queue decay or replayed passive fill from a zero-trade
  forward-filled row;
- configured participation, including an explicit `1.0` optimistic case;
- aggressive partial -> same-order normal residual -> later passive, crossing,
  or cancellation outcomes;
- no same-snapshot second fill after conversion to a normal residual;
- cancel/repost and selective role cancellation restoring exact FIFO queues and
  visible external depth; and
- invalid prices, missing depth, exact message counts, and immutable input-row
  behaviour.

Pairs smoke tests must cover:

- same-bundle fresh-leg initial hedge with VWAP at available prices;
- P-only parent fill immediately sweeping an already-supplied carried S book;
- a parent fill before the first S snapshot deferring its initial S hedge;
- a partial normal S hedge residual neither repricing nor passively queue-filling
  on a P-only zero-trade forward fill, while any marketable crossing is
  depth-limited;
- an exit-tail residual cancelling all basis exits before another one can fill;
- correct maker/taker telemetry for carried residuals; and
- exact warm-up/wind-down boundaries for one- and two-row bundles.

## Documentation changes required with implementation

This design supersedes the pairs policy language that calls the hedge a
one-interval protected ticket and says it never rests in `bids`/`asks`.  Update
the policy, pairs README, pairs AGENTS notes, common README, and the normative
requirements together.  The common requirements must describe the explicit
same-interval aggressive-sweep convention, the subsequent normal-limit
lifecycle, and the rule that a supplied forward-filled `curr` snapshot remains
executable until a replacement snapshot arrives.  It must also define that an
absent leg forward-fills quote state but clears all interval trade flow, rather
than presenting this behaviour as an interval ticket.
