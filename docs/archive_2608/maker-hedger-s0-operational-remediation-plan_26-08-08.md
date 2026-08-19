# Maker-Hedger S0 Operational Remediation Plan

**Date:** 2026-08-10
**Source acceptance review:** `D:\OneDrive\Research\Futures-Maker-Hedger\analysis\backtest-infra-acceptance-review.md` (last review commit: `215549c`, `revise on raw data limitations`)
**Engine baseline:** `0df18931` (`implement r10 of s0 remediations`). The raw-data decision `d0f01c49` is the documentation-only descendant that defined the accepted snapshot contract; the supported implementation below carries that contract on the foundation route.
**Status:** Integrated component/API gate **PARTIAL PASS**. B1-B10 controls, the raw-snapshot adapter, separately typed matcher evidence, and raw proxy telemetry are implemented and deterministically verified. B10 seals the exchange-published dual-book batch as the sole replay clock and removes receive-time ordering. S0 promotion remains **NO-GO** pending Gate 7's frozen realistic-policy/test-day and disjoint-holdout evidence. The 2026-08-10 raw-tick review accepts the available five-level snapshots as the bounded snapshot-proxy data contract; a replacement trade tape is not an entry requirement.

## Decision

The sole S0 evidence route is:

```text
strict loader -> CausalIngress -> ProductionReplayAdapter -> DualBookFoundation
              -> calendar EOD -> PnL attribution -> canonical + research telemetry
```

`ProductionReplayAdapter` is the only S0 evidence runner. `Backtest`,
`Market`, `Strategy`, `PairMarket`, and `public_tools/` are frozen
compatibility/example paths and must not produce or be described as S0
economics, stress, holdout, or promotion evidence.

The implementation controls below are necessary infrastructure evidence, not a
promotion decision. No S0 economics comparison, stress comparison, holdout
result, or promotion claim may be made until Gate 7 completes.

## Implemented controls

| Area | Status | Control |
|---|---|---|
| B1 decision snapshots | Implemented | Each material decision exports linked quoted and hedge `snapshot_reason="decision"` rows, validated by product and sequence. |
| B2 outcome projection | Implemented | The replay derives route and risk-open duration from ledger events, executions, and EOD; research sealing independently reconciles retained inventory projections to the exported fill and hedge-execution facts. |
| B3 maker queue evidence | Implemented | Immutable `MakerQueueEvidence` is captured at arrival independently of fills; absent arrival evidence fails the research gate. |
| B4 signal values | Implemented | Policies receive immutable `CausalSignalSnapshot` values through `DecisionContext.signal_value(ref)`. |
| B5 stress validity | Implemented | Tick-aware volatility stress validates transformed books and trade-level inputs before ingress. Raw snapshot books and proxy buckets are tick-validated by their adapter; volatility stress on proxy intervals fails closed until a separately declared transformation model exists. |
| B6-R4 PnL and mark evidence | Implemented | Eligibility requires authenticated canonical artifacts, deployment-owned trust roots, authority separation, content-to-value binding, methodology/version, and EOD as-of constraints. |
| B7 research provenance | Implemented | Sealing revalidates every persisted research row's owned identity, declared fields, types, nullability, enums, and timestamps before joins; separately typed proxy fills add raw interval/source/model fields and are checked for book causality and bucket conservation. Every mandatory table and each present optional table is content-hashed in the manifest, which is bound into canonical provenance before eligibility is exposed. Raw snapshot provenance additionally binds the complete adapter configuration, authenticated source manifest, and the canonical immutable book-event payload actually replayed. |
| B8 duration and route semantics | Implemented | Research sealing requires exactly one run aggregate PnL row, rejects duplicate episode and singleton business IDs before joins, reconciles each fill-backed order's positive, monotonically cumulative fills and final status to its requested quantity, recomputes fill/hedge-reconciled inventory state, and validates risk-open duration, start/end coverage, route order, and EOD activity. |
| B9 signal joins | Implemented | Research rows carry exact signal-snapshot identities; sealing recomputes and validates each decision's membership commitment. |
| B10 exchange-batch sequencing | Implemented | Production replay requires complete atomic quoted/hedge batches, exposes current/prior batch identity and interval-fill IDs to policy, validates policy-owned prior-batch price references for fill-triggered orders, and rejects receive-time market/signal delay stress. |

## Round 4 economic-evidence controls

### B6-R4. Authenticated PnL and mark evidence

`DeploymentEvidenceAuthorityRegistry` supplies deployment-owned HMAC-SHA256
trust roots when the replay host constructs `ProductionReplayAdapter`; HMAC
authorities and their keys are not accepted by `ProductionReplayConfig`. Only
authority/key identities are retained in run provenance; secrets are never
emitted. A qualifying source artifact must be canonical JSON with
schema `s0-economic-evidence-v1`, an approved signature, run/session identity,
artifact identity, methodology/version, as-of timestamp, and the exact
declared PnL total or valuation mark.

