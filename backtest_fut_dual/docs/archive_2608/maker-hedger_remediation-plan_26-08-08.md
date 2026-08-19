# Maker-Hedger Foundation Remediation Plan

**Date:** 2026-08-08  
**Source assessment:** [maker-hedger_review-consolidated_26-08-08.md](maker-hedger_review-consolidated_26-08-08.md)
**Objective:** Make `common/` a credible, reusable S0 foundation for single-instrument passive market making with a correlated-instrument hedge.  
**Implementation status:** Phases 0, 1, 2, 3, 4a, 4b, 5, 4c, and 6 completed on 2026-08-08. Phase 7 now includes the raw snapshot adapter/proxy-evidence bridge alongside the generic strict-loader and stress controls; G7 and S0 readiness remain partial solely because frozen realistic-policy/test-day and disjoint-holdout evidence are still required. This plan was resequenced on 2026-08-08 to make dual-book correctness a constraint from the first implementation phase, split the former G4 scope, and move provenance into G5.

## Governing principles

1. **Close critical defects before comparing S0 economics.** A passing PnL result is not evidence while C1-C5 remain open.
2. **Build the foundation, not an entry strategy.** Reservation price, quote size/width, hedge ratios, target, trigger classes, hysteresis, cooldown, retry policy, and disposition remain policy-layer declarations. `common/` schedules, executes, accounts, validates, and records declared intents.
3. **Use declared availability time for causality.** Exchange time remains analysis data; it is not the default decision clock. For the accepted raw snapshot files, availability is the versioned `max(exchtime, timestamp)` replay convention, not a claim that `timestamp` is a certified receive clock.
4. **Make assumptions explicit.** Participation, latency, synthetic-book refresh, data cleaning, fee schedule, and EOD rules must be configured, versioned, emitted, and stress-tested.
5. **No silent fallback.** Any stale book, incomplete hedge, capacity breach, causal breach, invalid input, or reconciliation breach follows a declared disposition and is visible in telemetry.
6. **Characterize before replacing.** Every confirmed current behaviour that is intentionally retained needs a deterministic test; every corrected defect needs a test that fails on the current implementation.
7. **Dual-book by construction.** From G0 onward, every supported foundation path and acceptance fixture represents a quoted product and a correlated hedge product. Single-product checks may be additional diagnostics, never evidence that a dual-book gate has passed.

### Raw snapshot acceptance addendum (2026-08-10)

`E:\FinData\HFT\ticks` is accepted as the raw market-data contract for a
bounded snapshot-interval queue model: five levels of displayed depth plus
cumulative `totalvol`/`totalvalue`. The dataset is not required to provide a
trade tape, trade IDs, aggressor side, or a certified receive clock. The
supported path must preserve these constraints through a versioned snapshot
adapter and matcher-owned snapshot-proxy evidence type; it must not fabricate
`PassiveTrade` values. Trade-level input is optional calibration evidence, not
a prerequisite for this model's frozen-data or holdout evaluation.

## Target acceptance gates

| Gate | Required outcome | Blocks |
| --- | --- | --- |
| G0 | Canonical ownership, supported runtime, dual-book vocabulary/fixtures, and acceptance-test harness are fixed. | All implementation work |
| G1 | Product-specific calendars preserve trading-day state across breaks and close only at declared EOD, for both products. | S0 replay |
| G2 | Declared-availability ingress produces causally valid, reconstructible decision contexts. | Any decision/fill economics |
| G3 | One depth-consuming aggressive execution service reports realistic prices, residuals, and levels; depth cannot be reused without a declared refresh. | Hedge/EOD cost claims |
| G4a | Generic capacity reservation and lifecycle state transitions constrain every live order. | Capacity/cancel/retry claims |
| G4b | Dual-leg fills/positions and depth-realistic EOD completion are recorded with explicit residual risk. | Inventory/EOD claims |
| G5 | Versioned telemetry, fail-closed generic invariants, and immutable per-trial provenance prove each run. | Any S0 or diagnostic economics |
| G4c | Non-overlapping PnL attribution and reconciliation are computed from the dual-leg ledger and canonical telemetry. | PnL/basis claims |
| G6 | A small generic dual-book conformance client passes against the published foundation API. | A policy integration |
| G7 | Loader hardening, declared stress replay, and operational cleanup meet the remaining experiment contract. | S0 comparison/holdout |

