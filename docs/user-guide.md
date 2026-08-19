# Backtest Infrastructure User Guide

**Date:** 2026-08-09
**Contract version:** `0.13.0` | **Telemetry schema:** `0.6.0`

## Overview

This is a **maker-hedger backtesting foundation** for dual-book futures strategies —
one quoted product (passive maker) and one correlated hedge product (aggressive
hedger). It provides two execution paths:

| Path | Module | Status | Purpose |
|------|--------|--------|---------|
| **Production replay** | `production_replay.py` | Supported S0 | Strict causal replay, research telemetry, economic eligibility |
| **Legacy** | `backtest.py` / `market.py` / `strategy.py` | Frozen compatibility | Historic strategies, example drivers |

The production replay path is the **sole S0 evidence route**. It never imports
the legacy path. Legacy code is retained so existing strategies still run, but
cannot produce S0 economic, stress, or promotion evidence.

### Runtime

- **Python:** 3.10.13 (`py310` conda environment)
- **PYTHONPATH:** `D:\OneDrive\Python\Fut_HFT\backtest`

```powershell
$env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run_acceptance
```

### Key dependencies

`pandas`, `numpy`, `matplotlib`, `plotly`, `polars`, `pyarrow`, `fastparquet`, `joblib`, `numba`

---

## Examples

`examples/foundation_taker/` is the runnable, supported-path example. Its
`ThresholdHedgePolicy` consumes an explicitly declared signal and uses
`ProductionReplayAdapter` to submit an aggressive order on the correlated
hedge leg. Its compact synthetic input is for operational demonstration only;
it does not produce economic or promotion evidence.

`examples/legacy/arb/` and `examples/legacy/taker/` are demoted historical
examples. They retain the frozen `public_tools` execution path for comparison
and diagnostics, and must not be described as S0 economics, stress, holdout,
or promotion evidence. The foundation does not presently offer a generic
single-leg taker route: passive orders are scoped to the quoted product and
aggressive orders to its hedge product.

---

## Architecture

```
                  ProductionReplayAdapter.run()
  (sole S0 evidence runner — production_replay.py)
                            │
    ┌───────────────────────┼───────────────────────┐
    │                       │                       │
    ▼                       ▼                       │
ValidatedMarketData   CausalIngress       ProductionMakerHedgePolicy
(foundation_loader)   (ingress.py)        (user-supplied, two methods)
    │            declared availability              │
    │                  ordering,                    │
[apply_ingress_stress] hash-addressed               │
    │                  snapshots                    │
    │                       │                       │
    └───────────┬───────────┘                       │
                │                                   │
          IngressBatch loop ────────────────────────┘
                │        schedule_decision →
                │        select_signal_ids → propose
                │
                ▼
     DualBookFoundation  (foundation_api.py — public S0 facade)
     ┌──────────────────────────────────────────────────┐
     │  PassiveMatchingService  (passive_matching.py)   │
     │    price-time maker fill allocation              │
     │                                                  │
     │  DepthExecutionService   (execution.py)          │
     │    aggressive hedge + EOD depth consumption      │
     │                                                  │
     │  IntentLifecycleService  (lifecycle.py)          │
     │    order state machine, capacity reservations    │
     │                                                  │
     │  DualLegLedger           (ledger.py)             │
     │    positions, residual risk, fill-cost tracking  │
     │                                                  │
     │  TelemetryEmitter ──► canonical JSONL artifacts  │
     │    (telemetry.py)                                │
     └──────────────────────────────────────────────────┘
                │
                ▼  (after calendar EOD)
     ┌───────────────────────────────────────────────────┐
     │  PnlAttributionService   (pnl_attribution.py)     │
     │    maker-capture / leg-price / waterfall P&L      │
     │                         │                         │
     │  ResearchTelemetryEmitter (research_telemetry.py) │
     │    cross-table validation + manifest hashing      │
     └───────────────────────────────────────────────────┘
                │
                ▼
     OperationalReplayResult
       .economics_eligible  (fail-closed conjunction)
       .telemetry           (canonical run result)
       .pnl_attribution     (reconciled waterfall)
       .research_telemetry  (cross-table result + manifest hash)

Legacy path (compatibility only, frozen):
  backtest.py ──► Market (market.py) ──► Strategy (strategy.py) ──► PnL / reporting
```

