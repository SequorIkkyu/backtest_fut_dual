# Single-Contract Foundation Design

**Date:** 2026-08-14
**Status:** Revised design proposal -- not implemented
**Scope:** A separate, single-product replay surface alongside the strict
dual-book maker-hedger foundation.

## Decision

Implement a **separate single-book foundation**. Do not add an optional hedge
leg, a `mode` flag, or a same-product `HedgePairRef` to the dual foundation.

The existing foundation is intentionally a strict dual-contract simulation. Its
S0 evidence relies on the relationship between the passive quoted leg and the
aggressive hedge leg: residual risk, pending hedge quantity, hedge execution
shortfall, dual-leg reconciliation, and a pair-qualified audit trail. Making a
hedge leg optional would weaken these invariants across the supported
maker-hedger path.

The first supported single-contract use case is the legacy-style **taker**:
one product traded aggressively, with no hedge leg. `TAKER` is therefore a
distinct single-contract role; it must not be represented as dual `HEDGE`.

## Current boundary and implications

The dual nature is not confined to `DualBookFoundation`. It is carried through
the public contracts and leaf services:

| Component | Existing dual constraint | Single-contract implication |
|---|---|---|
| `HedgePairRef`, `DecisionContext`, `OrderIntent`, `ExecutionResult` | Identity, books, and execution results are pair-qualified | Define parallel single-product contracts. |
| `CausalIngress.decision_context` | Requires both pair books in the sealed batch | Reuse sealed-batch mechanics, with a single-book context projection. |
| `DepthExecutionService` | Registers `OrderIntent` against a dual context and emits pair-qualified results | Extract a private generic depth-consumption core; retain dual facade unchanged. |
| `IntentLifecycleService`, `PassiveMatchingService` | Validate or emit pair-qualified intents, events, and fill evidence | Add a single adapter/core only when that capability is in scope. |
| `DualLegLedger`, EOD, PnL | Reconcile two legs and a hedge relationship | Implement explicit single-leg counterparts. |
| Canonical and research telemetry | Require pair joins and hedge-specific fields | Create a separate single-contract schema; reuse artifact plumbing only. |
| `RawSnapshotAdapterConfig` | Requires non-empty queue-proxy interval contracts | Provide an explicit taker-only raw-adapter profile with no proxy interval. |

`DepthBook`, depth consumption, snapshot validation, session calendars,
exchange-batch ordering, stress controls, and artifact-writing primitives are
genuinely reusable. Pair-qualified public types and evidence are not.

## Target architecture

Keep the dual public path untouched:

```text
dual ingress -> ProductionReplayAdapter -> DualBookFoundation
             -> dual ledger / PnL / canonical + research telemetry
```

Add an independent single path with a narrow internal reuse boundary:

```text
single ingress -> SingleProductReplayAdapter -> SingleBookFoundation
               -> single ledger / PnL / canonical + research telemetry
                         |
                         +-> shared internal batch, depth, session, and artifact primitives
```

Suggested additive modules and contracts:

- `single_contract_contracts.py`: `SingleDecisionContext`,
  `SingleOrderIntent`, `SingleExecutionResult`, `SingleIntentBatch`,
  `SingleEodCloseRequest`, and single-leg evidence types.
- `single_contract_ingress.py`: `SingleCausalIngress`, which projects a
  sealed one-product batch into `SingleDecisionContext`.
- `single_contract_api.py`: `SingleBookFoundation`, the single public facade.
- `single_contract_replay.py`: `SingleProductReplayAdapter` and its
  single-product configuration.
- `single_contract_ledger.py` and `single_contract_pnl.py`: a signed-position
  ledger, EOD completion, and an explicitly single-leg waterfall.
- `single_contract_telemetry.py` and `single_contract_research_telemetry.py`:
  separate schemas, emitters, semantic checks, and producer maps.

The exact filenames may change, but the public boundary must remain separate
from the dual types. A single run should be manifest-qualified, for example
`foundation_kind: single-contract`, so it cannot be mistaken for dual
maker-hedger S0 evidence.

## Shared internal extraction

Do not generalize the existing public `DepthExecutionService` to accept
unrelated context and intent unions. Instead, extract a private
`DepthExecutionCore` (name illustrative) which accepts only neutral execution
inputs:

- product, side, requested quantity, limit price, and execution-model values;
- the immutable decision-book reference and currently retained active book;
- the decision and execution sequence/timestamps.