No gate may be waived by documenting an assumption in a strategy. If an assumption is needed, it must instead be represented by foundation configuration, telemetry, and acceptance tests.

## Optional pre-S0 diagnostic

A single-instrument passive-making baseline may be run for early engineering feedback only after its applicable G1, G2, G3, and G5 checks pass. It must be labelled **pre-S0 diagnostic**, retain canonical telemetry and provenance, and remain ineligible for promotion, holdout comparison, or any claim about executable hedging. It cannot replace G4a, G4b, G4c, or G6, and it must not be called an S0 result.

## Phase 0 - Freeze the boundary and establish the test baseline (G0)

**Priority:** Critical prerequisite  
**Review coverage:** C4, C5, H4, H7, duplicate ownership, runtime-policy discrepancy.
**Status:** Complete (2026-08-08), including G0.1 dual-book and G0.2 snapshot/execution-model contract hardening. See [maker-hedger-foundation-boundary_26-08-08.md](maker-hedger-foundation-boundary_26-08-08.md).

### Work

1. Declare `common/` the canonical reusable implementation. Record the supported status of `public_tools/` (deprecated compatibility layer, maintained adapter, or separately owned engine) before sharing fixes between them.
2. Choose and document one supported Python version. Align `pyproject.toml`, test documentation, and CI/runtime commands; preserve a compatibility matrix only if supporting more than one version is deliberate.
3. Freeze a minimal, versioned foundation contract vocabulary:
   - `InstrumentSpec`, `SessionCalendar`, `ExecutionModelConfig`;
   - quoted-product and hedge-product identities, hedge-mapping version, immutable snapshot reference, ingress event, `DecisionContext`, `OrderIntent`, and `ExecutionResult`;
   - capacity reservation and ledger events;
   - telemetry schema version and invariant-result type.
4. Record the minimum dual-book API constraint: all decision, intent, execution, telemetry, and acceptance-test contracts identify both products and do not expose mutable book state to policy code. This is a design constraint now; the runnable conformance client remains G6.
   - `OrderIntent` explicitly selects either the run-default execution model or a named/versioned execution-model reference; it cannot carry a raw participation override.
5. Create a dedicated foundation acceptance-test suite separate from legacy strategy tests. Use synthetic dual-book fixtures with independently controlled `exchange_ts`, `recv_ts`, source sequence, snapshots, depth, signals, and action-arrival times.
6. Mark the old pair-v2 behavioural suite as unsupported until it is either adapted to the published API or explicitly archived. Preserve its contract/manifest tests as reference material, not acceptance evidence. **Disposition in Phase 6: archive in place; exclude it from supported commands.**
7. Add baseline/characterization tests for every confirmed current defect so the migration can prove both the old and intended behaviours without ambiguity.

### Exit evidence

- A written ownership/runtime decision is checked into the repository.
- One documented command runs all common and new foundation acceptance tests deterministically.
- The API vocabulary and telemetry version have explicit ownership and compatibility rules, including the two-product design constraint.
- A `HedgePairRef` binds every dual-book decision and downstream Phase-0 intent/execution/reservation/ledger vocabulary to distinct products and a declared mapping version.
- `BookSnapshotRef` exposes only a product, sequence, ID, and hash; no policy-facing context carries mutable depth or a raw book object.
- Legacy pair tests cannot silently appear as a passing supported suite.

## Phase 1 - Product specification and session lifecycle (G1)

