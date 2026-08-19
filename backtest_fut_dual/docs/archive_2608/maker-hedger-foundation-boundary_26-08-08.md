# Maker-Hedger Foundation Boundary and Compatibility Policy

**Effective:** 2026-08-08  
**Raw-snapshot addendum:** 2026-08-10
**Scope:** the maker-hedger remediation programme only.

## Canonical ownership

`common/` is the sole canonical reusable foundation for the maker-hedger programme. New S0 foundation work, acceptance tests, contract vocabulary, and documentation belong here.

`public_tools/` is a **frozen legacy compatibility implementation** for existing example strategies. It is not an adapter to `common/`, it is not S0-accepted, and no maker-hedger feature/fix is copied into it during this programme without a separately approved migration plan. It is intentionally retained so historic examples are not broken by Phase-0 ownership clarification.

`examples/legacy/arb/` and `examples/legacy/taker/` are historical/example callers. They are not acceptance evidence for the new S0 foundation. `examples/foundation_taker/` is a runnable production-replay example, but its synthetic demonstration inputs are operational-only and are not promotion evidence.

## Supported runtime

The supported Phase-0 foundation verification runtime is **CPython 3.10.13** (`py310`). The packaging metadata and lockfile use the same Python constraint. Tests must run with bytecode generation disabled:

```powershell
$env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run_acceptance
```

The full application dependency environment is not established by this policy; its dependency resolution is separately captured by `uv.lock`. A later version change is a deliberate compatibility decision, requiring a lockfile refresh and rerun of the acceptance command.

## Foundation contract compatibility

`common.foundation_contracts` owns the Phase-0 vocabulary and declares:

- `FOUNDATION_CONTRACT_VERSION = "0.13.0"`;
- `TELEMETRY_SCHEMA_VERSION = "0.5.0"`; and
- immutable configuration, ingress, decision, order-intent, execution-result, reservation, ledger, telemetry, and invariant types.

Phase 2 through Phase 4c, plus the Phase-6 batch vocabulary, are breaking 0.x-minor vocabulary revisions: snapshot identities retain event/feed/receive/availability provenance; decision contexts retain consumed signal identities; execution results retain side, resolved execution model/participation, decision/execution sequences, source book snapshot, and terminal disposition; capacity/lifecycle contracts retain their declared envelope and terminal state; ledger/EOD contracts retain a declared mapping, fill costs, inventory, and residual outcome; PnL contracts retain price observations, non-overlapping effects, and reconciliation verdicts; and `MakerHedgeIntentBatch` binds S0 maker/hedge declarations to their capacity envelope. `Market.load_md()` also uses receive time for dataframe-backed audit clocks while preserving the index as `exchange_ts`. Breaking vocabulary changes advance the 0.x minor version and require migration evidence; additive corrections advance the patch version.

### Dual-book constraint

`HedgePairRef` is the mandatory Phase-0 identity for a quoted product, a distinct hedge product, and the declared hedge-mapping ID/version. `DecisionContext` validates its quoted/hedge fields against that reference. Order intents, execution results, capacity reservations, and ledger events carry the same reference and run/decision identifiers, so they remain traceable to one dual-book decision.

`BookSnapshotRef` passes a product, product-local `book_seq`, run-wide `feed_seq`, source event ID, receive/availability times, immutable snapshot ID, and immutable snapshot hash to policy code; it never carries book depth, a DataFrame, or a mutable snapshot container. `SignalSnapshotRef` does the equivalent for every signal actually consumed by a decision. The Phase-5 telemetry emitter records the canonical dual-book identity fields in durable, schema-versioned artifacts.

`OrderIntent.execution_model_ref = None` explicitly means use the run's declared `ExecutionModelConfig`; an explicit `ExecutionModelRef` selects a named/versioned model only. An intent cannot embed a raw participation-rate override. G3/G4a must validate each reference against the run configuration and intent registry, including intent/product/pair consistency. G6 verifies that enforcement through the conformance client; it is not the first enforcement point.

The `MAKER -> quoted product` and `HEDGE -> hedge product` bindings are the declared S0 maker-hedger scope, not universal multi-instrument constraints. A future passive hedge-leg policy requires a deliberate contract revision. G6 supplies the runnable conformance client.

## Versioned public dual-book API

