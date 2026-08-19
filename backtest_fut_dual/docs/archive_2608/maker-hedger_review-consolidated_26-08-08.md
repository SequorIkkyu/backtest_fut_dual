# Maker-Hedger Infrastructure Review — Consolidated Validation

**Date:** 2026-08-08  
**Scope:** `common/`, its pair-policy integration points, and the research contracts in `D:\OneDrive\Research\Futures-Maker-Hedger`.  
**Consolidates:** the DS-4 Pro static audit and GPT-5.6 static/runtime validation.

> **Historical / supersession note (2026-08-10):** This is the
> pre-remediation assessment at the 2026-08-08 engine state, not the current
> acceptance decision. The implemented-control status is superseded by the
> [operational remediation plan](maker-hedger-s0-operational-remediation-plan_26-08-08.md).
> For the accepted five-level raw snapshots, causal replay uses declared
> availability `max(exchtime, timestamp)`, preserving both raw clocks and
> source ordering; it does not treat `timestamp` as certified receive time.
> The supported snapshot adapter and matcher-issued snapshot-proxy evidence
> path are now implemented; frozen realistic-policy and untouched-holdout
> evidence remain the separate Gate-7 promotion requirement.

## Verdict

Both reviews reach the same conclusion: `common/` is **not an S0-acceptable foundation** for the maker-hedger research programme.

The matching and single-leg accounting kernels are useful and should be preserved. The surrounding replay, session/EOD, aggressive execution, dual-book, telemetry, invariant, and provenance capabilities do not meet the registered research contracts. The five consolidated-review critical findings are confirmed; two liquidity/causality weaknesses are broader than the original review stated.

| Finding | Consolidated validation |
| --- | --- |
| C1: session / EOD semantics | Confirmed. Night and day are independent backtest lifecycles; segment end cancels and touch-flattens. |
| C2: aggressive execution / depth | Confirmed. Direct and sweep FAK have price/depth defects; sparse pair bundles can also restore consumed depth. |
| C3: causal replay | Confirmed. Pair replay is exchange-time-first and has no causal sequences or decision snapshots. |
| C4: telemetry / invariants | Confirmed. Records are strategy-local and optional; reconciliation is advisory only. |
| C5: dual-book policy boundary | Confirmed. Pair-v2 behavioural tests cannot instantiate the current market engine. |

## Validation basis

The code was traced through `backtest.py`, `market.py`, `strategy.py`, `sessions.py`, `reporting.py`, `grid.py`, `order_limit.py`, `public_tools/`, the common tests, pair-v2 code/tests, and the research experiment, hedge-universe, and telemetry contracts.

Runtime checks used Python 3.10.13 with bytecode generation disabled:

| Check | Result |
| --- | --- |
| Common zero-dependency runner | 72 / 72 passed |
| Pair-v2 contract/manifest tests | 8 / 8 passed |
| Pair-v2 behavioural tests | 0 / 8 passed; all fail at unsupported `engine_version` setup |
| Core-module `py_compile` | Passed |
| Targeted lifecycle, FAK, pair-causality, sparse-depth, timestamp, and cancellation checks | Reproduced the issues below |

The green common suite validates useful matching and single-leg accounting primitives. It does not validate S0 research conformance: it does not cover EOD state continuity, off-touch FAK pricing, post-FAK depth, depth reuse across sparse pair bundles, receive-time decisions, schema telemetry, or fail-closed reconciliation.

## Critical findings

### C1 — session splitting turns a break into EOD; EOD liquidation is not depth-realistic

`load()` splits data into global night/day windows. `run_date()` calls `Backtest.backtest()` separately for those frames. Every call resets strategies before its loop and invokes `stop()` at its end; this erases state between the night and day portions of one trading day.

`Strategy.stop()` cancels resting orders and creates a synthetic closing fill at the best bid/ask. It does not consume depth, produce a partial/residual outcome, or record a realistic aggressive execution lifecycle.