**Priority:** Critical - C1
**Dependencies:** G0
**Status:** Complete (2026-08-08). Product-aware replay is provided by `Backtest.backtest_trading_day()`; legacy split APIs are explicitly compatibility-only.

### Work

1. Implement injected immutable `InstrumentSpec` objects containing product/contract identifier, multiplier, tick, timezone, calendar, fee-model reference, roll mapping reference, and top-K/depth requirements.
2. Implement a per-product `SessionCalendar` that distinguishes valid trading windows, intra-day breaks, trading-day boundaries, declared EOD, holidays/early closes, and roll transitions.
3. Replace global `NIGHT_SESSION`, `DAY_SESSION`, and `run_date()` split semantics with a scheduler that preserves market, order, strategy-adapter, ledger, and reservation state across an intra-day break.
4. Treat EOD as a calendar event, not as a side effect of finishing an input DataFrame. EOD invokes the generic execution/lifecycle service defined in later phases; until then its API must be represented but not simulated at touch.
5. Move or wrap global tick/multiplier assumptions so each quoted/hedge product has its own declared values.

### Required deterministic tests

- Zn: 21:00-01:00 plus following-day sessions share one trading day; position and live order state survive the 01:00 break.
- T: data through 15:15 is processed; the calendar never truncates it at 15:00.
- An actual EOD is distinct from a break and invokes one declared close-out lifecycle.
- Two instruments with different ticks/multipliers can coexist in one run without cross-contamination.
- Calendar holiday, early-close, and missing-data behaviour follows an explicit disposition.

### Exit evidence

- No reset/stop occurs solely because of an intra-day break.
- All timestamped actions carry the product/session/trading-day identity supplied by the calendar.
- The legacy global calendar path is removed or explicitly deprecated with no supported caller relying on it.

## Phase 2 - Causal ingress, state reconstruction, and decision contexts (G2)

**Priority:** Critical - C3
**Dependencies:** G0; designed to consume `InstrumentSpec` from G1
**Status:** Complete for generic ingress and the accepted raw snapshot adaptation. `CausalIngress` is the supported availability-time foundation path; `PairMarket` is explicitly legacy exchange-time compatibility replay. `RawSnapshotAdapterConfig` supplies the Gate-7 raw source adaptation and sends only separately typed snapshot intervals downstream.

### Work

1. Define one ingress event shape with `product`, `exchange_ts`, `recv_ts`, explicit source sequence/tie-breaker, and book or signal payload. For the accepted raw snapshot source, retain `exchtime`, `timestamp`, content-hashed file identity, and file-row ordinal; derive `recv_ts = max(exchtime, timestamp)` and a run-global, strictly monotone `source_seq` from the declared merge order `(recv_ts, file identity, row ordinal)`.
2. Sort replay ingress by `(recv_ts, source_tie_breaker)`, assign monotone run-wide `feed_seq`, and increment a product's `book_seq` only when that product's book changes.
3. Supersede `PairMarket.step_pair()` for supported foundation replay with declared-availability-time processing; retain that class only as explicitly unsupported legacy exchange-time compatibility code. If a source provides atomic bundles, model the bundle explicitly and set the decision instant no earlier than that bundle's declared arrival.
4. Produce immutable top-K book snapshots/hashes at run start, recovery, and material decisions. Retain both quoted and hedge book identities in every `DecisionContext`.
5. Integrate signal availability as ingress data. A consumed signal must have `available_at <= dec_ts`; future labels may never enter a decision context.
6. Separate replay processing time from exchange time in base order/event/message-limit APIs. Eliminate the current `datetime` overloading that changes a receive-ordered event back into an exchange-time audit record.

### Required deterministic tests