`EconomicReplayInputs.verified_evidence_eligible()` requires separate
installed authorities for accounting, cycle, and valuation evidence. PnL
calculation times must not predate EOD; valuation marks must not postdate it.
Unsigned, incorrectly signed, non-canonical, mismatched, or unapproved source
content leaves both `semantic_compliance_eligible` and `economics_eligible`
false.

### B7. Sealed research provenance

Research export finalizes before canonical provenance capture. It writes a
canonical `s0-research-manifest-v2` containing content hashes for every
mandatory research table and each present optional research table (with an
explicit null hash for an absent optional table), plus the complete
research-result payload. Its hash is returned
by `ResearchTelemetryResult`, stored in `research_result.json`, and captured
as the canonical `research_manifest` artifact. Economic eligibility requires
the research and canonical manifest hashes to agree. Before any cross-table
join, sealing reloads each JSONL row and fail-closes on malformed JSON, missing
or unsupported fields, changed emitter-owned identity, invalid timestamp,
type, nullability, or enum value. The manifest is therefore never an eligible
attestation of an artifact that only passed transient emission-time checks.
When present, `label_outcomes` is producer-owned by the research label layer;
sealing validates its label-row identity, decision/order/fill/episode joins,
side consistency, and feature/anchor/outcome/finalisation timestamp order
before including its content hash in the manifest.

### B8. Outcome duration and route semantics

Research sealing requires a decision-state inventory record at the outcome
start and an EOD-state record at the outcome end. Every maker or EOD quoted
fill inventory transition carries `fill_id`; every hedge or EOD hedge
transition carries `hedge_id`. Exactly one `episode_id = null` row represents
the run aggregate; non-null episode identifiers must be unique. At seal time,
fill and hedge business identifiers must be unique before any join is built. Positions are replayed
from those facts, and each inventory row's `q`, `h`, `residual_risk`, and
`exposure_risk_scaled` is reconciled against the declared `HedgeMappingSpec`.
Each fill or hedge execution may appear in exactly one inventory transition
before risk-open duration, route shape/order, and EOD activity are accepted.
The emitter receives the declared `HedgeMappingSpec` and also recomputes
`beta_t` rather than trusting the exported value. The producer map is:
decision state from the foundation ledger snapshot, current trade-level fill
state from matcher-issued `PassiveFillEvidence` or current snapshot-proxy fill
state from matcher-issued `SnapshotIntervalQueueProxyEvidence`, and hedge/EOD
state from retained `ExecutionResult`/ledger events. The proxy producer retains
its raw interval identity rather than relabelling an interval as a
`PassiveTrade`. Independently of that
inventory projection,
sealing groups fill-backed orders by order and requires positive fill quantities, exact
cumulative progression, and aggregate quantity no greater than the declared
request. A full aggregate requires `filled`; `partial` requires a strictly
partial aggregate; and `cancelled`, `expired`, `failed`, or `rejected` cannot
claim a fully filled order (with `rejected` requiring no fills).

### B9. Complete signal-consumption joins

Each `signal_snapshots` row now carries `signal_snapshot_id`. At seal time the
checker recomputes the decision's canonical SHA-256 membership commitment from
the exact emitted identities, rejects duplicates, and requires equality with
the decision's `signal_set_hash`.

## Gate status

### Gates 0-5: IMPLEMENTED

The structural semantic-compliance gate, research reconstruction, causal signal
boundary, instrument validity, authenticated PnL/valuation evidence, and the
permanent economic predicate are implemented. The predicate conjoins canonical
eligibility, PnL reconciliation, research eligibility, execution freshness,
verified economic evidence, stress-input validity, and the sealed research
manifest hash.

### Gate 6: COMPLETE

Round 4 closed B6-R4 through B9 and added B5 negative coverage. Deterministic
acceptance includes an unsigned-PnL rejection, research-manifest binding,
false-duration and invalid-route rejection, incomplete signal-membership
rejection, and off-tick transformed input rejection.

### Gate 7: PENDING -- frozen realistic policy and holdout evidence

**Depends on:** a separately approved policy and data scope.

The inspected `E:\FinData\HFT\ticks` files provide five-level snapshots and
cumulative volume/turnover, but no trade ID, aggressor side, per-trade
price/quantity, or certified receive clock. Those limits are accepted as the
snapshot-proxy model boundary, not as a requirement to obtain a replacement
trade feed. `RawSnapshotAdapterConfig` and the separately typed
matcher-issued snapshot evidence now carry this contract through the supported
path. See `passive-order-fill-simulation-review_26-08-09.md`.