This conflicts with the research product requirements:

- Zn's 01:00 break is intra-day; the night and following day sessions share the same trading day.
- T trades until 15:15; the global day window ends at 15:00.
- EOD close-out needs a depth-consuming execution result and explicit incomplete-liquidity disposition.

Relevant implementation:

- `common/backtest.py:70-73` — global session split.
- `common/backtest.py:154-227` — separate session backtest calls.
- `common/backtest.py:241-259` — per-call reset and stop.
- `common/strategy.py:1048-1070` — cancellation and touch-priced synthetic flatten.
- `common/sessions.py:18-19` — global session constants.

Targeted reproduction:

```text
two_backtest_segments_trace:
[("reset", "X"), ("stop", "X"), ("reset", "X"), ("stop", "X")]
```

### C2 — FAK reports incorrect prices and permits reusable liquidity

The direct `Market.fak()` path returns the submitted limit for an off-touch marketable FAK rather than the executable touch, and does not reduce displayed level-0 depth. `_fak_sweep()` computes a correct VWAP but also does not decrement consumed levels.

```text
direct_fak: {"px": 100.01, "qty": 3}
touch=100.005
remaining_askvol0=10

sweep_fak: {"px": 100.00642857142857, "qty": 7}
remaining_askvol0=10
remaining_askvol1=10
```

The targeted aggressive-order path is a partial positive exception: `place_order(..., aggressive=True)` followed by targeted `match()` executes through `_sweep_current_depth()`, which does calculate VWAP and reduce `curr` depth within the current bundle. This is not a contradiction; it shows two inconsistent aggressive-execution implementations that must be unified.

An additional material defect exists in the intended active pair hedge route. On a sparse pair bundle, `PairMarket` recreates the absent leg's book from its prior raw snapshot. It therefore restores depth consumed by an earlier sweep even though the hedge leg had not updated. One 5-lot S snapshot permitted two 3-lot sweeps with no S update:

```text
first_fill=3, depth_after_first=2
next_bundle_updated=["P"], restored_depth_without_S_update=5
second_fill=3, depth_after_second=2
total_filled_since_only_S_snapshot=6
```

`test_pairmarket_forward_fill_reloads_supplied_depth_after_immediate_sweep` explicitly asserts this restoration. Any unobserved liquidity replenishment must be an explicit, conservative, versioned model assumption; it cannot silently follow every unrelated-leg bundle.

Relevant implementation:

- `common/market.py:449-515` — direct and sweep FAK.
- `common/market.py:627-710` — targeted aggressive depth sweep.
- `common/market.py:874-890, 951-956` — raw-snapshot forward fill restoring depth.

### C3 — replay cannot establish receive-time causality

`PairMarket` is explicitly exchange-time-first. It selects the next earliest exchange timestamp across legs; receive time only orders rows at the same exchange timestamp. It then applies all rows at that exchange time before the coordinator acts. This prevents a decision between individual arrivals and cannot prove that consumed market/signal inputs were available at the decision moment.

```text
pair_bundles:
[(2025-01-02 21:00:00, ["P"], [2025-01-02 21:00:02]),
 (2025-01-02 21:00:01, ["S"], [2025-01-02 21:00:00])]

sequence_attributes_present: {"feed_seq": false, "book_seq": false}
```

There is also a single-book audit-clock weakness. `run_date()` sorts the combined data by receive timestamp, but `Market.load_md()` assigns the working `datetime` from the exchange-time DataFrame index. The base strategy's orders, diagnostic events, and message-limit timestamps can therefore be exchange times even when rows were processed in receive order.

```text
processed_(recv_ts, stored_datetime):
[(2025-01-02 09:00:00, 2025-01-02 09:00:01),
 (2025-01-02 09:00:02, 2025-01-02 09:00:00)]
```