### Module map

| Module | Role |
|--------|------|
| `foundation_contracts.py` | Immutable vocabulary: config, snapshots, intents, evidence, errors |
| `foundation_api.py` | Public `DualBookFoundation` facade + `ProductionMakerHedgePolicy` protocol |
| `foundation_loader.py` | Strict market-data validation → `ValidatedMarketData` |
| `ingress.py` | `CausalIngress`: atomic exchange-batch ordering, hash-addressed snapshots |
| `production_replay.py` | `ProductionReplayAdapter`: wires loader → ingress → facade → telemetry |
| `execution.py` | `DepthExecutionService`: aggressive hedge/EOD depth consumption |
| `lifecycle.py` | `IntentLifecycleService`: order state machine, capacity reservations |
| `ledger.py` | `DualLegLedger`: signed positions, residual risk, fill-cost tracking |
| `passive_matching.py` | `PassiveMatchingService`: price-time maker fill allocation |
| `pnl_attribution.py` | `PnlAttributionService`: maker-capture / leg-price / waterfall P&L |
| `telemetry.py` | `TelemetryEmitter`: canonical artifact writing, invariant checks |
| `research_telemetry.py` | Research schema export, cross-table validation, manifest hashing |
| `stress.py` | `StressScenario`: delay, vol, fee, participation, basis dimensions |
| `reporting.py` | Post-hoc report generation from canonical tables |
| `cycles.py` | Cycle normalizer: bucket trade records, P&L summaries |
| `sessions.py` | Session calendar: day/night windows, trading-day rollover |
| `grid.py` | Signal preparation and grid infrastructure |
| `market.py` | Legacy matching engine (frozen compatibility) |
| `strategy.py` | Legacy P&L engine (frozen compatibility) |
| `backtest.py` | Legacy backtest runner (frozen compatibility) |

---

## Quick Start: Running the Tests

```powershell
# Full acceptance suite (legacy + foundation)
$env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run_acceptance

# Legacy suite only
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run

# Foundation suite only
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.foundation.run

# With pytest (if installed; run from common/)
pytest tests/
```

Expected: **216 tests pass** (72 legacy, 144 foundation). Any failure means the
engine primitives changed — investigate before trusting results.

---

## Path 1: Production Replay (Supported S0)

This is the only path that produces S0 economic evidence. It follows a strict
pipeline:

1. **Validate** market data through the strict loader
2. **Ingest** complete exchange-published dual-book batches atomically
3. **Decide** via a user-supplied policy after each sealed book batch
4. **Execute** through the public `DualBookFoundation` facade
5. **Close** at calendar EOD
6. **Attribute** P&L with independently evidenced inputs
7. **Seal** research telemetry with cross-table validation

### Exchange batches and raw snapshot status

Production replay uses exchange time and batch identity, not receive time, for
market-event order. Every non-empty production book batch must contain exactly
the quoted and hedge snapshots. The default batch identity is `exchange_ts`;
if the venue emits more than one batch at that instant, provide a distinct
`exchange_batch_id` and `exchange_batch_seq`.

`recv_ts` remains transport provenance and must not precede `exchange_ts`, but
it does not alter policy visibility or execution order. For the accepted
five-level raw snapshots in `E:\FinData\HFT\ticks`,
`RawSnapshotAdapterConfig` retains both clocks and derives the replay batch
from `exchtime`; its deterministic merge is by batch, contract, source ID, and
row ordinal.

Use `read_raw_snapshot_market_data()` for declared files or
`adapt_raw_snapshot_frames()` for in-memory deterministic inputs. The adapter
requires one or more declared sources for every configured contract, retains
each raw file hash and row ordinal, validates the five-level tick grid, and
assigns a run-global `source_seq` as provenance only.
Its matcher-owned fills are labelled `snapshot_interval_proxy_evidence`, not
observed trades. They are valid only under the declared
`snapshot_interval_queue_proxy_v1` / `bid_then_ask_v1` model; they do not make
live-fill or physical-latency claims. Volatility stress for this proxy is
currently rejected rather than silently transforming interval evidence.

### Step 1: Configure the run

