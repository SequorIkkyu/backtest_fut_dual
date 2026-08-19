# Maker-Hedger Canonical Telemetry Schema

**Schema version:** `0.4.0`
**Owner:** `common.telemetry.TelemetryEmitter`  
**Status:** Phase 5 (G5) and Phase 4c (G4c) supported contract.

## Artifact layout and lifecycle

A run is written under an operator-supplied artifact root as `<run_id>/`. All
rows are canonical JSON Lines in `tables/<table>.jsonl`; a row is appended and
flushed before the call returns. Snapshot payloads are atomic, hash-addressed
canonical JSON in `snapshots/book/<digest>.json` or
`snapshots/signal/<digest>.json`. `provenance/artifacts/` and
`provenance/manifest.json` contain the immutable per-trial provenance set.
`meta/validation.sqlite3`, `meta/invariants.jsonl`, and `meta/run_result.json`
are finalization artifacts.

Every telemetry row has these common fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | The emitted telemetry schema version. |
| `table` / `row_id` | Canonical table and deterministic run-scoped row identity. |
| `run_id` | Trial run identity. |
| `pair_id`, `quoted_product`, `hedge_product` | Mandatory dual-book identity. |
| `hedge_mapping_id`, `hedge_mapping_version` | Declared mapping identity. |

`TelemetryEmitter.finalize()` locks the run. It requires a complete provenance
manifest and writes a `TelemetryRunResult`; any failed `error` invariant sets
`eligible` to `false`.

Every required table must have a retained artifact. For a legitimate no-record
outcome (for example, no fills), call `declare_empty_table(table)` to retain an
explicit empty JSONL artifact. A missing table remains an error-level coverage
failure; declaring an empty table does not waive any other invariant.

## Required canonical tables

| Table | Required fields in addition to the common fields | Purpose |
| --- | --- | --- |
| `decisions` | `decision_id`, `dec_ts`, `feed_seq`, `quoted_book_snapshot_id`, `hedge_book_snapshot_id`, `consumed_signal_snapshot_ids` | Causal policy input. |
| `book_events` | `event_id`, `product`, `exchange_ts`, `recv_ts`, `available_at` | Book input timing and source order. |
| `book_snapshots` | `snapshot_id`, `snapshot_hash`, `product`, `book_seq`, `feed_seq`, `available_at`, `artifact_path` | Durable hash-addressed book state. |
| `orders` | `order_id`, `decision_id`, `product`, `record_type`, `occurred_at` | Declared intent, lifecycle event, and capacity reservation stream. |
| `fills` | `fill_id`, `order_id`, `decision_id`, `product`, `quantity`, `position_delta` | Ledger-recorded maker, hedge, and EOD effects. |
| `hedge_executions` | `execution_id`, `order_id`, `decision_id`, `product`, `status`, `requested_qty`, `filled_qty`, `residual_qty`, `book_snapshot_id`, `decision_feed_seq`, `execution_feed_seq` | Depth-consuming HEDGE/EOD result. |
| `trigger_evaluations` | `trigger_id`, `decision_id`, `occurred_at` | Policy trigger audit. |
| `signal_snapshots` | `snapshot_id`, `snapshot_hash`, `signal_id`, `product`, `feed_seq`, `available_at`, `artifact_path` | Durable consumed signal state. |
| `outcome_pnl` | `outcome_id`, `attribution_status`, `economics_eligible` | Non-attributed marker or Phase-4c reconciled economics outcome. |
| `inventory_series` | `inventory_id`, `occurred_at`, `quoted_position`, `hedge_position`, `pending_hedge_quantity`, `residual_risk` | Dual-leg ledger state. |

`orders.record_type` is one of `order_declared`, `lifecycle`, or
`capacity_reservation`. Lifecycle rows include lifecycle state, resolved and
residual quantities, optional execution ID, and disposition reason. Reservation
rows include envelope identity, action, amount, and the declared capacity.

## Finalization checks

The generic G5 checker emits machine-readable `InvariantResult` records for:

- schema/table coverage and immutable snapshot reconstruction;
- decision snapshot/signal causal availability;
- execution use of its decision-bound snapshot/sequence and duplicate execution IDs;
- capacity-envelope limits;
- one terminal lifecycle outcome per declared order;
- fill-to-order joins and ledger quantities matching the terminal inventory.

`outcome_pnl.attribution_status = not_attributed` requires
`economics_eligible = false` and makes no PnL claim. A Phase-4c `reconciled`
row requires the complete canonical waterfall fields, empty reconciliation
failures, matching accounting/cycle residuals within tolerance, and both
telemetry/EOD reconciliation flags. An `unreconciled` row is retained for
audit but makes the telemetry run ineligible.

## Phase-4c PnL outcome fields

A reconciled outcome includes `maker_capture`, `quoted_leg_price_pnl`,
`hedge_leg_price_pnl`, `hedge_execution_shortfall`, `fees`, `rebates`,
`waterfall_total`, `residual_basis_pnl`, the independent accounting/cycle
totals and their residuals, reconciliation flags/failures, tolerance, and the
unique `attributed_ledger_event_ids`. The checker verifies:

- `waterfall_total = maker_capture + quoted_leg_price_pnl + hedge_leg_price_pnl - hedge_execution_shortfall - fees + rebates`;
- `residual_basis_pnl = quoted_leg_price_pnl + hedge_leg_price_pnl`, reported only as a derived diagnostic; and
- accounting/cycle residuals equal their declared totals less the waterfall and lie within tolerance.

The attribution service also verifies that the IDs correspond one-to-one with
canonical ledger/fill effects and, where applicable, that EOD completion is
consistent with the final ledger state.

## Provenance manifest

`RunProvenance` requires content hashes for `market_data`, `signal_data`,
`configuration`, `code`, `schema`, `fee_profile`, `instrument_roll_mapping`,
and `execution_models`. Its `TrialDeclaration` also records development,
calibration, and holdout windows, candidate-freeze decision, policy version,
pair/mapping, model references, and data-cleaning transformations. A missing or
changed artifact is a distinct trial provenance; do not reuse a winner's
manifest for another candidate run.