The required replacement is receive-time ingress with a deterministic source tie-breaker, run-wide `feed_seq`, per-product `book_seq`, immutable snapshot IDs/hashes, signal availability, and an explicit decision timestamp.

Relevant implementation:

- `common/backtest.py:186-187` — receive-time sort for the single-book driver.
- `common/market.py:117-121` — stored `datetime` comes from exchange-time index.
- `common/market.py:782-958` — exchange-time pair merge.

### C4 — required telemetry and fail-closed invariants do not exist

The research telemetry contract requires run-scoped, schema-versioned tables for decisions, book events/snapshots, orders, fills, hedge executions, trigger evaluations, consumed signals, P&L outcomes, and inventory. Current `Strategy.event_log` and `Strategy.session_record` are optional per-strategy lists. They do not supply run IDs, decision joins, causal sequences, dual-book snapshot identities, complete lifecycle status, capacity reservation, or complete hedge shortfall records.

| Required table | Current support |
| --- | --- |
| `decisions`, `book_events`, `book_snapshots` | Absent |
| `orders`, `fills`, `outcome_pnl` | Partial, ad hoc records only |
| `hedge_executions`, `trigger_evaluations`, `signal_snapshots`, `inventory_series` | Absent |
| `label_outcomes` | Correctly a research/label-layer responsibility, but no engine input identity supports it |

Reporting only compares one-leg summary net PnL to cycle net PnL and prints a warning if the relative difference reaches one percent. It does not reconcile hedge-leg PnL, fees/rebates, inventory, basis attribution, or causal/depth/cap/staleness invariants, and does not fail the run.

Relevant implementation:

- `common/strategy.py:376-428, 446-610` — state and ad hoc events.
- `common/strategy.py:723-761` — optional session records.
- `common/reporting.py:69-78` — warning-only reconciliation.

### C5 — no validated stable dual-book foundation contract

The current Market constructor accepts only `(mult, tick, verbose)`. Pair-v2 behavioural tests construct it with `engine_version="v2"`; all eight fail during harness setup with:

```text
Market.__init__() got an unexpected keyword argument "engine_version"
```

Static inspection identifies two additional stale integration calls:

- the pair strategy calls nonexistent `market.match_batch()`; and
- its driver passes `trading_date` to `run_pair_session()`, whose signature does not accept it.

The eight pair contract/manifest tests pass. Their typed event, cycle, and provenance concepts are useful patterns, but they are not evidence of an executable strategy/engine integration. Calendar-pair entry is deferred by the research registry; S0 should define a new, generic versioned dual-book foundation interface and a neutral conformance client instead of reviving the obsolete pair-entry policy.

## Confirmed high and medium findings

No substantive false positive was found among the consolidated review's remaining findings.

- **Instrument model:** sessions, tick, multiplier, and related configuration are global rather than per product/leg.
- **Capacity and ledger:** no generic worst-case live-order reservation, dual-leg ledger, policy-declared hedge mapping, or residual-risk accounting exists.
- **Policy boundary:** base `Strategy.hedge()` embeds touch-plus-offset aggression; `auto_unwind` and extensive prediction state live in the base class; `Market._build_fill_event()` copies a long uncontrolled list of strategy metadata keys into top-level fill records.
- **Latency and stale data:** no generic decision/submission/arrival/fill scheduler, stress hook, or configurable stale-data invariant exists.
- **Provenance/trials:** generic `grid.py` records config/git state but lacks content hashes, holdout/freeze declaration, and durable artifacts for every candidate/trial. Retired pair-v2 code has better hashing/manifest primitives but is not runnable through this foundation boundary.
- **Signal universe:** `load_signals()` takes `nth(-2)` and `nth(-1)` contracts per day rather than validating a declared instrument universe.
- **Loader cleaning:** `load()` replaces prices at zero-volume levels, potentially creating a misleading executable-looking book.
- **Liquidity assumptions:** `FAK_AVAIL = 0.5` is a module constant. Advanced callers can override per-order `participation`, but there is no first-class versioned execution-model configuration/provenance surface.
- **Cancellation diagnostics:** `bid_and_cancel()` / `ask_and_cancel()` log cancellation before `min_count` filtering. A targeted reproduction logged a cancel although the order remained resting.
- **Memory:** `event_log` and `session_record` grow unbounded in memory.
- **Ownership:** `public_tools/` duplicates Market, Strategy, and Backtest implementations; root documentation also describes an older/different layout, leaving canonical ownership unclear.
- **Fees:** fee treatment exists, but exchange schedules and message fees/rebates are not independently validated.