- An earlier exchange timestamp received later cannot influence an earlier decision.
- Equal availability timestamps use a stable documented source tie-breaker.
- Every decision can reconstruct both books and all consumed signals from sequences/snapshots.
- A policy cannot access a later signal, book event, or derived value.
- Single-book and dual-book paths use the same timestamp/sequence semantics.
- Atomic source bundles are causal only at their declared availability time.
- The raw snapshot adapter preserves its two raw clocks and accepts a row whose
  `timestamp < exchtime` through the declared `max` availability convention.
- A multi-file raw episode with identical availability times has a unique,
  strictly monotone `source_seq` and retains each file/row identity.

### Exit evidence

- `feed_seq`, `book_seq`, snapshot IDs/hashes, input ages, `recv_ts`, and `dec_ts` are available before an intent is evaluated.
- No supported replay path defaults to exchange time as its decision or audit clock.
- Raw snapshot provenance records the availability-convention version and
  source row sequence; it does not claim measured receive latency.

## Phase 3 - Unified aggressive execution and book-depth semantics (G3)

**Priority:** Critical - C2
**Dependencies:** G1, G2
**Status:** Complete (2026-08-08). `DepthExecutionService` is the supported foundation path for registered aggressive HEDGE/EOD intents. Historical `Market.fak()`, `_fak_sweep()`, and targeted matching remain explicit legacy compatibility behaviour and are not G3 acceptance evidence.

### Work

1. Replace `fak()`, `_fak_sweep()`, and targeted aggressive execution with one execution service driven by an explicit `ExecutionModelConfig`.
2. Return an immutable `ExecutionResult` containing requested, filled, residual, rejection reason/status, per-level consumption, executable touch, VWAP, limit, participation assumption, decision mid, cost versus decision mid, execution timestamps/sequences, and the resolved execution-model identity. Define and enforce the status-to-quantity table (for example, full fill versus partial versus zero-fill terminal dispositions); reject inconsistent combinations.
3. Mutate the active book state exactly once per consumed level. Do not restore simulator-consumed depth without a genuine book update or a separately declared replenishment model.
4. Define an explicit sparse-book policy. The default must not infer unobserved replenishment from an unrelated-leg update. If a depth-refresh model is later permitted, it must be conservative, named, parameterized, versioned, and reported.
5. Make marketability, tick normalization, zero/invalid depth, and partial fills consistent across buy/sell, FAK, crossing limit, hedge, and EOD routes.
6. Move participation from the `FAK_AVAIL` module constant to configuration. Resolve `OrderIntent.execution_model_ref` against the run configuration; `None` means the declared run default. Allow named per-intent model selection only when the model is configured, recorded, and validated. Never accept a raw participation-rate override in an intent.
7. Register accepted intents and cross-check every execution request/result against its run, decision, pair, and product before it mutates a book. G4a extends this registry into the complete lifecycle.

### Required deterministic tests

- An off-touch marketable buy/sell reports touch/VWAP, not submitted limit.
- Every consumed level decreases exactly by its consumed amount; repeated execution sees remaining depth only.
- Two aggressive orders in one decision context cannot double-consume a level.
- A P-only bundle cannot restore S depth after an S sweep; the reproduced 6-filled-from-5 scenario fails.
- Partial, empty, gapped, crossed, zero-depth, and limit-rejected paths produce explicit residual/disposition values.
- Participation changes are visible in execution results and run provenance.
- An execution result with an unknown/mismatched intent, pair, product, or execution-model reference is rejected before any depth is consumed.

### Exit evidence

- One tested service is the sole supported path for aggressive hedge and EOD execution.
- No test or caller relies on standalone FAK's old price/depth semantics.
- Liquidity fabrication invariant fails closed.

## Phase 4a - Capacity reservation and intent lifecycle (G4a)

**Priority:** Critical/high - H2, H6
**Dependencies:** G1-G3
**Status:** Complete (2026-08-08). `IntentLifecycleService` is the supported registry for intent transitions and live-maker capacity reservations; legacy strategy/market mutations are compatibility-only and not G4a evidence.