1. **Implemented infrastructure:** `RawSnapshotAdapterConfig` freezes the
   declared universe, proxy-interval contracts, timezone, tick/multiplier
   tables, five-level depth,
   exchange-batch identity from `exchtime`, raw source hash/ordinal,
   batch/contract/source-row merge order, cumulative-flow two-tick allocation,
   price-reach, retained-depth boundary, and interval dispositions. It derives
   proxy intervals only for the declared proxy contracts; production replay
   binds that set exactly to the quoted product. Each allocated bucket must be
   inside the current retained positive-depth five-level envelope, otherwise it
   rejects by default or drops the offending source row under the declared
   `drop` disposition. Every raw-adapter drop is a source-qualified
   `LoaderValidationIssue` carrying the source ID, raw row ordinal, reason, and
   disposition, so accepted data loss is auditable through `ValidatedMarketData`.
   If a dropped row leaves a partial quoted/hedge exchange batch, production
   replay rejects that batch rather than constructing a mixed pair state.
   The typed immutable provenance
   artifact content-hashes this complete configuration and the source manifest.
   `read_raw_snapshot_market_data()` parses the exact bytes it hashes and binds
   a canonical digest of the immutable book-event payload actually replayed.
   Production verifies that digest before ingress, then replays the verified
   event tuple. It accepts intervals only when the declared file hashes,
   interval source ID/hash, contract, row ordinal, and model fields agree with
   the manifest, and the adapter tick/multiplier tables equal the replay
   `InstrumentSpec`s. In-memory caller-supplied frame hashes are test-only and
   fail closed in production. A dropped reset begins a source-qualified
   cumulative epoch, so later rows are not compared against the preceding
   counter epoch.
2. **Implemented infrastructure:** matcher-owned snapshot-interval evidence
   links every proxy fill to its raw source interval, queue-at-arrival, retained
   book, and shared bucket without fabricating `PassiveTrade` or claiming an
   observed aggressor side.
3. Freeze the policy, parameters, registered signals, market-data and signal
   artifacts, instrument/roll map, valuation evidence, stress matrix, and
   code/config hashes.
4. Run a realistic test-day episode only through `ProductionReplayAdapter`.
   Demonstrate behavior that changes with causal signal values, not merely
   signal presence.
5. Retain canonical and research manifests, source PnL/mark artifacts, and
   machine-readable gate results for the base run and every declared stress.
6. Freeze the candidate before evaluating a disjoint untouched holdout. Do not
   tune policy, parameters, data cleaning, or stress settings using holdout
   results.
7. Review the holdout as research evidence. Passing it is necessary for
   promotion consideration, but it cannot waive an infrastructure failure.
   A trade-level feed, if later available, is a calibration enhancement rather
   than a prerequisite for this bounded snapshot-proxy claim.

## Acceptance evidence and verification

The Round 4 parent `b6153bf4` passed **187/187** checks: 115 foundation and 72
legacy. The committed Round 5 baseline `d607e2e4`, which includes the
deployment-registry and inventory-reconciliation remediations, passed
**188/188** checks: 116 foundation and 72 legacy. Round 7 execution-fact
identity and residual-risk reconciliation passed **191/191** checks: 119
foundation and 72 legacy. Round 8 sealed-row validation and fill-backed order
reconciliation passes **195/195** checks: 123 foundation and 72 legacy. The
raw-snapshot provenance, manifest-binding, reset-epoch, and adapted-replay
payload-binding remediations pass **210/210** checks: 138 foundation and 72
legacy. The exchange-batch sequencing refinement passes **216/216** checks:
144 foundation and 72 legacy. `git diff --check` passes.
This is meaningful deterministic regression evidence, but does not prove
realistic performance, real-world policy behavior, or promotion readiness.

After every foundation, replay, telemetry, matching, stress, or session change,
run:

```powershell
$env:PYTHONPATH = Split-Path -Parent (Get-Location)
& 'C:\Users\sgjia\miniconda3\envs\py310\python.exe' -m common.tests.run_acceptance
git diff --check
```

## Final S0 acceptance gate

S0 promotion consideration may begin only after Gate 7 demonstrates all of the
following:

1. the supported causal replay route, arrival-time execution, and calendar EOD;
2. value-sensitive decisions from registered causally available signals;
3. matcher-issued, source-interval-qualified snapshot-proxy fills with
   price-time allocation, interval-budget conservation, and depth-consuming
   aggressive/EOD execution; any stronger trade-level claim requires stronger
   input data;
4. decision-linked research books, maker queue evidence, authoritative outcome
   semantics, and complete consumed-signal joins;
5. canonical and research manifests identifying the exact reviewed artifacts;
6. reconciled dual-leg PnL bound to independently authenticated accounting,
   cycle, and valuation evidence;
7. declared stress through the same production path with post-transform
   instrument validation; and
8. a frozen base episode followed by a disjoint untouched holdout.

Until then, passing component tests remain necessary but insufficient S0
promotion evidence.
