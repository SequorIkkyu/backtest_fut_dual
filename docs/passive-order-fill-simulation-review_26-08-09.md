# Passive Order Fill Simulation Review

**Date:** 2026-08-09
**Updated:** 2026-08-10; raw snapshot constraints accepted as the model boundary
**Scope:** supported production-replay passive-maker path and the actual raw
tick files in `E:\FinData\HFT\ticks` that drive the compatibility backtests
and their reported order fills

## Decision

The available feed is accepted as a **five-level snapshot and cumulative-flow
data contract**. It is not an exchange trade tape, and the absence of
trade-level IDs, aggressor side, and true receive timestamps is a permanent
model boundary for this dataset, not a reason to reject the dataset or require
an unavailable replacement feed.

The appropriate passive-fill interpretation is therefore a deterministic
**snapshot-interval queue proxy**:

- queue advancement is inferred from each raw cumulative-volume and turnover
  interval using a declared price-allocation rule;
- all source fields remain intact and every derived interval is linked to its
  source file and row sequence;
- results are valid for comparison of policies under this fixed data and
  model convention; and
- a fill is not an assertion that a particular exchange order, or the strategy
  itself, would have filled live.

This relaxation applies to the raw-data requirements, not to the production
evidence boundary. `RawSnapshotAdapterConfig` now creates immutable
`SnapshotInterval` inputs, and `PassiveMatchingService` issues the separately
typed `SnapshotIntervalQueueProxyEvidence` records from their declared shared
buckets. The raw snapshot convention is never passed to `PassiveTrade`, and an
inferred interval bucket is never relabelled as an observed aggressor trade.

`backtest.py`, `market.py`, and `public_tools/` remain frozen compatibility
paths. Accepting their raw-data assumptions does not retroactively make their
results S0 evidence. A future supported snapshot bridge may use the same
declared model family, but must preserve the supported path:

```text
strict loader with snapshot adapter -> CausalIngress -> ProductionReplayAdapter
-> DualBookFoundation -> matcher-issued snapshot-proxy evidence
-> calendar EOD -> PnL attribution -> canonical + research telemetry
```

## Actual raw-data contract

The inspected files use one common schema:

```text
timestamp, exchtime,
bidpx4/bidvol4 ... bidpx0/bidvol0, askpx0/askvol0 ... askpx4/askvol4,
lastpx, totalvol, totalvalue, openinterest
```

The accepted mapping is:

| Item | Raw source or derived value | Accepted convention |
|---|---|---|
| Contract | File name, for example `IF2501.csv` | Declared by the adapter and retained in provenance. |
| Exchange event time | `exchtime` | Canonical raw event clock after declaring the source timezone. |
| Availability time | `max(exchtime, timestamp)` | Deterministic replay-availability proxy. Both raw times remain preserved; this is not a claim that `timestamp` is certified receive time. |
| Source sequence | Content-hashed file identity and file-row ordinal | Preserve both raw fields, then assign a run-global, strictly monotone integer `source_seq` from the declared merge order `(availability, file identity, row ordinal)`. It is a deterministic replay tie-breaker, not an exchange sequence. |
| Book state | Five bid and five ask levels | The complete retained depth boundary for this model. |
| Interval flow | Delta of `totalvol` and `totalvalue` | The accepted source for proxy queue consumption on the declared proxy/quoted contract only. It is aggregate flow, not a list of prints. |
| Trade ID and side | Not present | Intentionally unavailable in snapshot mode; no synthetic observed-trade identity or aggressor-side claim is made. |

No raw field is overwritten. The adapter derives the normalized contract,
availability proxy, and run-global source sequence, records their rule/version
in the run manifest, and retains the original file identity, row ordinal,
`timestamp`, and `exchtime` for audit.

### Clock check

Using `timestamp` directly as `recv_ts` is not valid: it precedes `exchtime`
in some rows and a mechanical strict-loader mapping fails closed.