### Work

1. Implement generic worst-case capacity reservation for live maker orders. Policy supplies its cap/envelope; foundation calculates, reserves, and releases capacity from lifecycle events.
2. Define generic intent/action lifecycle states: submitted, arrived, partially filled, filled, cancelled, expired, rejected, stale, deadline, and failed.
3. Bind reservation changes to lifecycle transitions so a direct book mutation or an incomplete transition cannot bypass the capacity envelope.
4. Define the declared terminal disposition for expiry, stale data, deadline, cancellation, and reservation rejection. Do not embed retry or hedge policy in the foundation.
5. Make lifecycle transitions resolve the registered intent, decision, pair, product, and execution-model reference rather than trusting duplicated fields supplied by a caller.

### Required deterministic tests

- Worst-case fills from all live maker orders cannot exceed a declared cap.
- Reservation is released only on the correct partial/full/cancel/expiry lifecycle event.
- Invalid lifecycle transitions and reservation underflow/overflow fail closed.
- Each terminal lifecycle state records one declared disposition and cannot be silently retried.
- A reservation, fill, or execution cannot be attached to a different intent/product/pair than the registered lifecycle record.

### Exit evidence

- All live maker orders consume generic reservation capacity for their worst-case declared envelope.
- Every supported intent reaches a reconstructible lifecycle state; policy cannot bypass reservation through direct book mutation.

## Phase 4b - Dual-leg ledger and EOD completion (G4b)

**Priority:** Critical/high - C1 completion, H8
**Dependencies:** G3, G4a
**Status:** Complete (2026-08-08). `DualLegLedger` derives dual-leg inventory and fill costs from lifecycle/execution records, and `EodCompletionService` cancels live intents then closes actual inventory only through `DepthExecutionService`.

### Work

1. Implement a dual-leg ledger for quoted position `q`, hedge position `h`, policy-declared hedge mapping/version, risk-scaled residual exposure, fills, fees/rebates, and pending hedge quantity.
2. Record ledger effects from lifecycle and execution events, not policy-local mutable state. The ledger must preserve both product identities and mapping version for every effect.
3. Implement EOD using the Phase 3 execution service. It must cancel incompatible live orders, execute the declared close intent through available depth, preserve residual risk, and emit a declared incomplete-liquidity outcome rather than a synthetic touch fill.
4. Keep PnL attribution out of this phase: it belongs to G4c after the canonical telemetry/provenance gate is available.

### Required deterministic tests

- A policy-declared hedge mapping yields deterministic `q`, `h`, pending hedge quantity, and residual-risk updates.
- Partial maker, hedge, and EOD execution retain residual position and report the terminal disposition.
- EOD does not manufacture liquidity, double-close an order, or hide residual risk.
- Fills, fees/rebates, positions, and EOD result reconcile to ledger events within a declared quantity tolerance.

### Exit evidence

- Dual-leg position, residual risk, and EOD results are reconstructible from ledger and lifecycle events.
- Every EOD fill follows the G3 execution service and any residual reaches an explicit terminal outcome.

## Phase 5 - Telemetry, invariants, and provenance (G5)

**Priority:** Critical - C4, H4
**Dependencies:** G2, G3, G4a, G4b
**Status:** Complete (2026-08-08). `TelemetryEmitter` streams canonical JSONL and immutable snapshot/provenance artifacts; `TelemetryInvariantChecker` produces the fail-closed run eligibility result. G4c extends `outcome_pnl` with a canonical reconciled waterfall.

### Work