```python
from datetime import date

from common.foundation_contracts import ExecutionModelRef, TrialDeclaration
from common.production_replay import (
    EconomicReplayInputs, ProductionReplayAdapter, ProductionReplayConfig,
)
from common.stress import StressScenario

# Construct the declared contract objects before configuring the run.
config = ProductionReplayConfig(
    run_id="my-run-001",
    hedge_mapping=hedge_mapping,           # HedgeMappingSpec
    instrument_specs=(q_spec, h_spec),     # InstrumentSpec for each product
    execution_models=(depth_model,),       # ExecutionModelConfig
    default_execution_model=ExecutionModelRef("depth", "1.0.0"),
    capacity_envelopes=(envelope,),        # CapacityEnvelope
    artifact_root="./artifacts",
    session_date=date(2025, 1, 2),
    trial=TrialDeclaration(...),           # trial identity + provenance
    provenance_artifacts={
        "configuration": {"policy": "my-policy:1.0.0", "market_data": "sha256:..."},
        "code": "sha256:...",
    },
    # Optional:
    stress_scenario=StressScenario("latency", "1.0.0", action_arrival_delay_ms=5.0),
    economic_inputs=EconomicReplayInputs(...),  # for P&L eligibility
    research_export=True,                       # enable research telemetry
    registered_signal_ids=frozenset({"my-signal"}),
    max_execution_book_age_ms_by_product={"Q": 1_000.0, "H": 1_000.0},
)
```

`provenance_artifacts` must include a `configuration` entry. The placeholders
above stand for fully constructed contract values; they are not inferred by the
adapter.

### Step 2: Write a policy

Implement the `ProductionMakerHedgePolicy` protocol — two methods:

```python
from common.foundation_api import (
    ProductionMakerHedgePolicy, PolicyProposal, PolicyTrigger
)
from common.foundation_contracts import (
    DecisionContext, MakerHedgeIntentBatch,
    OrderIntent, OrderRole, OrderSide,
)

class MyPolicy:
    def select_signal_ids(self, available_signals):
        """Declare which signal IDs to consume."""
        return tuple(s.signal_id for s in available_signals if s.signal_id == "my-signal")

    def propose(self, context: DecisionContext) -> PolicyProposal:
        """Return a maker/hedge batch using only values in context."""
        # Read bound signal values. This signal supplies both the score and
        # the policy's quote price.
        score = 0.0
        quote_price = None
        for signal in context.consumed_signal_values:
            score = float(signal.payload.get("score", 0.0))
            quote_price = float(signal.payload["quote_price"])

        if score > 0.5:
            if quote_price is None:
                raise ValueError("my-signal must supply quote_price")
            maker = OrderIntent(
                intent_id=f"{context.decision_id}:maker",
                run_id=context.run_id,
                decision_id=context.decision_id,
                hedge_pair=context.hedge_pair,
                role=OrderRole.MAKER,
                side=OrderSide.BUY,
                product=context.quoted_product,
                requested_qty=1,
                limit_price=quote_price,
            )
            batch = MakerHedgeIntentBatch(
                maker_intent=maker,
                maker_capacity_envelope_id="q-maker-capacity",
            )
        else:
            batch = MakerHedgeIntentBatch()  # explicit no-action

        return PolicyProposal(
            batch=batch,
            decision_attributes={"action": "quote" if score > 0.5 else "hold"},
            triggers=(PolicyTrigger(f"{context.decision_id}:trigger", {"score": score}),),
        )
```

**Rules:**
- `select_signal_ids` declares which signals the policy *will* consume
- `propose` receives only immutable context-bound values via `consumed_signal_values` and `signal_value(ref)`
- `BookSnapshotRef` intentionally contains no depth; the policy must supply
  its own causally available `limit_price`, such as a bound signal value
- A maker batch must declare a `maker_capacity_envelope_id` that matches one
  of the configured quoted-product capacity envelopes
- Return an empty `MakerHedgeIntentBatch()` for an explicit no-action decision
- Trigger IDs must be unique per decision

### Step 3: Load market data

```python
import pandas as pd

from common.foundation_loader import (
    MarketDataValidationConfig, validate_market_data
)

validation_config = MarketDataValidationConfig(
    declared_contract_universe=("Q", "H"),
    book_levels=1,
    source_timezone="Asia/Shanghai",  # or None if timestamps already tz-aware
)

market_data = validate_market_data(
    pd.DataFrame(rows),  # columns: contract, exchange_ts, recv_ts, source_seq,
    validation_config,   #          bidpx0, bidvol0, askpx0, askvol0,
)                        #          totalvol, totalvalue, passive_trades (optional)
```