| File checked | Direct mapping result | Raw-derived availability result |
|---|---|---|
| `IF/2025/2025-01-02/IF2501.csv` | Rejected at row 8,451: `receive_before_exchange` | 27,778 rows accepted; no validation issues. |
| `SHFE/2025/2025-02-21/sc2504.csv` | Rejected at row 16,772: `receive_before_exchange` | 103,550 rows accepted; no validation issues. |

The `max` convention leaves the raw records unchanged and derives a simulation
availability time that cannot precede the event time. Any latency result must
be labelled as a result under this replay convention, not measured network or
exchange latency.

### Snapshot characteristics

The following samples describe the accepted resolution of the data rather than
a data-quality failure. `delta volume` is the change in raw cumulative
`totalvol`; the interval VWAP is `delta totalvalue / (delta totalvol * multiplier)`.

| File checked | Rows | Observed exchange cadence | `timestamp < exchtime` | Positive intervals with quantity > 1 | Off-tick interval VWAP |
|---|---:|---|---:|---:|---:|
| `IF/2025/2025-01-02/IF2501.csv` | 27,778 | Median 500 ms | 550 (1.98%) | 15,092 / 21,474 (70.3%) | 1,608 / 21,474 (7.49%) at 0.2 tick |
| `SHFE/2025/2025-02-21/sc2504.csv` | 103,550 | Median 250 ms | 52,395 (50.60%) | 12,733 / 26,707 (47.7%) | 1,210 / 26,707 (4.53%) at 0.1 tick |

A multi-lot increment may be one trade or many, and an off-tick interval VWAP
is consistent with several valid-tick prints. Snapshot mode accepts that loss
of detail and applies one declared aggregation rule consistently across all
policies and all base/stress/holdout episodes.

Older flat files remain a separate capture generation. For example,
`ag2210.csv` has an initial zero-depth/crossed observation and a maximum
`timestamp - exchtime` gap of about 22,503,704 ms. It should receive its own
declared raw-data scope and session/start-row convention; it need not be
silently pooled with the 2025 hierarchy.

## Accepted snapshot-interval fill model

The existing compatibility loader provides the practical model baseline:

1. Declare the proxy-interval contract set. In the supported single-pair
   replay it must be exactly the passive-maker / quoted product; other product
   snapshots carry books but never proxy flow.
2. Take the per-row deltas of `totalvol` and `totalvalue` on that proxy leg.
3. Compute the interval average price from turnover, contract multiplier, and
   positive volume.
4. Allocate the interval quantity across the two neighbouring valid ticks with
   a deterministic, quantity-conserving interpolation rule.
5. Reject an interval by default when either allocated bucket is outside the
   current retained positive-depth envelope (lowest bid through highest ask).
   A declared `drop` disposition removes the offending source row before it can
   reach matching or economics.
6. Use those price buckets to advance displayed queues in price priority and
   simulated FIFO order. A price-reach rule determines which resting buy or
   sell queue can consume each bucket; the same interval budget may not be
   reused by incompatible simulated orders.
7. Link every resulting proxy fill to the source file hash, contract, row
   sequence, interval bucket, arrival snapshot, queue-at-arrival, and model
   version.

This model has no observed aggressor side. Its directional result comes from
the price-reach rule, not a statement about who initiated a particular trade.
The first-row, cumulative-reset, zero-volume, turnover-rounding, and
off-tick-VWAP dispositions must be declared with the model version. They are
not grounds to demand a different feed.

The present foundation matcher has strong controls that the snapshot bridge
must retain: maker arrival only after a causal snapshot, price priority,
same-price FIFO, shared interval-budget conservation, lifecycle/capacity
checks, matcher-issued evidence, and telemetry/ledger reconciliation. The
bridge must expose its evidence as `snapshot_interval_queue_proxy_v1` (or a
successor), never as a vendor trade or exchange queue position.