1. Implement a run-scoped, schema-versioned emitter for the experiment-contract subset: `decisions`, `book_events`, `book_snapshots`, `orders`, `fills`, `hedge_executions`, `trigger_evaluations`, consumed `signal_snapshots`, `outcome_pnl`, and `inventory_series`. G4c will populate the final attributed PnL/reconciliation fields; no placeholder may be presented as reconciled PnL. Snapshot-mode fills must identify their source interval and proxy-model version rather than a fictitious observed trade.
2. Give all entities stable run-scoped IDs and parent joins. Include both quoted and hedge products in every applicable table.
3. Stream or checkpoint telemetry rather than retaining unbounded Python lists. This supersedes Phase 2's deliberately uncapped in-memory causal snapshot cache: define durable hash-addressed snapshot retrieval, cache retention, artifact paths, and completion/flush semantics. A latest-N in-memory cache without durable retrieval does not satisfy reconstruction.
4. Implement generic fail-closed checks for causal availability, snapshot reconstruction, invalid/stale book use, depth reuse, declared capacity, lifecycle consistency, and ledger quantity consistency. Add PnL/basis reconciliation checks in G4c.
5. Create immutable per-trial provenance that hashes market data, signal data, configuration, code, schema, fee profile, instrument/roll mapping, and execution-model configuration. Retain a durable artifact set for every development candidate, not only the winner.
6. Record development/calibration/holdout windows, candidate-freeze decision, policy/mapping/model versions, and declared data-cleaning transformations.
7. Make reporting consume canonical telemetry rather than strategy-local record shapes. A failed invariant must make the run ineligible for economics comparison.

### Required deterministic tests

- Every material decision joins to both book snapshots, sequences, products, and consumed signals.
- Missing/late signal, stale book, double-consumed depth, cap breach, orphan fill, and invalid lifecycle/ledger event each fail a run with a machine-readable reason.
- Every order reaches a final lifecycle status and every fill joins to exactly one order/decision.
- Changing any hashed input/config/code artifact changes run provenance; every trial retains its own immutable artifact set.
- Telemetry from a large synthetic run streams/checkpoints without unbounded in-memory growth.

### Exit evidence

- The S0-required tables in `contracts/telemetry_schema.md` are emitted and schema-validated.
- Each run has immutable, complete provenance before economics are considered.
- An invariant breach cannot produce a result that appears as a valid S0 or pre-S0 diagnostic run.

## Phase 4c - PnL attribution and reconciliation (G4c)

**Priority:** Critical/high - H8
**Dependencies:** G4a, G4b, G5
**Status:** Complete (2026-08-08). `PnlAttributionService` derives non-overlapping price/cost effects from immutable ledger events, reconciles them to canonical fills, independent accounting/cycle totals, and EOD completion, then `TelemetryEmitter` records the economics-eligibility verdict.

### Work

1. Build non-overlapping PnL attribution from dual-leg ledger events: maker capture, quoted-leg price PnL, hedge-leg price PnL, hedge execution shortfall, fees/rebates, and reconciliation residual.
2. Treat residual-basis PnL as an attribution of combined price PnL, not an additional waterfall item.
3. Reconcile ledger, fills, fees, PnL waterfall, cycle/accounting views, telemetry, and EOD result to declared tolerances. A breach must become a G5-style ineligible run result.

### Required deterministic tests

- The PnL waterfall sums exactly to the independently calculated total within declared tolerance.
- Every fill and fee/rebate has one ledger and attribution effect; no effect appears in two PnL categories.
- Residual basis is reported without double counting combined-leg price PnL.
- A deliberately injected reconciliation residual makes the run ineligible.

### Exit evidence

- Inventory, execution shortfall, fees, PnL waterfall, and EOD accounting reconcile from canonical events.
- No S0 result contains an unreconciled PnL or basis claim.

## Phase 6 - Versioned dual-book foundation API and conformance client (G6)

**Priority:** Critical - C5
**Dependencies:** G1-G5, G4c
**Status:** Complete (2026-08-08). `DualBookFoundation` debuted as v1.0.0 and owns retained snapshot-derived depth, lifecycle/capacity, execution, ledger, EOD, and telemetry; `ConformanceClient` validates the public-only two-leg flow, and a dedicated facade test covers retained-empty-book EOD `INCOMPLETE_LIQUIDITY`. Legacy pair-v2 remains archived reference code and is not adapted or invoked by supported acceptance commands.