Each row must have: `contract`, `exchange_ts`, `recv_ts`, `source_seq`, bid/ask
prices and volumes at each level, and cumulative `totalvol`/`totalvalue`.
`recv_ts` is retained transport provenance, and the combined input must be ordered by
that value with a strictly monotone, run-global `source_seq`. A source adapter
must retain the original source identity and row ordinal separately.

`passive_trades` is optional; when supplied, it is a list of quoted-product
aggressor trades used for verified passive-maker fills. It is for trade-level
input and must not be inferred from snapshot cumulative flow. The validator
checks monotone availability order, positive non-crossed prices, non-negative
depth, non-decreasing cumulative volumes, and contract-universe membership.

For accepted five-level raw snapshots, use the versioned adapter instead of
constructing canonical rows by hand:

```python
from common.foundation_loader import (
    RawSnapshotAdapterConfig, RawSnapshotFile, read_raw_snapshot_market_data,
)

raw_config = RawSnapshotAdapterConfig(
    declared_contract_universe=("Q", "H"),
    proxy_interval_contracts=("Q",),  # the passive-maker / quoted contract
    source_timezone="Asia/Shanghai",
    tick_by_contract={"Q": 0.2, "H": 0.2},
    multiplier_by_contract={"Q": 300.0, "H": 300.0},
)
market_data = read_raw_snapshot_market_data(
    (
        RawSnapshotFile("q-session", r"E:\FinData\HFT\ticks\Q.csv", "Q"),
        RawSnapshotFile("h-session", r"E:\FinData\HFT\ticks\H.csv", "H"),
    ),
    raw_config,
)
```

The first source row has no interval. Only declared `proxy_interval_contracts`
produce cumulative-flow intervals; the other declared sources still provide
their retained books for hedge and valuation decisions. Production replay
requires this set to be exactly the pair's quoted product. Positive proxy-leg
cumulative-flow deltas become quantity-conserving valid-tick interval buckets
within the current snapshot's positive five-level price envelope (lowest bid
through highest ask). An off-depth bucket rejects the adaptation by default;
`off_depth_interval_disposition="drop"` drops the offending source row from
replay, so it cannot produce a proxy fill. If this leaves an incomplete
dual-book exchange batch, production replay fails closed. Zero-volume, reset, and
other invalid-interval dispositions are explicit configuration. With
`cumulative_reset_disposition="drop"`, the reset row is dropped and subsequent
rows begin a new source-qualified cumulative epoch, so cumulative validation
does not compare them to the prior counter epoch. Every raw-adapter drop is
returned in `ValidatedMarketData.issues` as a `LoaderValidationIssue` with its
raw source ID, row ordinal, reason code, and `drop` disposition.

The returned `source_provenance` is a typed immutable manifest. It records the
complete adapter configuration, including the declared universe, proxy-interval
contracts, tick and multiplier tables, model and availability rules,
dispositions, source identity, row extent, file-content hash authority, and an
`adapted_replay_hash`. The
file reader parses the already-hashed bytes in memory; it does not reopen the
path. `adapted_replay_hash` covers the exact canonical book-event payload
(timing, sequence, retained depth, and any proxy interval) that will be handed
to ingress. Production materializes that immutable event tuple once, verifies
its hash before replay, then replays that same tuple. It also verifies every
interval's source ID/hash, contract, row ordinal, and model fields against the
manifest, and each adapter tick/multiplier against the corresponding
`InstrumentSpec`. `adapt_raw_snapshot_frames()` remains a deterministic
in-memory test helper; its caller-supplied hashes are rejected by production
replay.

### Step 4: Run

```python
adapter = ProductionReplayAdapter(config)
result = adapter.run(market_data, MyPolicy())

print(f"Decisions: {len(result.decision_ids)}")
print(f"EOD: {result.eod_completion.disposition.value}")
print(f"Canonical eligible: {result.telemetry.eligible}")
print(f"Research eligible: {result.research_telemetry.eligible}")
print(f"Economics eligible: {result.economics_eligible}")
```

### Step 5: Read results