`common.foundation_api.DualBookFoundation` is the published S0 adapter. A `MakerHedgePolicy` receives only an immutable `DecisionContext` and returns a `MakerHedgeIntentBatch` containing an optional quoted-leg `MAKER` intent, optional hedge-leg `HEDGE` intent, and the maker capacity-envelope ID. An empty batch is an explicit no-action decision. The facade owns lifecycle/capacity registration, aggressive execution, ledger effects, EOD completion, canonical telemetry, and provenance. It exposes no public route for a policy to attach an `ExecutionResult`, create a reservation, or write a ledger effect.

`common.production_replay.ProductionReplayAdapter` is the sole supported S0
evidence runner. It composes the strict loader, `CausalIngress`, the public
facade, calendar EOD, PnL attribution, and the research telemetry export. It
does not import `Market`, `Strategy`, `Backtest`, or `public_tools`; no legacy
command can produce an artifact labelled as S0 evidence.

Market connectors must persist a `BookSnapshotRef` and its payload before it can be used. `ingest_depth_from_snapshot()` reconstructs the hash-checked payload and derives mutable depth internally from the canonical `bids`/`asks` level shape. Therefore a policy cannot receive or mutate a `DepthBook`, and successful execution is bound both to its decision snapshot and to retained snapshot content. The direct `DepthExecutionService` remains the lower-level component boundary; it is not the public policy interface.

`tests/foundation/conformance_client.py` is a test-only consumer of this public API. It is not a calendar-pair entry strategy or a supported production policy. It exercises passive placement, partial maker fill, full and partial hedge fills, residual cancellation, EOD close-out, and canonical dual-leg telemetry. The Phase-6 API suite separately exercises a retained empty-book EOD path, preserving its `NO_LIQUIDITY` execution, `FAILED` lifecycle, explicit residual inventory, and `INCOMPLETE_LIQUIDITY` completion.

## Product calendars and lifecycle

Phase 1 extends `SessionCalendar` with immutable trading windows, trading-day rollover, declared EOD, holidays, early closes, and an explicit reject/drop disposition for out-of-calendar data. Each `InstrumentSpec` injects that calendar plus its own tick and multiplier.

`Backtest.backtest_trading_day()` is a frozen compatibility replay path for
historic callers. It initializes each strategy once for the trading day,
preserves state across breaks, and emits session-break/EOD lifecycle events,
but it remains coupled to legacy `Market`/`Strategy` state. It is not an S0
runner and must not be used for S0 economics, stress, holdout, or promotion
evidence.

`Backtest.backtest()`, `backtest.load(..., calendar=None)`, `NIGHT_SESSION`, and `DAY_SESSION` remain deprecated compatibility paths for historic callers. `backtest.run_date()` now uses the continuous calendar path.

When `run_date()` has no injected `InstrumentSpec`, it uses `DEFAULT_SESSION_CALENDAR` (EOD 15:00, reject disposition). A legacy file containing a timestamp after that EOD now fails clearly instead of being silently truncated; callers must inject the real per-product calendar to process a different close.

## Exchange-batch ingress

`common.ingress.CausalIngress` is the supported Phase-2 foundation replay
ingress. It seals exchange-published batches, assigns a monotone run-wide
`feed_seq`, increments a product's `book_seq` only for that product's book
snapshots, and constructs only atomic, aligned dual-book `DecisionContext`
values. Receive time and source sequence are retained provenance, never a
decision-ordering route.

An `IngressEvent` must have `recv_ts >= exchange_ts`, but a production batch
must contain exactly one quoted and one hedge snapshot. The source may identify
the batch explicitly with `exchange_batch_id`; multiple batches sharing an
exchange timestamp require distinct `exchange_batch_seq` values. A partial
batch, a duplicate product snapshot, or ambiguous same-time ordering fails
closed.

For the accepted `E:\FinData\HFT\ticks` snapshot contract, the source has
`exchtime`, `timestamp`, five levels of book depth, and cumulative
`totalvol`/`totalvalue`, but no certified receive clock. The supported
`RawSnapshotAdapterConfig` retains both raw times as provenance, derives the
exchange batch from `exchtime`, and orders source rows by resolved exchange
batch then contract/file/row identity. It retains the content-hashed file
identity and file-row ordinal. This is not a measurement or certification of
physical receive latency. The raw fields, mapping, batch identity, and source
file hash are retained in provenance.