### Work

1. Publish the deliberately small versioned interface constrained in G0 for a generic quoted-leg maker intent plus aggressive correlated-hedge intent. It consumes `DecisionContext` and emits validated intents/results; it does not expose mutable books to policy code. **Completed:** `MakerHedgeIntentBatch` plus `DualBookFoundation` v1.0.0.
2. Build a test-only conformance client, not a revived calendar-pair entry strategy. It must exercise passive placement, partial maker fill, immediate/partial hedge, residual handling, cancellation, EOD, and dual-leg telemetry. **Completed:** `tests/foundation/conformance_client.py` uses only public facade methods.
3. Decide one explicit disposition for the legacy pair-v2 code: adapt to this interface as a non-S0 example, archive it, or remove it from supported test commands. Do not allow it to define the new API accidentally. **Completed:** archive in place as historical/reference code; it is excluded from supported commands.
4. Remove unsupported constructor/signature assumptions and invalid calls such as `engine_version` on `Market`, `match_batch`, and incompatible `run_pair_session` arguments only as part of the chosen disposition. **Not applicable to the archive disposition:** those historical assumptions remain unexecuted reference material rather than being silently made part of the new API.

### Required deterministic tests

- The conformance client passes against the public foundation API without implementation-private access.
- A client cannot mutate a book, fabricate a fill, or bypass reservation/ledger/emitter paths.
- A client cannot attach an execution, reservation, or ledger event to a mismatched registered intent, product, pair, or execution-model reference.
- One test covers each dual-leg lifecycle transition and one covers each final failure disposition.
- A retained empty-book EOD close through the facade reports `INCOMPLETE_LIQUIDITY`, its zero-fill execution disposition, and residual dual-leg inventory.

### Exit evidence

- A versioned dual-book contract is executable and independently tested.
- Legacy pair entry is clearly supported, adapted, or retired; it is not ambiguous.

## Phase 7 - Loader hardening, stress replay, and operational cleanup (G7)

**Priority:** High - H5 and medium findings
**Dependencies:** G1-G6
**Status:** Partial. `foundation_loader.py` supplies strict ingress-ready book validation plus `RawSnapshotAdapterConfig`; signal selection requires an explicit declared universe; `stress.py` supplies versioned, composable stress controls; and `DualBookFoundation` emits/hashes an attached stress scenario. Snapshot interval evidence is separately matcher-issued and exported through canonical/research telemetry. G7 remains open only for frozen realistic-policy/test-day and disjoint-holdout evidence. Historical loader cleaning, grid manifests, and strategy-local prediction state remain compatibility-only and are excluded from S0 evidence.

### Work

1. Replace silent loader assumptions with validation: declared contract universe, active-contract selection, timestamp validity, monotone/valid books, non-negative depth, volume resets/corrections, zero-depth semantics, and explicit missing-data disposition. **Completed for canonical input:** `MarketDataValidationConfig` and `validate_market_data()` reject or return explicit dropped-row dispositions, and `load_signals()` requires `CONTRACT_UNIVERSE` plus an explicit schedule for a universe larger than two. **Completed raw snapshot bridge:** `RawSnapshotAdapterConfig` declares source mappings, `max(exchtime, timestamp)` availability, content-hashed raw file/row identity, a run-global source-sequence merge rule, five-level retention, tick/multiplier tables, and declared cumulative-flow conversion rather than treating these as legacy cleaning.
2. Add configurable latency and stress hooks for market-data receive delay, signal availability delay, action submission/arrival delay, depth/participation, fees, basis, volatility, and opening-session conditions. **Completed:** versioned `StressScenario` and its deterministic ingress, timing, execution-model, fee, basis, volatility, and opening-session hooks.
3. Ensure each stress changes only its declared model dimension and is recorded in the already-established G5 provenance and telemetry. **Completed:** participation produces a new model version; `DualBookFoundation` records its scenario in trigger telemetry and automatically adds `stress_scenario` to the hashed provenance artifacts.
4. Remove policy-specific prediction state/metadata from base infrastructure or move it into a namespaced strategy payload. Keep a documented stable foundation schema. **Completed at the foundation boundary:** only immutable `OrderIntent.strategy_metadata` remains policy namespaced; the historical `Strategy` prediction fields and `Market` fill metadata are compatibility-only and are neither accepted nor emitted by the S0 API.
5. Consolidate ownership/documentation, deprecate duplicate implementations intentionally, and align README/test documentation with the canonical engine. **Completed:** this boundary distinguishes supported `foundation_loader`/`foundation_api` paths from archived/compatibility paths (`backtest.load`, grid artifacts, old pair-v2, and `public_tools/`).

