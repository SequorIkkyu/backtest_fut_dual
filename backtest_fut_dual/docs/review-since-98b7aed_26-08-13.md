# Review of Changes Since `98b7aed38c1c4eb6ffc107bf254a691900bc323a`

**Review date:** 2026-08-13  
**Scope:** tracked diff from the named commit to the current `HEAD` (`15e31be2`), plus the resulting working tree. The pre-existing untracked `.claude/` directory is intentionally excluded.

## Overall assessment

This is a substantial and justified shift from a legacy backtest implementation
to an isolated, auditable maker-hedger replay foundation. The diff changes 77
tracked files, adding 21,349 lines and removing 1,139. Its main value is not
more strategy logic; it establishes the controls required to treat a simulated
run as defensible research evidence.

The added complexity is necessary for the supported S0 evidence path, but not
for the retained compatibility examples. The key design decision is sound:
legacy `backtest.py`/`market.py`/`strategy.py` remain usable but cannot be
presented as S0 economics, stress, holdout, or promotion evidence.

## Review of the additions

| Area | Nature of the addition | Usefulness and necessity |
| --- | --- | --- |
| Foundation boundary and contracts | Adds versioned, immutable vocabulary and a narrow `DualBookFoundation` public facade for one passive quoted leg and one aggressive hedge leg. Policy code receives immutable, depth-free context and supplies its own actions, prices, and triggers. | This removes ambiguous ownership and prevents a policy or caller from bypassing the accounting/execution path. It is necessary to make decisions, orders, fills, and ledger effects traceable to the same dual-book decision. |
| Strict loader and causal ingress | Adds validation of declared contracts, clocks, tick/depth data, source ordering, retained snapshots, and causally available signals; decisions are ordered by availability rather than merely exchange time. | This is essential protection against look-ahead bias, invalid books, and non-reproducible event ordering. Without it, a backtest can appear profitable using information that was not available at the decision. |
| Production replay and sessions | Adds `ProductionReplayAdapter` as the only supported route, enforces product calendars/EOD, retains state across breaks, blocks decisions in breaks, and applies scheduled timing once. | This turns otherwise separate components into one operationally controlled pipeline. It is necessary because session breaks, EOD close-out, and duplicated delay application materially alter execution and PnL. |
| Execution, matching, lifecycle, and ledger | Adds retained-depth aggressive/EOD execution, price-time/FIFO passive matching, capacity reservation and terminal state machines, dual-leg positions, residual risk, and one-shot EOD completion. Production maker fills require matcher-issued evidence. | These controls prevent fabricated or duplicated fills, restored consumed depth, capacity bypasses, double closes, and unhedged risk being hidden. They are necessary for an execution simulation whose quantities and inventory can be reconciled. |
| PnL and economic eligibility | Adds independent dual-leg PnL attribution, fee/rebate handling, price-observation validation, waterfall reconciliation, and a fail-closed economics predicate. | This separates a calculated result from an eligible economic result. It is necessary because a reported total without reconciled fills, marks, EOD state, and independent accounting inputs is not credible economic evidence. |
| Canonical and research telemetry | Adds schema-versioned JSONL artifacts, invariant checking, content-hashed provenance, research-specific semantic validation, cross-table joins, and sealed manifests. | This makes runs inspectable and reproducible after execution rather than relying on mutable in-memory strategy records. It is necessary for review, comparison, and diagnosing whether a result rests on valid data and controls. |
| Raw five-level snapshot adapter | Adds authenticated source-file/row provenance, `max(exchtime, timestamp)` replay availability, run-global source sequencing, tick/depth validation, interval-flow allocation, and distinct `SnapshotIntervalQueueProxyEvidence`. | This is a careful, necessary accommodation of the available data. It permits repeatable policy comparison without falsely relabelling aggregate snapshot flow as observed trades or certified latency. The explicit evidence type and shared-bucket conservation are especially important safeguards. |
| Stress controls, documentation, and tests | Adds independent, content-hashed stress dimensions; documents the supported/legacy boundary and raw-data claim boundary; reorganizes examples; and adds 141 foundation acceptance checks alongside 72 legacy checks. | Stress without declared ownership can accidentally transform data twice or make incomparable runs. Documentation and regression tests are necessary to keep the new route usable and to prevent future work from silently drifting back into legacy assumptions. |

## What the changes do not establish

The implementation is deliberately honest about the remaining boundary. The
available raw files are five-level snapshots with cumulative flow, not a
trade-level tape and not a certified receive-time feed. Consequently,
snapshot-interval fills support comparisons under the declared proxy model;
they do **not** establish live fill probability, exchange queue position,
individual aggressor side, or physical latency.

The operational remediation plan therefore remains a **partial pass / S0
promotion no-go**. A frozen realistic policy and input episode, followed by a
disjoint untouched holdout through `ProductionReplayAdapter`, are still
required. Deterministic fixtures verify the controls; they cannot establish
real-world performance or promotion readiness.

## Verification performed

On the reviewed working tree, the required aggregate acceptance command passed
with **214/214** tests: **72/72 legacy** and **142/142 foundation**. `git diff
--check` also passed. This is strong regression evidence for the implemented
contracts and controls, but it does not change the outstanding realistic-data
and holdout requirement above.

## Conclusion

The additions are useful and, for an auditable S0 maker-hedger study, largely
necessary: they replace implicit simulation assumptions with explicit,
versioned, causally constrained, and independently checkable evidence. The
most important remaining work is empirical rather than infrastructural—freeze
a real policy/data episode, run it solely through the new production path, and
evaluate a genuinely untouched holdout while retaining the bounded
snapshot-proxy claim.