`PairMarket.step_pair()` remains a legacy exchange-time compatibility implementation. It is not a supported maker-hedger foundation replay path and cannot satisfy G2 evidence. `Market.load_md()` preserves a dataframe index as `exchange_ts`, records `recv_ts`, and uses receive time for its historical `datetime` audit field. In a legacy dataframe with no `recv_ts`, its `timestamp` column is *assumed* to be receive time; that is a compatibility convention, not evidence of a true receive clock. For the accepted raw snapshot files, use the `max(exchtime, timestamp)` convention above rather than treating `timestamp` as an observed receive time. Direct `Market.load_md()` callers must supply rows already sorted by their declared availability convention. Callers use `CausalIngress` when they need Phase-2 sequencing/reconstruction guarantees.

`CausalIngress` retains snapshots in process memory for the active replay. `TelemetryEmitter` is the Phase-5 durability boundary: it persists every emitted snapshot by content hash under its run artifact directory, atomically, and reconstructs it by hash on demand. It streams telemetry rows directly to JSONL and uses an on-disk validation index at finalization, so it does not retain an unbounded event-row history in Python memory.

## Strict loading and declared active contracts

`common.foundation_loader` is the supported Phase-7 market-data boundary. `MarketDataValidationConfig` declares the complete contract universe, source timezone (when raw timestamps are naïve), top-K level count, cumulative-volume columns, optional exchange-batch ID/sequence columns, and independent `reject`/`drop` dispositions for missing data, invalid books, zero top depth, and cumulative-volume corrections. `validate_market_data()` validates receive/exchange clock sanity and source provenance, declared membership, exchange-batch consistency, positive/non-crossed ordered prices, non-negative integer depth, explicit zero-depth handling, and non-decreasing per-contract cumulative totals. It returns every permitted drop as a `LoaderValidationIssue`; it does not fill an empty level with an executable-looking price. `ValidatedMarketData.to_ingress_events()` is the bridge into `CausalIngress` and emits the canonical `bids`/`asks` payload shape required by the public API.

The raw snapshot adapter belongs at the supported-loader boundary, not in
legacy cleaning. `RawSnapshotAdapterConfig` maps declared sources to contracts,
declares the proxy-interval contracts, timezone/tick/multiplier coverage,
retains five levels, derives the availability proxy and globally unique source
sequence while retaining raw file/row identity, and content-hashes file content
when read through `read_raw_snapshot_market_data()`. It derives cumulative-flow
intervals only for the declared proxy contracts (bound to the quoted product in
the supported single-pair replay), and rejects by default any allocated bucket
outside the current retained positive-depth five-level envelope. It never
represents those intervals as observed
`PassiveTrade` values. The separately named matcher-owned
`SnapshotIntervalQueueProxyEvidence` type carries the raw-fill model into
canonical and research telemetry; realistic episode and holdout evidence still
remain separate Gate-7 requirements.

`common.backtest.load_signals()` no longer chooses `nth(-2)`/`nth(-1)` files. Callers must provide `CONTRACT_UNIVERSE`; for more than two declared contracts they must also provide `ACTIVE_CONTRACTS_BY_DATE`. `SIGNAL_MISSING_DATA_DISPOSITION` is explicitly `reject` (the default) or `drop`. `grid.signal_prepare()` forwards those declarations.

The older `backtest.load()` price-forward-fill/volume-difference route and the generic grid manifest are retained only for historical/example drivers. They are not strict loaders, do not form a content-hashed trial provenance set, and cannot be offered as S0 loader or economics evidence.

## Declared stress replay

`common.stress.StressScenario` is a versioned, immutable profile for scheduler-owned action submission/arrival delay, participation, fee multiplier, basis shift, volatility multiplier, and opening-session admission. Receive-time market-data and signal delays are rejected: they would imply an ordering route excluded by exchange-batch replay. `apply_ingress_stress()` preserves exchange-batch ordering and applies only batch-safe payload stress; `stressed_execution_models()` changes only participation and produces a distinct execution-model version/reference. Fee, basis, and action-timing hooks are applied by their declared owners. Volatility and opening-session hooks are explicit pure inputs for the policy/replay layer; they are not silently inferred by the engine.

When a `DualBookFoundation` is built with a scenario, each decision records a `stress_scenario` trigger evaluation and `capture_provenance()` adds the canonical scenario payload to the immutable content-hashed artifact set. A stress run therefore cannot be mistaken for an unstressed candidate. The `TrialDeclaration.candidate_freeze_decision` and immutable configuration artifact together distinguish a frozen candidate from subsequent development configurations.

Policy-specific legacy fields on `Strategy` and historical top-level `Market` fill metadata remain compatibility data. The supported foundation accepts only the immutable, namespaced `OrderIntent.strategy_metadata` declaration and never promotes it to a canonical telemetry field.

## Depth-consuming execution