### Required deterministic tests

- A missing or mismatched declared universe fails clearly rather than selecting `nth(-2)`/`nth(-1)` silently.
- Invalid/crossed/zero-depth/corrected input reaches an explicit validation outcome.
- Each latency/depth/fee/basis stress changes only its declared model dimension and is recorded in output provenance.
- A frozen candidate cannot be confused with a later development configuration.

### Exit evidence

- Every supported loader reports a deterministic validation outcome for malformed or incomplete input.
- S0 economics are reported for base and declared stress scenarios only after all invariant gates pass.

## Final S0 readiness review

S0 may begin only when G0, G1, G2, G3, G4a, G4b, G5, G4c, G6, and G7 all pass. The readiness review must demonstrate:

- instrument-correct session/EOD behaviour;
- causal declared-availability-time dual-book replay and signal availability;
- depth-consuming hedge and close-out execution with explicit residuals;
- declared capacity reservation, hedge mapping, inventory, and basis ledger;
- complete versioned telemetry, immutable provenance, and passing fail-closed invariants;
- reconciled PnL attribution;
- generic dual-book conformance evidence; and
- complete trial retention and base/stress results.

The review should reject any result that bypasses these gates, even if it has positive PnL.

### Component-review verdict (2026-08-08)

The consolidated findings in [maker-hedger_review-consolidated_26-08-08.md](maker-hedger_review-consolidated_26-08-08.md) were re-mapped against the completed phases and the committed working tree (`HEAD` `1de79ff9`, clean):

- Every C1-C5 critical finding and every confirmed high/medium finding maps to committed, acceptance-tested remediation evidence at the component level.
- The generic component controls then implemented across G0-G7 plus G4c were demonstrated by the foundation suite (79/79) alongside the preserved legacy characterization suite (72/72). The later accepted raw snapshot bridge is not included in that historical result.

**Verdict: component/API gate PASS; S0 economic-infrastructure gate NO-GO.**

The generic G0-G7 work produced clean, well-tested foundation components and the
component-level remediation is complete. However, the research-owned acceptance
review at `D:\OneDrive\Research\Futures-Maker-Hedger\analysis\backtest-infra-acceptance-review.md`
identifies five S0 blockers that component tests alone cannot close: no
production bridge connects the loader, ingress, facade, telemetry, and PnL
attribution; the research-owned `contracts/telemetry_schema.md` requires
materially more field-level evidence; passive fills are caller-asserted rather
than match-derived; calendar EOD is not wired into the trading-day lifecycle;
and arrival-time execution is not modelled.

These are addressed by the operational remediation sequence in
[maker-hedger-s0-operational-remediation-plan_26-08-08.md](maker-hedger-s0-operational-remediation-plan_26-08-08.md),
which supersedes the local readiness conclusion. No S0 economic result, stress
comparison, holdout evaluation, or promotion claim may be produced from the
current paths until that plan's final gate passes.