```python
from common.telemetry import load_canonical_table
from pathlib import Path

decisions = load_canonical_table(Path("./artifacts") / config.run_id, "decisions")
fills     = load_canonical_table(Path("./artifacts") / config.run_id, "fills")
outcomes  = load_canonical_table(Path("./artifacts") / config.run_id, "outcome_pnl")

for row in fills:
    print(f"{row['fill_id']}: {row['product']} qty={row['fill_qty']} @ {row['fill_price']}")
```

### The `OperationalReplayResult`

| Field | Meaning |
|-------|---------|
| `telemetry.eligible` | Canonical artifacts passed invariant checks |
| `pnl_attribution` | Reconciled P&L waterfall (if `economic_inputs` supplied) |
| `research_telemetry.eligible` | Research schema passed cross-table validation |
| `execution_freshness_eligible` | All executions used books within age limits |
| `semantic_compliance_eligible` | All S0 semantic checks passed |
| `economics_eligible` | **Final gate**: all above + verified economic evidence |

`economics_eligible` is the fail-closed conjunction of six conditions. It is
the only gate that authorizes S0 economic claims.

---

## Path 2: Legacy Backtest (Frozen Compatibility)

The legacy path is retained so historic strategies continue to run. It cannot
produce S0 evidence.

```python
from common.backtest import Backtest
from common.market import Market
from common.strategy import Strategy

bt = Backtest()
bt.load_data("Q", "path/to/Q.csv", multiplier=10000, tick=0.005)
bt.load_data("H", "path/to/H.csv", multiplier=10000, tick=0.005)

# Define a strategy (subclass Strategy)
class MyStrategy(Strategy):
    def on_step(self, market, pair_market):
        # legacy decision logic
        ...

bt.run(MyStrategy)
```

**Fee convention:** `FEE` (rate) and `FEE_LOT` (fixed $/lot) represent the
**full round-trip** cost, charged **once on the closing trade**. Opens add no fee.

---

## Stress Scenarios

`StressScenario` applies independently composable stress dimensions. Each is a
pure transformation — the base scenario has all dimensions at neutral values.

```python
from common.stress import StressScenario

scenario = StressScenario(
    scenario_id="latency-50ms",
    version="1.0.0",
    action_submission_delay_ms=5.0,  # delay order submission
    action_arrival_delay_ms=10.0,    # additional delay before arrival
    participation_multiplier=0.8,    # reduce fill participation
    fee_multiplier=0.9,              # reduce fee impact
    basis_shift=0.5,                 # shift decision mid-price
    volatility_multiplier=0.7,       # compress book spread
    opening_session_disposition="skip",  # "allow" or "skip"
)
```

Set `stress_scenario` on `ProductionReplayConfig` to apply it. The base scenario
(`is_base = True`) has all dimensions at zero/1.0/"allow".

Receive-time `market_data_delay_ms` and `signal_delay_ms` are not supported by
exchange-batch replay and are rejected. Use action timing for post-batch order
latency; see [the exchange-batch contract](exchange-batch-replay-contract_26-08-13.md)
for the interval and pricing-reference rules.

**Tick preservation:** When `volatility_multiplier ≠ 1.0`, stressed prices are
rounded to instrument ticks (floor for bids, ceiling for asks). An off-tick
transformed book fails closed.

---

## Economic Eligibility

To claim `economics_eligible=True`, supply economic evidence through
`EconomicReplayInputs`:

```python
from common.production_replay import EconomicReplayInputs
from common.foundation_contracts import (
    PnlViewEvidence, ValuationMarkEvidence, PnlAccountingView
)

inputs = EconomicReplayInputs(
    marks_by_product={"Q": 100.5, "H": 99.5},
    accounting_view=PnlAccountingView("accounting", total_pnl=-1.0),
    cycle_view=PnlAccountingView("cycle", total_pnl=-1.0),
    accounting_evidence=PnlViewEvidence(
        "acct-evidence", "accounting", -1.0,
        "general-ledger", "1.0.0", "gl-close",
        calculated_at=eod_ts,
        source_artifact=signed_json_bytes,  # HMAC-signed canonical JSON
    ),
    cycle_evidence=PnlViewEvidence(...),
    mark_evidence_by_product={
        "Q": ValuationMarkEvidence(
            "q-mark", "Q", 100.5,
            "settlement", "1.0.0", "q-settlement",
            observed_at=obs_ts,
            source_artifact=signed_json_bytes,
        ),
        "H": ValuationMarkEvidence(...),
    },
)
```