`common.execution.DepthExecutionService` is the sole supported Phase-3 foundation service for aggressive `HEDGE` and `EOD` intents. It accepts only a registered `OrderIntent` paired with its causal `DecisionContext`, resolves the named/versioned execution model (or declared run default), requires the active `DepthBook` to match the context's immutable snapshot, and returns one immutable `ExecutionResult`.

The service consumes each reachable opposite-side level at its executable touch price, reduces remaining depth exactly once, and reports per-level fills, VWAP, residual, decision/execution sequences, model reference, participation, and a declared terminal disposition. A book can be replaced only by a strictly newer snapshot for the same product; a quoted-product update cannot replenish a hedge-product book. Empty/zero depth follows the model's `sparse_book_disposition`; malformed or crossed books fail closed. `participation_rate = 0.0` is an intentional deterministic no-op and therefore produces that sparse-depth disposition, rather than a fill.

At the lower-level Phase-3 boundary, `DepthBook` accepts caller-supplied levels bound to a `BookSnapshotRef`; it validates the reference identity but cannot independently prove that the levels were derived from the retained payload/hash. G5 verifies retained snapshot reconstruction and the execution result's causal snapshot/sequence. The supported G6 facade closes this gap for policy integrations by reconstructing and hash-checking the retained payload before deriving execution depth. Synthetic depth refresh is rejected until a future explicitly configured, versioned, and tested model exists.

`Market.fak()`, `_fak_sweep()`, `_sweep_current_depth()`, and `PairMarket` matching remain historical compatibility APIs. Their legacy tests characterize those behaviours only and are not accepted evidence for S0 aggressive hedge or EOD economics.

## Intent lifecycle and capacity

`common.lifecycle.IntentLifecycleService` is the sole supported Phase-4a registry for intent state transitions and policy-declared live-maker capacity. A `CapacityEnvelope` states only a pair/product and its maximum worst-case open quantity; it does not contain reservation-price, quote-size, retry, or hedge policy. Capacity is envelope-scoped: distinct envelope IDs may deliberately cover the same pair/product, while policy that requires one aggregate limit must submit those intents against the same envelope. Submitting a maker intent reserves its entire requested quantity. A capacity breach becomes a reconstructible terminal `REJECTED` lifecycle event with `capacity_envelope_exceeded`, rather than an over-cap order.

Only lifecycle actions can release that reservation: a passive maker fill releases its resolved quantity, and a declared terminal disposition releases any remaining live quantity. Submitted, arrived, partial, filled, cancelled, expired, rejected, stale, deadline, and failed states are immutable events, monotone in time, and terminal states cannot be retried. Aggressive execution results attach only after matching the registry's intent, decision, pair, product, model reference, snapshot, and sequence. The Phase-4a one-shot execution contract permits exactly one execution result per aggressive intent and rejects reused execution IDs; G4b will introduce an explicit multi-fill ledger if required. Maker fills cannot bypass their reservation with an aggressive result.

The historical `Market`/`Strategy` order books do not participate in this registry and remain compatibility-only. `TelemetryEmitter` records lifecycle/reservation events, and finalization validates terminal lifecycle and capacity invariants.

## Dual-leg ledger and EOD completion

`HedgeMappingSpec` binds policy-declared quoted and hedge risk weights to the `HedgePairRef` mapping ID/version. `DualLegLedger` uses it to reconstruct signed quoted position `q`, hedge position `h`, pending hedge quantity, and residual risk. Its only accepted effects are incremental maker fills from lifecycle events and HEDGE/EOD fills from an execution result that the same lifecycle registry accepted. Each immutable `LedgerEvent` retains both product identities through its pair reference, plus its source event, signed position delta, observed fee/rebate amount, and source role. Fees and rebates must be recorded with the fill at acceptance time: EOD catch-up may recover an omitted position effect but deliberately records zero inferred costs.

`EodCompletionService` first preflights the declared execution model, then cancels non-EOD live intents for the pair and derives close quantity and side from the ledger's actual `q` and `h`. An `EodCloseRequest` must declare both product limits and the causal decision context. The service creates one-shot EOD intents and runs them only through `DepthExecutionService`; it never infers a touch fill or refreshes depth. A partial EOD result is terminally marked `eod_incomplete_liquidity`, and `EodCompletion` reports explicit residual positions/risk with `INCOMPLETE_LIQUIDITY`. Reusing an EOD request ID fails closed, preventing a double close.

## Canonical telemetry, invariants, and provenance

