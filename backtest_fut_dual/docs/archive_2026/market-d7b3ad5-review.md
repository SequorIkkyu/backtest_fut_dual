# Review: `d7b3ad550d721a1f007ea33c19cd8bc9abd48172` (`market.py`)

Date: 2026-07-24  
Scope: usefulness and prudence of the `SnapshotLiquidity` / interval-limit execution changes.

## Decision summary

The commit is useful infrastructure for a snapshot-based hedge-execution simulator, but it should be treated as an experimental, explicitly calibrated feature. It is not prudent to use its results as conservative estimates of performance or hedge risk without the safeguards below.

The historical commit's `tests/test_market.py` suite passed (48 tests). That establishes internal behavior, not the realism of its execution assumptions.

## What is good

- Interval-limit tickets are intentionally separate from both resting orders and FAKs. This avoids mixing their lifecycle with the queue model.
- Raw market snapshots are separated from mutable `curr`, so simulated orders do not increase the external displayed depth later used for interval settlement.
- The shared `SnapshotLiquidity` cursor can reserve per-level liquidity across tickets without mutating raw market data.
- The implementation retains useful audit information: placement snapshot time, execution snapshot time, frozen limit, execution levels, VWAP, and cancellation state.
- The code validates contract matching and requires the execution snapshot to be strictly later than the stated placement snapshot.

## Findings and required decisions

### 1. Full displayed-depth fills are optimistic (high model risk)

`settle_interval_limit` fills against 100% of visible opposing depth through the frozen limit, explicitly without the normal `FAK_AVAIL` haircut. Snapshot L2 data does not establish queue priority, latency, intervening trades/cancels, or competing participants.

Impact: fill rate, fill size, hedge quality, PnL, and tail-risk estimates may be materially overstated.

Required decision: define a configurable, empirically calibrated participation/latency model. Treat 100% displayed-depth results as an optimistic upper bound, not a base case. Run sensitivity analysis across conservative participation assumptions.

### 2. Shared-liquidity protection is optional (high correctness risk)

The double-count safeguard works only when every caller passes the same `bundle["liquidity"]` cursor into every settlement. If a caller omits it, `settle_interval_limit` silently creates a new cursor, allowing multiple tickets to each consume the full displayed size. The cursor also stores snapshots but does not verify that a supplied execution snapshot belongs to it.

Impact: an integration mistake can create impossible fills and artificially improve results.

Required action: require a cursor for snapshot settlement, or bind it internally to the pair bundle. Validate contract, exchange timestamp, and snapshot identity before consumption.

### 3. Input ordering was unsafe in the reviewed commit (resolved immediately afterward)

The reviewed commit assumed pair frames were already sorted and unique by exchange time. Since `step_pair` is a two-head merge, unsorted rows can be processed out of chronological order and duplicate exchange timestamps can become separate bundles.

The immediate follow-up commit `2a9d7b5` (`fix: sort when load pairs`) adds deterministic sorting/deduplication and corresponding tests. Any branch based directly on `d7b3ad5` needs that follow-up or an equivalent fix.

### 4. “One interval” is a caller convention, not an enforced invariant (medium risk)

The code only checks that execution time is later than placement time. A ticket may be settled many snapshots later, and the caller supplies the placement timestamp rather than a verified prior snapshot object.

Impact: stale-ticket execution and accidental timing/look-ahead mistakes are easy to introduce.

Required decision: specify whether settlement must occur on the next eligible snapshot. If so, encode the originating snapshot/time immutably in the ticket and reject skipped intervals unless a deliberate policy override is recorded.

### 5. No strategy-level integration yet (medium delivery risk)

The associated strategy change retains interval-execution audit fields, but does not submit, settle, or cancel interval-limit tickets. The feature is exercised only by market-level tests.

Impact: the market API is unproven in the pair strategy’s actual state machine, risk controls, and event logging path.

Required action: add end-to-end pair-strategy tests after requirements are defined.

## Minimum test plan after pair-strategy requirements arrive

1. Multiple interval tickets against the same execution snapshot, including accidental missing or mismatched cursor use.
2. Partial fill, cancel, replacement, and message counts across a trading-day boundary.
3. Next-snapshot-only settlement and explicit skipped-interval policy.
4. Pair frames with unordered and duplicate exchange times (covered by `2a9d7b5` at the market layer).
5. Latency/participation sensitivity and no-look-ahead assertions.
6. Full pair-strategy integration: passive fill -> interval hedge submit -> execution/cancel/requote -> fill event audit trail.

## Provisional recommendation

Retain the architecture, but gate rollout on: (1) the input-normalization follow-up, (2) mandatory/validated shared liquidity, (3) a documented and calibrated execution assumption, and (4) pair-strategy integration tests.