## Claim boundary

| Supported claim with this data contract | Not supported by this data contract |
|---|---|
| Relative policy, modelled-arrival-latency, fee, queue-inflation, and depth-coverage comparison under the frozen snapshot-interval model. | A prediction of a strategy's live fill probability or a statement that a specific order would have filled. |
| Causal replay according to the declared `max(exchtime, timestamp)` availability convention and modelled submission/arrival delays. | Measurement of physical market-data, gateway, or exchange latency. |
| Quantity-conserving accounting of simulated orders against a shared, model-derived interval budget. | Reconstruction of individual trades, their true order, their taker side, or an exchange queue position. |
| A frozen realistic raw-data episode and disjoint holdout for a bounded snapshot-proxy study. | A claim about hidden liquidity, cancellation priority, market-by-order priority, or venue-specific execution quality. |

The model has no guaranteed optimistic or conservative bias. It does not see
hidden liquidity, cancellations, replenishment, amendments, order-level
priority, or events inside a 250/500 ms snapshot interval. Quote prices and
derived interval buckets outside the five retained levels must be rejected or
explicitly handled as off-depth; the accepted `drop` disposition never reaches
matching or `economics_eligible` evidence. Assuming zero queue ahead is allowed
only when declared as a model choice and separately reported.

## Required record for a snapshot-proxy experiment

The following is sufficient for this raw-data mode; a separate trade-level
feed is an optional calibration enhancement, not an entry requirement:

1. Raw-file hashes, venue/product/session/date scope, filename-to-contract
   mapping, timezone, retained rows, and explicit session/start-row rules.
2. The exact availability convention, content-hashed merge order, raw file and
   row identity, run-global source-sequence rule, book depth, and all snapshot
   validation/disposition results.
3. The proxy-interval contract set, interval-flow formula, multiplier/tick
   table, two-tick allocation, retained-depth boundary/disposition, price-reach
   rule, shared-budget conservation rule, and estimator version.
4. Policy decision, submission, and arrival timing; queue-at-arrival evidence;
   quote-depth disposition; fees; and stress controls, all content-hashed.
5. Proxy-fill telemetry that names the source snapshot interval and model
   version, rather than a fictitious vendor trade ID or aggressor side.
6. The frozen calibration/base/holdout split and the exact claim boundary.

Where trade-level executions become available later, they should be used to
calibrate or challenge the proxy. Their absence does not invalidate a
snapshot-proxy study.

## Implementation status and promotion work still required

1. **Implemented:** `RawSnapshotAdapterConfig`, `RawSnapshotFile`, and
   `read_raw_snapshot_market_data()` retain source fields, raw file hashes and
   row ordinals, apply `max(exchtime, timestamp)`, validate five-level grids,
   and assign the globally unique `source_seq` required by strict ingress.
2. **Implemented:** matcher-owned `SnapshotIntervalQueueProxyEvidence`
   preserves interval/bucket identity, queue-at-arrival, price priority,
   FIFO, and shared-bucket conservation without fabricating `PassiveTrade`.
3. **Implemented:** canonical and research telemetry export the distinct
   evidence type, raw interval references, source hash/ordinal, model,
   price-reach, and availability fields. Research sealing validates their
   type/nullability/enum/book-causality and bucket-conservation joins.
4. **Implemented:** raw book and interval buckets are tick-validated; reset,
   zero-volume, and invalid-interval dispositions are explicit. Volatility
   stress for snapshot proxy evidence fails closed until it has its own
   declared transformation model.
5. **Still required:** Run a frozen actual snapshot episode and a disjoint holdout through the
   supported production route. Promotion, if considered, is promotion of this
   bounded snapshot-proxy model, not of live-fill prediction accuracy.

Deterministic adapter fixtures prove infrastructure only. The remaining
realistic episode and untouched holdout are still required before any S0
comparison, stress, or promotion claim.