`common.telemetry.TelemetryEmitter` is the only supported Phase-5 reporting source. It writes the schema-versioned canonical tables in [`contracts/telemetry_schema.md`](../../contracts/telemetry_schema.md): decisions, book events/snapshots, orders, fills, hedge executions, trigger evaluations, consumed signal snapshots, outcome markers, and inventory series. Each row carries the run and hedge-pair identity; decision-derived records retain both product identities through that pair. `load_canonical_table()` is the read path for reporting. Strategy-local record shapes remain compatibility data and are not S0 evidence.

Every required canonical table must be retained. A legitimate no-record table, such as `fills` in a no-fill trial, is declared with `declare_empty_table()` and remains an explicit empty artifact; an absent table is still an error-level eligibility failure.

Finalization requires a complete immutable `RunProvenance`: `TrialDeclaration` records development, calibration, and holdout windows, candidate-freeze decision, policy version, pair/mapping, execution models, and data-cleaning transforms. The durable artifact set content-hashes market data, signal data, configuration, code, schema, fee profile, instrument/roll mapping, and execution models. Every candidate run captures its own manifest before economics are considered.

`TelemetryInvariantChecker` scans the durable artifacts through an on-disk index at finalization. It checks schema coverage, snapshot reconstruction, causal input availability, execution use of the decision-bound book/sequence and duplicate execution identity, capacity envelopes, terminal lifecycle, fill/order joins, ledger-to-inventory quantities, and the arithmetic/eligibility of any reconciled PnL outcome. A failed error-severity invariant gives `TelemetryRunResult.eligible == False`; it cannot be reported as a valid pre-S0 diagnostic or S0 economics run.

## PnL attribution and reconciliation

`common.pnl_attribution.PnlAttributionService` is the sole supported Phase-4c PnL path. It consumes `DualLegLedger` events, one immutable `PnlPriceObservation` per event, declared product marks, and the corresponding immutable `InstrumentSpec` multipliers. It refuses missing, duplicate, or extra price observations; it never falls back to legacy strategy position/PnL records.

For signed fill quantity `q`, multiplier `k`, fill price `F`, decision reference `R`, and accounting mark `M`, a passive quoted fill is split into maker capture `q(R-F)k` and quoted-leg price PnL `q(M-R)k`. An aggressive quoted EOD fill has no maker-capture claim and uses `q(M-F)k` as quoted-leg price PnL. A hedge fill uses hedge-leg price PnL `q(M-R)k` and hedge execution shortfall `q(F-R)k`, which is subtracted from the waterfall. Net PnL is exactly maker capture + quoted-leg price PnL + hedge-leg price PnL − hedge shortfall − fees + rebates. Residual-basis PnL is only the derived sum of the two leg-price categories; it is never added to that total.

The service reconciles every ledger event to exactly one canonical `fills` row (including source, quantity, position, fee, and rebate), and compares the waterfall independently with declared accounting and cycle totals. If an EOD-derived ledger effect exists, its completion must also reconcile to the final ledger positions and execution IDs. A zero-fill EOD result deliberately has no ledger/PnL effect: it remains in the EOD completion and its residual inventory is marked, but it is not required to appear in `fills`. Any residual beyond the declared tolerance returns `economics_eligible == False`. `TelemetryEmitter.emit_pnl_attribution()` records that verdict in `outcome_pnl`; only a fully reconciled row may claim economics eligibility.

## Test-suite status

| Suite | Status | Role |
| --- | --- | --- |
| `common.tests.run` | Supported | Legacy matching, session, PnL, cycle, and order-limit regression checks. |
| `common.tests.foundation.run` | Supported | Phase-0/Phase-7 contract, calendar-lifecycle, causal-ingress, strict loading, declared stress replay, depth-execution, intent-lifecycle/capacity, dual-leg-ledger/EOD, canonical telemetry/provenance, PnL attribution/reconciliation, public-API conformance, and current-behaviour characterization checks. |
| `common.tests.run_acceptance` | Supported | Required aggregate local acceptance command. |
| `strategies.pairs.test_contracts_v2` | Reference only | Typed pair-v2/manifest patterns; not an S0 foundation gate. |
| `strategies.pairs.test_pairs_v2` | Archived reference only | Historical pair-v2 code may retain unsupported constructor/signature assumptions; it is excluded from supported commands and is not an S0 foundation adapter. |

Phase-specific characterization tests intentionally assert defects in the current engine. When a phase remediates a defect, replace its characterization assertion with the target-state acceptance assertion in the same change. Do not keep obsolete behaviour as a passing requirement.