### Authority registry

Economic source artifacts must be **HMAC-SHA256 signed** by authorities in a
deployment-owned registry:

```python
from common.production_replay import DeploymentEvidenceAuthorityRegistry
from common.foundation_contracts import ApprovedEvidenceAuthority

registry = DeploymentEvidenceAuthorityRegistry((
    ApprovedEvidenceAuthority("accounting-dept", "v1", accounting_key),
    ApprovedEvidenceAuthority("cycle-dept", "v1", cycle_key),
    ApprovedEvidenceAuthority("valuation-desk", "v1", valuation_key),
))

adapter = ProductionReplayAdapter(config, authority_registry=registry)
result = adapter.run(market_data, policy)
```

Obtain these byte keys from the deployment's secret manager; do not hard-code
production key material. The independently produced source artifacts must be
canonical `s0-economic-evidence-v1` JSON with the expected authority selector,
declared values, and HMAC signature.

`economics_eligible` requires:
1. Canonical telemetry passes invariants
2. P&L attribution reconciles to within tolerance
3. Research telemetry passes cross-table validation
4. Execution freshness (book age ≤ configured max per product)
5. Semantic compliance (provenance manifest present, evidence hashes match,
   research schema version matches)
6. Verified economic evidence (signed source artifacts, authority independence,
   temporal constraints, content-to-value binding)

---

## Research Telemetry

When `research_export=True`, the replay emits **10 research tables** as JSONL
under `<artifact_root>/research/<run_id>/tables/`:

| Table | Key identity | Content |
|-------|-------------|---------|
| `decisions` | `decision_id` | Policy inputs, book refs, signal hash, attributes |
| `book_events` | `event_id` | Book event timing and source order |
| `book_snapshots` | `snapshot_id` | Decision-linked book state with top-K levels |
| `orders` | `order_id` | Declared intents, lifecycle events, reservations |
| `fills` | `fill_id` | Maker/taker/EOD fill records |
| `hedge_executions` | `hedge_id` | Hedge and EOD execution results |
| `trigger_evaluations` | `trigger_id` | Policy trigger audit |
| `signal_snapshots` | `signal_snapshot_id` | Consumed signal payloads |
| `outcome_pnl` | `row_id` | P&L waterfall, route, inventory duration |
| `inventory_series` | `row_id` | Position/exposure timeline |

At seal time, `_cross_table_errors()` validates:
- **Duplicate detection:** singleton business IDs (decision_id, snapshot_id,
  order_id, fill_id, hedge_id, trigger_id) must be unique
- **Decision→snapshot joins:** each decision links to two `snapshot_reason="decision"`
  snapshots with matching product and book_seq
- **Maker queue evidence:** every maker order must have non-null `queue_ahead_submit`
- **Signal causality:** `available_at ≤ dec_ts` and membership hash validation
- **Fill/hedge joins:** fills link to orders, hedges link to triggers
- **Inventory reconciliation:** positions, exposure, and residual risk
  reconstructed from authoritative fill/hedge events and compared to stored values
- **Outcome route validation:** route steps must follow legal ordering
  (quote→fill→inventory→hedge→eod) with no false claims
- **Duration recomputation:** `inventory_time` recalculated from reconciled exposure
- **Waterfall arithmetic:** `episode_total = maker_capture + quoted_leg + hedge_leg
  − shortfall − fees + rebates`

The research manifest (`meta/research_manifest.json`) contains per-table SHA-256
hashes and the final result. Its own hash is recorded in canonical provenance.

---

## Key Contracts Reference

This is a concise reference to the public types. Constructor arguments are
shown by their current contract names; see `foundation_contracts.py` for the
complete definitions and validation rules.

### Immutable identity types

| Type | Key fields | Purpose |
|------|--------|---------|
| `HedgePairRef` | `pair_id`, `quoted_product`, `hedge_product`, `hedge_mapping_id`, `hedge_mapping_version` | Dual-book identity |
| `BookSnapshotRef` | `product`, `book_seq`, `feed_seq`, `event_id`, `recv_ts`, `available_at`, `snapshot_id`, `snapshot_hash` | Immutable book reference (no depth) |
| `SignalSnapshotRef` | `signal_id`, `product`, `feed_seq`, `event_id`, `snapshot_id`, `snapshot_hash`, `available_at` | Immutable signal reference |
| `CausalSignalSnapshot` | `ref: SignalSnapshotRef`, `payload: Mapping[str, Any]` | Value-bearing signal bound to a decision |