## What remains worth preserving

The matching/accounting kernel has tested value:

- FIFO queue construction behind displayed depth;
- shared-volume queue decay and price-priority cascades;
- passive partial fills and residual tracking;
- within-bundle targeted aggressive depth sweep with VWAP/per-level output;
- four-way average-cost position transitions; and
- per-contract, per-trading-day order-message tracking.

The required foundation work should preserve those elements while replacing their unreliable surrounding lifecycle and evidence model.

## Recommended S0 acceptance sequence

1. Introduce injected `InstrumentSpec` / calendar / fee configuration. Model product-specific sessions, breaks, EOD, multiplier, tick, roll, and fee rules; distinguish a break from terminal close.
2. Replace direct FAK, sweep FAK, and targeted aggressive execution with one result-bearing depth service. Record requested/filled/residual quantity, consumed levels, executable touch, VWAP, decision-mark shortfall, participation assumptions, and failure disposition.
3. Preserve consumed simulated depth until a genuine book update. Any synthetic depth refresh must be explicit, conservative, configured, recorded, and tested.
4. Replace exchange-time pair merging with receive-time ingress and explicit batch-arrival semantics. Produce immutable decision contexts with `feed_seq`, `book_seq`, book/signal snapshot identities, ages, and `dec_ts`.
5. Add a schema-owned telemetry emitter and fail-closed invariant checker for causality, accounting, stale data, depth reuse, capacity limits, and basis reconciliation.
6. Add generic policy-declared capacity reservation plus a quoted/hedge/residual-risk ledger.
7. Add submission/arrival/execution timestamps, latency/depth/basis stress configurations, partial-fill retries, deadline outcomes, and incomplete-EOD liquidity tests.
8. Retain content-hashed input/config/code artifacts for every development trial; declare and freeze the candidate before holdout.

Only after these capabilities and deterministic acceptance tests exist can an S0 economic result be treated as credible evidence.

## Component validation verdict (2026-08-08)

Follow-up: [maker-hedger_remediation-plan_26-08-08.md](maker-hedger_remediation-plan_26-08-08.md) (component review) and [maker-hedger-s0-operational-remediation-plan_26-08-08.md](maker-hedger-s0-operational-remediation-plan_26-08-08.md) (operational acceptance).

All five critical findings (C1-C5) and every confirmed high/medium finding in this review were remediated at the component level through the phased foundation programme (G0-G7 plus G4c). Each is closed by committed, acceptance-tested component evidence in `common/tests/foundation/` (79/79) alongside the preserved legacy characterization suite (72/72). The foundation API clients do not rely on replaced legacy paths; the remaining `backtest_trading_day()` bridge still uses legacy `Market`/`Strategy` and is not an S0 evidence path.

**Verdict: component/API gate PASS; S0 economic-infrastructure gate NO-GO.** The components are clean, well-tested, and suitable for controlled policy-adapter integration. However, the research-owned acceptance review at `D:\OneDrive\Research\Futures-Maker-Hedger\analysis\backtest-infra-acceptance-review.md` identifies five operational blockers — no production bridge, research-schema field gaps, unverified passive fills, unwired EOD, and no arrival-time execution model — that component tests cannot close. These are addressed by the operational remediation sequence linked above. No S0 economic result may be produced from the current paths until that plan's final gate passes.