It returns depth-consumption calculations and dispositions. The existing dual
service adapts those calculations into the current `ExecutionResult`; the
single service adapts them into `SingleExecutionResult`. This preserves the
dual API and makes shared causal depth handling testable once.

Apply the same rule elsewhere: extract a generic internal primitive only after
both paths demonstrably need it. Do not pre-emptively make lifecycle,
passive matching, or telemetry accept a broad union of dual and single types.

## Exchange-batch and sequential-event contract

The single path must retain the exchange-time discipline introduced for the
dual replay. Receive time is provenance, not a policy clock or ordering route.

For product `P`, every decision is made from one sealed exchange batch `B(i)`:

1. Require exactly one `P` book snapshot in a configured single-product batch.
2. If the source publishes multiple batches with the same exchange timestamp,
   require explicit source batch sequence values to establish their order.
3. Apply the entire batch atomically and expose a context only after it is
   sealed. Policies see immutable current and previous price views, never
   mutable execution depth.
4. Passive interval matching, once supported, resolves the interval
   `B(i-1) -> B(i)` before the decision for `B(i)` is formed.
5. An order triggered by a fill observed in `B(i)` must carry an explicit
   pricing reference to `B(i-1)`, not an arbitrary event ordering or receive
   timestamp within `B(i)`.
6. Aggressive execution consumes only a retained causally available book;
   consumed depth is never restored. The policy reaction timing for an
   aggressive result must be explicit and deterministic (normally a later
   sealed batch), rather than inferred from local receipt order.

The single decision context should therefore include the current book,
previous book when present, current/previous immutable top-of-book views,
sealed batch identity, feed sequence, consumed signals, and observed fill
identifiers. Single pricing-reference validation should mirror the dual
previous-batch rule for fill-triggered actions.

## Delivery plan

### Phase 0 -- lock the single-contract contract

Define the first supported vertical slice as **aggressive taker plus EOD**.
State the market-order/marketable-limit policy, fill-result reaction timing,
pricing-reference rules, session behaviour, artifact identity, and evidence
status. The initial path has no passive queue proxy and no maker capacity
reservation.

### Phase 1 -- extract no-behaviour-change shared primitives

Extract the private depth-execution core and, if necessary, sealed-batch
storage from the present implementations. Keep all dual public contracts and
acceptance behaviour byte-for-byte compatible where practical. Run the full
dual acceptance suite before adding a single facade.

### Phase 2 -- build the single taker replay vertical slice

Add parallel single contracts, `SingleCausalIngress`, a single raw-adapter
profile, `SingleBookFoundation`, lifecycle registration for `TAKER` and `EOD`,
single-leg ledger/PnL, and `SingleProductReplayAdapter`. Use one product and
one `InstrumentSpec`; no pair mapping is accepted by this route.

### Phase 3 -- single-contract artifacts and acceptance

Add canonical telemetry, research telemetry, schemas, semantic validation,
producer maps, and manifests specific to `foundation_kind: single-contract`.
Test depth consumption, partial/no-liquidity outcomes, EOD flattening,
sessions, sealed-batch ordering, equal-timestamp source sequences,
previous-batch fill-trigger pricing, artifact sealing, and rejection of
dual/single type mixing.

### Phase 4 -- add passive maker only if needed

Introduce single-product maker intents, queue reservation, matcher-issued
single fill evidence, snapshot intervals, and maker-specific telemetry as a
separate expansion. It must retain the same `B(i-1) -> B(i)` matching and
pricing-reference contract. This phase is not a prerequisite for a faithful
single-contract taker example.

## Scope, evidence, and estimate

The single path is a second evidence surface. It can eventually earn its own
promotion status only with its own frozen realistic inputs, holdout evaluation,
telemetry validation, and operational review. Synthetic fixtures prove
infrastructure only. It must never be described as proof for the dual
maker-hedger S0 route.

The original four-day estimate understates the pair-qualified contracts,
artifact schemas, and sequential batch tests. A focused taker-plus-EOD slice
is approximately one concentrated week including acceptance coverage. Full
taker, maker, EOD, canonical telemetry, and research telemetry is closer to
two weeks, depending on raw input and policy-adapter requirements.

## Non-goals

- Do not relax `HedgePairRef` or permit `quoted_product == hedge_product`.
- Do not introduce nullable dual fields or a dual/single mode flag.
- Do not use receive time to order events or derive policy synchronisation.
- Do not repurpose the legacy example path as supported evidence.
- Do not claim single-contract promotion from deterministic fixtures alone.