### Decision and execution

| Type | Key fields | Purpose |
|------|--------|---------|
| `DecisionContext` | `run_id`, `decision_id`, `dec_ts`, `feed_seq`, quoted/hedge products and books, consumed signals, input ages | Immutable policy input |
| `OrderIntent` | `intent_id`, `run_id`, `decision_id`, `hedge_pair`, `product`, `role`, `side`, `requested_qty`, `limit_price`, `execution_model_ref` | One leg of a maker/hedge batch |
| `MakerHedgeIntentBatch` | `maker_intent`, `hedge_intent`, `maker_capacity_envelope_id` | S0 batch: optional maker + optional hedge |
| `PolicyProposal` | `batch`, `decision_attributes`, `triggers` | Policy return value |
| `ExecutionResult` | `execution_id`, `intent_id`, `decision_id`, `status`, `requested_qty`, `filled_qty`, `residual_qty`, `levels`, `vwap`, `execution_model_ref` | Aggressive execution outcome |

### Evidence and P&L

| Type | Key fields | Purpose |
|------|--------|---------|
| `PnlViewEvidence` | `evidence_id`, `view_id`, `total_pnl`, methodology and version, `source_artifact_id`, `calculated_at`, `source_artifact` | Independently evidenced P&L view |
| `ValuationMarkEvidence` | `evidence_id`, `product`, `mark`, methodology and version, `source_artifact_id`, `observed_at`, `source_artifact` | Independently evidenced valuation mark |
| `ApprovedEvidenceAuthority` | `authority_id`, `key_id`, `authentication_key` | HMAC-SHA256 signing authority |
| `PnlAttributionResult` | `waterfall_total`, `maker_capture`, `quoted_leg_price_pnl`, `hedge_leg_price_pnl`, `hedge_execution_shortfall`, `fees`, `rebates`, `economics_eligible` | Reconciled P&L |

### Calendar and instrument

| Type | Key fields | Purpose |
|------|--------|---------|
| `SessionCalendar` | `calendar_id`, `timezone`, `version`, `windows`, `trading_day_rollover`, `eod_time`, `holidays` | Trading calendar with EOD |
| `InstrumentSpec` | `product`, `tick`, `multiplier`, `calendar`, `fee_model_id`, `roll_mapping_id` | Per-product configuration |
| `HedgeMappingSpec` | `hedge_pair`, `quoted_risk_weight`, `hedge_risk_weight`, `quantity_tolerance` | Risk-weight mapping |

### P&L waterfall

For signed fill quantity `q`, multiplier `k`, fill price `F`, decision reference
`R`, and accounting mark `M`:

- **Maker capture:** `q × (R − F) × k` (quoted passive fills only)
- **Quoted-leg price P&L:** `q × (M − R) × k`
- **Hedge-leg price P&L:** `q × (M − R) × k`
- **Hedge execution shortfall:** `q × (F − R) × k` (subtracted)
- **Net P&L:** `maker_capture + quoted_leg + hedge_leg − shortfall − fees + rebates`

---

## Adding Tests

Drop a `test_*` function into the matching test file. Use plain `assert`.

```python
def test_my_new_behavior():
    result = some_function(known_input)
    assert result == expected_output
```

From `common/`, run with pytest: `pytest tests/`

---

## Further Reading

- [Foundation Boundary & Compatibility Policy](archive_2608/maker-hedger-foundation-boundary_26-08-08.md) —
  module ownership, contract versioning, calendar/lifecycle/execution details
- [S0 Operational Remediation Plan](archive_2608/maker-hedger-s0-operational-remediation-plan_26-08-08.md) —
  S0 acceptance gates, remediation history, known caveats
- [Telemetry Schema](../contracts/telemetry_schema.md) —
  canonical table definitions, artifact layout, provenance requirements
- [Tests README](../tests/README.md) —
  legacy test suite conventions, helpers, troubleshooting
- [CLAUDE.md](../../CLAUDE.md) —
  fee conventions, project notes
