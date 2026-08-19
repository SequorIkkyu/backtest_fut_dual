"""Research-owned S0 telemetry export and field-level validation.

The schema implemented here is the engine-export contract from the
Futures-Maker-Hedger research repository.  It is intentionally separate from
the component telemetry emitter: component artifacts remain useful diagnostics,
but only this exporter may label a production replay research-conformant.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from common.foundation_contracts import FoundationContractError, HedgeMappingSpec


RESEARCH_TELEMETRY_SCHEMA_VERSION = "1.5.0"
# Versioned, fail-closed semantic checks below are the S0 containment gate.
S0_SEMANTIC_COMPLIANCE_VERSION = "s0-semantic-compliance-r11"
RESEARCH_MANIFEST_SCHEMA_VERSION = "s0-research-manifest-v2"
RESEARCH_S0_TABLES = (
    "decisions",
    "book_events",
    "book_snapshots",
    "orders",
    "fills",
    "hedge_executions",
    "trigger_evaluations",
    "signal_snapshots",
    "outcome_pnl",
    "inventory_series",
)
_OPTIONAL_RESEARCH_TABLES = ("label_outcomes",)
_SINGLETON_BUSINESS_ID_FIELDS = (
    ("decisions", "decision_id"),
    ("book_snapshots", "snapshot_id"),
    ("orders", "order_id"),
    ("fills", "fill_id"),
    ("hedge_executions", "hedge_id"),
    ("trigger_evaluations", "trigger_id"),
    ("label_outcomes", "label_row_id"),
)

_REQUIRED_FIELDS: Mapping[str, frozenset[str]] = {
    "decisions": frozenset(
        {
            "decision_id",
            "run_id",
            "session_date",
            "exchange_ts",
            "recv_ts",
            "dec_ts",
            "feed_seq",
            "quoted_product",
            "hedge_product",
            "quoted_book_seq",
            "hedge_book_seq",
            "quoted_snapshot_id",
            "hedge_snapshot_id",
            "side",
            "action",
            "quote_price",
            "size",
            "quote_age_ms",
            "queue_ahead",
            "reservation_price",
            "skew",
            "inventory_q",
            "inventory_h",
            "residual_risk",
            "cap_state",
            "capacity_reserved",
            "hedge_mapping_version",
            "signal_set_hash",
            "block_reason",
            "cancel_reason",
            "trigger_priority",
            "hysteresis_state",
        }
    ),
    "book_events": frozenset(
        {
            "run_id",
            "product",
            "session_date",
            "feed_seq",
            "book_seq",
            "exchange_ts",
            "recv_ts",
            "level",
            "side",
            "price",
            "event_type",
            "qty_delta",
            "displayed_qty_after",
            "ofi_delta",
        }
    ),
    "book_snapshots": frozenset(
        {
            "snapshot_id",
            "run_id",
            "decision_id",
            "product",
            "session_date",
            "feed_seq",
            "book_seq",
            "exchange_ts",
            "recv_ts",
            "snapshot_reason",
            "top_k_levels",
            "book_hash",
        }
    ),
    "orders": frozenset(
        {
            "order_id",
            "run_id",
            "decision_id",
            "product",
            "side",
            "order_role",
            "submit_ts",
            "timeout_ts",
            "cancel_ts",
            "price",
            "requested_qty",
            "reserved_capacity",
            "queue_ahead_submit",
            "final_status",
            "cancel_reason",
            "expiry_reason",
        }
    ),
    "fills": frozenset(
        {
            "fill_id",
            "run_id",
            "order_id",
            "decision_id",
            "product",
            "side",
            "fill_ts",
            "feed_seq",
            "book_seq",
            "fill_price",
            "fill_qty",
            "cumulative_fill_qty",
            "queue_ahead_fill",
            "liquidity_role",
            "fee_rebate",
            "match_evidence_type",
            "source_interval_ref",
            "source_interval_quantity",
            "source_interval_bucket_index",
            "source_interval_bucket_price",
            "source_interval_bucket_quantity",
            "raw_file_id",
            "raw_file_hash",
            "raw_row_ordinal",
            "proxy_model_version",
            "price_reach_rule",
            "availability_convention",
        }
    ),
    "hedge_executions": frozenset(
        {
            "hedge_id",
            "run_id",
            "decision_id",
            "trigger_id",
            "product",
            "side",
            "submit_ts",
            "completion_ts",
            "trigger_class",
            "target_before",
            "target_after",
            "requested_qty",
            "filled_qty",
            "depth_levels_consumed",
            "vwap",
            "hedge_touch",
            "mid_at_decision",
            "cost_vs_mid",
            "basis_at_fill",
            "residual_risk_after",
            "retry_count",
            "deadline_ts",
            "disposition",
        }
    ),
    "trigger_evaluations": frozenset(
        {
            "trigger_id",
            "run_id",
            "decision_id",
            "eval_ts",
            "feed_seq",
            "quoted_book_seq",
            "hedge_book_seq",
            "trigger_class",
            "inputs",
            "fired",
            "target",
            "reason",
            "hysteresis_state",
            "cooldown_ms",
        }
    ),
    "signal_snapshots": frozenset(
        {
            "signal_id",
            "signal_snapshot_id",
            "run_id",
            "decision_id",
            "model_version",
            "feature_version",
            "source",
            "score",
            "regime",
            "calibration_bucket",
            "available_at",
            "age_ms",
            "feature_snapshot_hash",
            "feature_coverage",
        }
    ),
    "outcome_pnl": frozenset(
        {
            "run_id",
            "episode_id",
            "session_date",
            "start_ts",
            "end_ts",
            "start_decision_id",
            "end_disposition",
            "maker_capture",
            "quoted_leg_price_pnl",
            "hedge_leg_price_pnl",
            "hedge_execution_shortfall",
            "fees_rebates",
            "residual_basis_attribution",
            "episode_total",
            "inventory_time",
            "route_transitions",
            "eod_result",
            "reconciliation_residual",
        }
    ),
    "inventory_series": frozenset(
        {
            "run_id",
            "ts",
            "feed_seq",
            "session_date",
            "quoted_product",
            "hedge_product",
            "quoted_book_seq",
            "hedge_book_seq",
            "event_source",
            "decision_id",
            "order_id",
            "fill_id",
            "hedge_id",
            "q",
            "h",
            "beta_t",
            "basis",
            "hedge_mapping_version",
            "residual_risk",
            "exposure_risk_scaled",
        }
    ),
    "label_outcomes": frozenset(
        {
            "label_row_id",
            "run_id",
            "label_id",
            "label_version",
            "decision_id",
            "order_id",
            "fill_id",
            "episode_id",
            "anchor_ts",
            "feature_cutoff_ts",
            "outcome_end_ts",
            "label_finalised_at",
            "horizon_type",
            "horizon_value",
            "side",
            "quote_price",
            "requested_qty",
            "fill_fraction",
            "terminal_event",
            "censor_reason",
            "value",
            "value_unit",
            "threshold",
            "quantile",
            "allocation_version",
            "replay_version",
            "policy_id",
            "construction_hash",
            "source_telemetry_hash",
        }
    ),
}


_IDENTITY_FIELDS = frozenset(
    {
        "run_id",
        "schema_version",
        "table",
        "row_id",
        "session_date",
        "pair_id",
        "quoted_product",
        "hedge_product",
        "hedge_mapping_id",
        "hedge_mapping_version",
    }
)
_INTEGER_FIELDS: Mapping[str, frozenset[str]] = {
    "decisions": frozenset({"feed_seq", "quoted_book_seq", "hedge_book_seq", "size"}),
    "book_events": frozenset({"feed_seq", "book_seq", "level", "qty_delta", "displayed_qty_after"}),
    "book_snapshots": frozenset({"feed_seq", "book_seq"}),
    "orders": frozenset({"requested_qty"}),
    "fills": frozenset(
        {
            "feed_seq",
            "book_seq",
            "fill_qty",
            "cumulative_fill_qty",
            "source_interval_quantity",
            "source_interval_bucket_index",
            "source_interval_bucket_quantity",
            "raw_row_ordinal",
        }
    ),
    "hedge_executions": frozenset({"target_before", "target_after", "requested_qty", "filled_qty", "depth_levels_consumed", "retry_count"}),
    "trigger_evaluations": frozenset({"feed_seq", "quoted_book_seq", "hedge_book_seq", "cooldown_ms"}),
    "signal_snapshots": frozenset({"age_ms"}),
    "outcome_pnl": frozenset(),
    "inventory_series": frozenset({"feed_seq", "quoted_book_seq", "hedge_book_seq", "q", "h"}),
    "label_outcomes": frozenset({"requested_qty"}),
}
_NUMBER_FIELDS: Mapping[str, frozenset[str]] = {
    "decisions": frozenset(
        {"quote_price", "quote_age_ms", "queue_ahead", "reservation_price", "skew", "inventory_q", "inventory_h", "residual_risk", "capacity_reserved"}
    ),
    "book_events": frozenset({"price", "ofi_delta"}),
    "book_snapshots": frozenset(),
    "orders": frozenset({"price", "reserved_capacity", "queue_ahead_submit"}),
    "fills": frozenset({"fill_price", "queue_ahead_fill", "fee_rebate", "source_interval_bucket_price"}),
    "hedge_executions": frozenset({"vwap", "hedge_touch", "mid_at_decision", "cost_vs_mid", "basis_at_fill", "residual_risk_after"}),
    "trigger_evaluations": frozenset({"target"}),
    "signal_snapshots": frozenset({"score", "feature_coverage"}),
    "outcome_pnl": frozenset(
        {"maker_capture", "quoted_leg_price_pnl", "hedge_leg_price_pnl", "hedge_execution_shortfall", "fees_rebates", "residual_basis_attribution", "episode_total", "inventory_time", "reconciliation_residual"}
    ),
    "inventory_series": frozenset({"beta_t", "basis", "residual_risk", "exposure_risk_scaled"}),
    "label_outcomes": frozenset({"horizon_value", "quote_price", "fill_fraction", "value", "threshold", "quantile"}),
}
_MAPPING_FIELDS: Mapping[str, frozenset[str]] = {
    "decisions": frozenset(),
    "book_events": frozenset(),
    "book_snapshots": frozenset({"top_k_levels"}),
    "orders": frozenset(),
    "fills": frozenset(),
    "hedge_executions": frozenset(),
    "trigger_evaluations": frozenset({"inputs"}),
    "signal_snapshots": frozenset(),
    "outcome_pnl": frozenset(),
    "inventory_series": frozenset(),
    "label_outcomes": frozenset(),
}
_BOOLEAN_FIELDS: Mapping[str, frozenset[str]] = {
    "decisions": frozenset(),
    "book_events": frozenset(),
    "book_snapshots": frozenset(),
    "orders": frozenset(),
    "fills": frozenset(),
    "hedge_executions": frozenset(),
    "trigger_evaluations": frozenset({"fired"}),
    "signal_snapshots": frozenset(),
    "outcome_pnl": frozenset(),
    "inventory_series": frozenset(),
    "label_outcomes": frozenset(),
}
_NULLABLE_FIELDS: Mapping[str, frozenset[str]] = {
    "decisions": frozenset({"quote_price", "size", "quote_age_ms", "queue_ahead", "block_reason", "cancel_reason", "trigger_priority", "hysteresis_state"}),
    "book_events": frozenset(),
    "book_snapshots": frozenset({"decision_id"}),
    "orders": frozenset({"timeout_ts", "cancel_ts", "reserved_capacity", "queue_ahead_submit", "cancel_reason", "expiry_reason"}),
    "fills": frozenset(
        {
            "source_interval_ref",
            "source_interval_quantity",
            "source_interval_bucket_index",
            "source_interval_bucket_price",
            "source_interval_bucket_quantity",
            "raw_file_id",
            "raw_file_hash",
            "raw_row_ordinal",
            "proxy_model_version",
            "price_reach_rule",
            "availability_convention",
        }
    ),
    "hedge_executions": frozenset({"trigger_id", "deadline_ts"}),
    "trigger_evaluations": frozenset({"target", "reason", "hysteresis_state"}),
    "signal_snapshots": frozenset(),
    "outcome_pnl": frozenset({"episode_id"}),
    "inventory_series": frozenset({"decision_id", "order_id", "fill_id", "hedge_id"}),
    "label_outcomes": frozenset({"decision_id", "order_id", "fill_id", "episode_id", "censor_reason", "threshold", "quantile"}),
}
_ENUM_FIELDS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "decisions": {"side": frozenset({"buy", "sell"}), "action": frozenset({"quote", "cancel", "reprice", "hedge", "no_trade", "blocked"})},
    "book_events": {"side": frozenset({"bid", "ask"}), "event_type": frozenset({"add", "cancel", "trade", "modify"})},
    "book_snapshots": {"snapshot_reason": frozenset({"run_start", "recovery", "decision"})},
    "orders": {"side": frozenset({"buy", "sell"}), "order_role": frozenset({"maker", "hedge"}), "final_status": frozenset({"filled", "partial", "cancelled", "expired", "rejected", "failed"})},
    "fills": {
        "side": frozenset({"buy", "sell"}),
        "liquidity_role": frozenset({"maker", "taker"}),
        "match_evidence_type": frozenset(
            {"trade_level_v1", "snapshot_interval_queue_proxy_v1", "aggressive_execution_v1"}
        ),
    },
    "hedge_executions": {"side": frozenset({"buy", "sell"}), "trigger_class": frozenset({"risk_cap", "risk_soft", "risk_var", "risk_basis", "risk_cb", "value_edge", "value_toxicity", "eod"}), "disposition": frozenset({"filled", "partial", "stale", "deadline", "failure"})},
    "trigger_evaluations": {"trigger_class": frozenset({"risk_cap", "risk_soft", "risk_var", "risk_basis", "risk_cb", "value_edge", "value_toxicity", "eod"})},
    "signal_snapshots": {},
    "outcome_pnl": {},
    "inventory_series": {"event_source": frozenset({"decision", "fill", "hedge", "eod"})},
    "label_outcomes": {"side": frozenset({"buy", "sell"}), "terminal_event": frozenset({"partial_fill", "full_fill", "adverse_barrier", "cancelled", "timeout", "eod", "hedge_complete", "unresolved"})},
}


@dataclass(frozen=True)
class ResearchTelemetryResult:
    """Durable field-level conformance outcome for one research export."""

    run_id: str
    eligible: bool
    errors: tuple[str, ...]
    manifest_hash: str
    semantic_compliance_version: str = S0_SEMANTIC_COMPLIANCE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise FoundationContractError("research telemetry run_id must be a non-empty string")
        if not isinstance(self.eligible, bool):
            raise FoundationContractError("research telemetry eligible must be boolean")
        if not isinstance(self.errors, tuple) or any(not isinstance(error, str) or not error for error in self.errors):
            raise FoundationContractError("research telemetry errors must be a tuple of non-empty strings")
        if not isinstance(self.manifest_hash, str) or not self.manifest_hash.startswith("sha256:") or len(self.manifest_hash) != 71:
            raise FoundationContractError("research telemetry manifest_hash must be a sha256 digest")
        if self.semantic_compliance_version != S0_SEMANTIC_COMPLIANCE_VERSION:
            raise FoundationContractError("research telemetry must use the current semantic compliance version")


class ResearchTelemetryEmitter:
    """Streaming JSONL emitter that enforces the research S0 field contract."""

    def __init__(
        self,
        artifact_root: str | Path,
        run_id: str,
        hedge_mapping: HedgeMappingSpec,
        session_date: date,
        *,
        registered_signal_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise FoundationContractError("run_id must be a non-empty string")
        if not isinstance(hedge_mapping, HedgeMappingSpec):
            raise FoundationContractError("hedge_mapping must be a HedgeMappingSpec")
        if not isinstance(session_date, date):
            raise FoundationContractError("session_date must be a date")
        if not isinstance(registered_signal_ids, frozenset) or any(
            not isinstance(signal_id, str) or not signal_id.strip() for signal_id in registered_signal_ids
        ):
            raise FoundationContractError("registered_signal_ids must be a frozenset of non-empty strings")
        self.run_id = run_id
        self.hedge_mapping = hedge_mapping
        self.hedge_pair = hedge_mapping.hedge_pair
        self.session_date = session_date
        self.registered_signal_ids = registered_signal_ids
        self.run_dir = Path(artifact_root).resolve() / run_id
        if self.run_dir.exists():
            raise FoundationContractError("research telemetry run artifact directory already exists")
        (self.run_dir / "tables").mkdir(parents=True)
        (self.run_dir / "meta").mkdir()
        self._declared_empty: set[str] = set()
        self._finalized: ResearchTelemetryResult | None = None

    def emit(self, table: str, entity_id: str, fields: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate and append one full research-contract telemetry row."""
        self._ensure_open()
        if table not in _REQUIRED_FIELDS:
            raise FoundationContractError("unknown research telemetry table")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise FoundationContractError("entity_id must be a non-empty string")
        if not isinstance(fields, Mapping):
            raise FoundationContractError("research telemetry fields must be a mapping")
        supplied = dict(fields)
        if table == "fills":
            supplied = {
                "match_evidence_type": "trade_level_v1",
                "source_interval_ref": None,
                "source_interval_quantity": None,
                "source_interval_bucket_index": None,
                "source_interval_bucket_price": None,
                "source_interval_bucket_quantity": None,
                "raw_file_id": None,
                "raw_file_hash": None,
                "raw_row_ordinal": None,
                "proxy_model_version": None,
                "price_reach_rule": None,
                "availability_convention": None,
                **supplied,
            }
        forbidden = {
            "run_id",
            "schema_version",
            "table",
            "row_id",
            "session_date",
            "pair_id",
            "quoted_product",
            "hedge_product",
            "hedge_mapping_id",
            "hedge_mapping_version",
        }
        if forbidden & set(supplied):
            raise FoundationContractError("research telemetry identity fields are owned by the emitter")
        row = {
            "run_id": self.run_id,
            "schema_version": RESEARCH_TELEMETRY_SCHEMA_VERSION,
            "table": table,
            "row_id": f"{self.run_id}:{table}:{entity_id}",
            "session_date": self.session_date.isoformat(),
            "pair_id": self.hedge_pair.pair_id,
            "quoted_product": self.hedge_pair.quoted_product,
            "hedge_product": self.hedge_pair.hedge_product,
            "hedge_mapping_id": self.hedge_pair.hedge_mapping_id,
            "hedge_mapping_version": self.hedge_pair.hedge_mapping_version,
            **supplied,
        }
        missing = _REQUIRED_FIELDS[table] - set(row)
        if missing:
            raise FoundationContractError(f"research telemetry row is missing required {table} fields: {sorted(missing)}")
        unexpected = set(row) - (_IDENTITY_FIELDS | _REQUIRED_FIELDS[table])
        if unexpected:
            raise FoundationContractError(f"research telemetry row has unsupported {table} fields: {sorted(unexpected)}")
        self._validate_row(table, row)
        self._append(self.run_dir / "tables" / f"{table}.jsonl", row)
        return row

    def declare_empty_table(self, table: str) -> None:
        self._ensure_open()
        if table not in RESEARCH_S0_TABLES:
            raise FoundationContractError("only S0 research telemetry tables may be declared empty")
        path = self.run_dir / "tables" / f"{table}.jsonl"
        path.touch(exist_ok=True)
        self._declared_empty.add(table)

    def finalize(self) -> ResearchTelemetryResult:
        """Seal the export after confirming every mandatory S0 table exists."""
        if self._finalized is not None:
            return self._finalized
        table_paths = {
            table: self.run_dir / "tables" / f"{table}.jsonl"
            for table in (*RESEARCH_S0_TABLES, *_OPTIONAL_RESEARCH_TABLES)
        }
        missing = tuple(table for table in RESEARCH_S0_TABLES if not table_paths[table].exists())
        sealed_tables = tuple(table for table, path in table_paths.items() if path.exists())
        errors = [f"missing_table:{table}" for table in missing]
        rows, schema_errors = self._load_validated_rows(sealed_tables)
        errors.extend(schema_errors)
        if not missing and not schema_errors:
            errors.extend(self._cross_table_errors(rows))
        result_core = {
            "run_id": self.run_id,
            "eligible": not errors,
            "errors": list(errors),
            "semantic_compliance_version": S0_SEMANTIC_COMPLIANCE_VERSION,
        }
        table_hashes = {
            table: (
                _sha256_bytes(table_paths[table].read_bytes())
                if table_paths[table].exists()
                else None
            )
            for table in (*RESEARCH_S0_TABLES, *_OPTIONAL_RESEARCH_TABLES)
        }
        manifest_payload = {
            "schema_version": RESEARCH_MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "tables": table_hashes,
            "research_result": result_core,
        }
        manifest_bytes = _json_bytes(manifest_payload)
        manifest_hash = _sha256_bytes(manifest_bytes)
        result = ResearchTelemetryResult(self.run_id, not errors, tuple(errors), manifest_hash)
        self._atomic_write(
            self.run_dir / "meta" / "research_result.json",
            _json_bytes(
                {
                    **result_core,
                    "research_manifest_hash": result.manifest_hash,
                }
            ),
        )
        self._atomic_write(self.run_dir / "meta" / "research_manifest.json", manifest_bytes)
        self._finalized = result
        return result

    def _validate_row(self, table: str, row: Mapping[str, Any]) -> None:
        if table == "signal_snapshots" and row["signal_id"] not in self.registered_signal_ids:
            raise FoundationContractError("signal_id is not registered for this research telemetry run")
        for field_name in _timestamp_fields(table):
            _require_aware_or_null(row[field_name], field_name)
        for field_name, value in row.items():
            if value is None:
                if field_name not in _NULLABLE_FIELDS[table]:
                    raise FoundationContractError(f"research telemetry {table}.{field_name} must not be null")
                continue
            if field_name in _INTEGER_FIELDS[table] and (not isinstance(value, int) or isinstance(value, bool)):
                raise FoundationContractError(f"research telemetry {table}.{field_name} must be an integer")
            if field_name in _NUMBER_FIELDS[table] and (
                not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))
            ):
                raise FoundationContractError(f"research telemetry {table}.{field_name} must be a finite number")
            if field_name in _MAPPING_FIELDS[table] and not isinstance(value, Mapping):
                raise FoundationContractError(f"research telemetry {table}.{field_name} must be a mapping")
            if field_name in _BOOLEAN_FIELDS[table] and not isinstance(value, bool):
                raise FoundationContractError(f"research telemetry {table}.{field_name} must be a bool")
            allowed = _ENUM_FIELDS[table].get(field_name)
            if allowed is not None and value not in allowed:
                raise FoundationContractError(
                    f"research telemetry {table}.{field_name} must be one of {sorted(allowed)}"
                )
            if field_name not in _INTEGER_FIELDS[table] | _NUMBER_FIELDS[table] | _MAPPING_FIELDS[table] | _BOOLEAN_FIELDS[table] | set(_timestamp_fields(table)):
                if not isinstance(value, str):
                    raise FoundationContractError(f"research telemetry {table}.{field_name} must be a string")
        if table == "hedge_executions" and row["product"] != self.hedge_pair.hedge_product:
            raise FoundationContractError("research hedge execution product must be the declared hedge product")

    def _cross_table_errors(self, rows: Mapping[str, tuple[Mapping[str, Any], ...]]) -> list[str]:
        """Validate durable joins and causal timestamp relationships at seal time."""
        errors: list[str] = []
        for table, values in rows.items():
            ids = [row["row_id"] for row in values]
            if len(ids) != len(set(ids)):
                errors.append(f"duplicate_row_id:{table}")
        # ``row_id`` protects the emitter-owned durable record identity;
        # singleton business IDs protect the domain facts referenced by joins.
        for table, business_id_field in _SINGLETON_BUSINESS_ID_FIELDS:
            if table not in rows:
                continue
            seen_business_ids: set[str] = set()
            duplicate_business_ids: set[str] = set()
            for row in rows[table]:
                business_id = row[business_id_field]
                if business_id in seen_business_ids:
                    duplicate_business_ids.add(business_id)
                seen_business_ids.add(business_id)
            errors.extend(
                f"duplicate_{business_id_field}:{business_id}"
                for business_id in sorted(duplicate_business_ids)
            )
        outcome_rows = rows.get("outcome_pnl", ())
        aggregate_outcomes = sum(row["episode_id"] is None for row in outcome_rows)
        if aggregate_outcomes != 1:
            errors.append(f"outcome_aggregate_cardinality:{aggregate_outcomes}")
        seen_episode_ids: set[str] = set()
        duplicate_episode_ids: set[str] = set()
        for row in outcome_rows:
            episode_id = row["episode_id"]
            if episode_id is None:
                continue
            if episode_id in seen_episode_ids:
                duplicate_episode_ids.add(episode_id)
            seen_episode_ids.add(episode_id)
        errors.extend(f"duplicate_episode_id:{episode_id}" for episode_id in sorted(duplicate_episode_ids))
        # Singleton business identities are authorities. Do not construct the
        # join dictionaries below after finding an ambiguity: a dict would
        # silently select the final duplicate row as its authority.
        if errors:
            return errors
        decisions = {row["decision_id"]: row for row in rows["decisions"]}
        snapshots = {row["snapshot_id"]: row for row in rows["book_snapshots"]}
        orders = {row["order_id"]: row for row in rows["orders"]}
        fills = {row["fill_id"]: row for row in rows["fills"]}
        triggers = {row["trigger_id"]: row for row in rows["trigger_evaluations"]}
        signal_rows = rows["signal_snapshots"]
        outcomes_by_episode_id = {
            row["episode_id"]: row for row in outcome_rows if row["episode_id"] is not None
        }

        for row in rows["book_snapshots"]:
            reason = row["snapshot_reason"]
            decision_id = row["decision_id"]
            if reason == "decision" and decision_id is None:
                errors.append(f"decision_snapshot_missing_decision:{row['snapshot_id']}")
            if reason != "decision" and decision_id is not None:
                errors.append(f"nondecision_snapshot_has_decision:{row['snapshot_id']}")
            if decision_id is not None and decision_id not in decisions:
                errors.append(f"book_snapshot_unknown_decision:{row['snapshot_id']}")
        for decision_id, decision in decisions.items():
            required_snapshots = (
                ("quoted_snapshot_id", self.hedge_pair.quoted_product, "quoted_book_seq"),
                ("hedge_snapshot_id", self.hedge_pair.hedge_product, "hedge_book_seq"),
            )
            for field_name, product, sequence_field in required_snapshots:
                snapshot = snapshots.get(decision[field_name])
                if snapshot is None:
                    errors.append(f"decision_missing_snapshot:{decision_id}:{field_name}")
                    continue
                if snapshot["decision_id"] != decision_id or snapshot["snapshot_reason"] != "decision":
                    errors.append(f"decision_snapshot_link:{decision_id}:{field_name}")
                if snapshot["product"] != product or snapshot["book_seq"] != decision[sequence_field]:
                    errors.append(f"decision_snapshot_identity:{decision_id}:{field_name}")
        for row in rows["orders"]:
            if row["decision_id"] not in decisions:
                errors.append(f"order_unknown_decision:{row['order_id']}")
            if row["order_role"] == "maker" and row["queue_ahead_submit"] is None:
                errors.append(f"maker_queue_ahead_missing:{row['order_id']}")
        for row in rows["fills"]:
            order = orders.get(row["order_id"])
            if order is None or order["decision_id"] != row["decision_id"]:
                errors.append(f"fill_order_join:{row['fill_id']}")
                continue
            if order["product"] != row["product"] or order["side"] != row["side"]:
                errors.append(f"fill_order_identity:{row['fill_id']}")
            evidence_type = row["match_evidence_type"]
            proxy_fields = (
                "source_interval_ref",
                "source_interval_quantity",
                "source_interval_bucket_index",
                "source_interval_bucket_price",
                "source_interval_bucket_quantity",
                "raw_file_id",
                "raw_file_hash",
                "raw_row_ordinal",
                "proxy_model_version",
                "price_reach_rule",
                "availability_convention",
            )
            if evidence_type == "snapshot_interval_queue_proxy_v1":
                valid_proxy = (
                    order["order_role"] == "maker"
                    and row["product"] == self.hedge_pair.quoted_product
                    and isinstance(row["source_interval_ref"], str)
                    and bool(row["source_interval_ref"])
                    and isinstance(row["source_interval_quantity"], int)
                    and row["source_interval_quantity"] > 0
                    and isinstance(row["source_interval_bucket_index"], int)
                    and row["source_interval_bucket_index"] >= 0
                    and isinstance(row["source_interval_bucket_price"], (int, float))
                    and math.isfinite(float(row["source_interval_bucket_price"]))
                    and float(row["source_interval_bucket_price"]) > 0
                    and isinstance(row["source_interval_bucket_quantity"], int)
                    and 0 < row["source_interval_bucket_quantity"] <= row["source_interval_quantity"]
                    and isinstance(row["raw_file_id"], str)
                    and bool(row["raw_file_id"])
                    and isinstance(row["raw_file_hash"], str)
                    and row["raw_file_hash"].startswith("sha256:")
                    and isinstance(row["raw_row_ordinal"], int)
                    and row["raw_row_ordinal"] >= 0
                    and isinstance(row["proxy_model_version"], str)
                    and bool(row["proxy_model_version"])
                    and row["price_reach_rule"] == "bid_then_ask_v1"
                    and row["availability_convention"] == "max_exchange_timestamp_v1"
                )
                if not valid_proxy:
                    errors.append(f"snapshot_proxy_fields:{row['fill_id']}")
                matching_snapshots = tuple(
                    snapshot
                    for snapshot in snapshots.values()
                    if snapshot["product"] == row["product"]
                    and snapshot["book_seq"] == row["book_seq"]
                    and snapshot["feed_seq"] <= row["feed_seq"]
                    and snapshot["exchange_ts"] <= row["fill_ts"]
                )
                if not matching_snapshots:
                    errors.append(f"snapshot_proxy_book_causality:{row['fill_id']}")
            elif any(row[field_name] is not None for field_name in proxy_fields):
                errors.append(f"nonproxy_fill_has_snapshot_fields:{row['fill_id']}")
        errors.extend(self._order_fill_errors(rows["orders"], rows["fills"]))
        proxy_bucket_fills: dict[tuple[str, int, int], int] = {}
        for row in rows["fills"]:
            if row["match_evidence_type"] != "snapshot_interval_queue_proxy_v1":
                continue
            key = (
                str(row["source_interval_ref"]),
                int(row["source_interval_bucket_index"]),
                int(row["source_interval_bucket_quantity"]),
            )
            proxy_bucket_fills[key] = proxy_bucket_fills.get(key, 0) + int(row["fill_qty"])
        for (interval_ref, bucket_index, bucket_quantity), filled_quantity in proxy_bucket_fills.items():
            if filled_quantity > bucket_quantity:
                errors.append(f"snapshot_proxy_bucket_conservation:{interval_ref}:{bucket_index}")
        for row in rows["hedge_executions"]:
            if row["decision_id"] not in decisions:
                errors.append(f"hedge_unknown_decision:{row['hedge_id']}")
                continue
            if row["trigger_id"] is not None:
                trigger = triggers.get(row["trigger_id"])
                if trigger is None:
                    errors.append(f"hedge_trigger_join:{row['hedge_id']}")
                else:
                    if not trigger["fired"]:
                        errors.append(f"hedge_trigger_not_fired:{row['hedge_id']}")
                    if trigger["target"] is not None and row["target_after"] != trigger["target"]:
                        errors.append(f"hedge_trigger_target:{row['hedge_id']}")
            decision = decisions[row["decision_id"]]
            quoted_snapshot = snapshots.get(decision["quoted_snapshot_id"])
            hedge_snapshot = snapshots.get(decision["hedge_snapshot_id"])
            if quoted_snapshot is None or hedge_snapshot is None:
                errors.append(f"hedge_basis_snapshot_join:{row['hedge_id']}")
            else:
                expected_basis = _snapshot_midpoint(quoted_snapshot) - _snapshot_midpoint(hedge_snapshot)
                if not math.isclose(float(row["basis_at_fill"]), expected_basis, rel_tol=0.0, abs_tol=1e-9):
                    errors.append(f"hedge_basis_at_fill:{row['hedge_id']}")
        for row in rows["trigger_evaluations"]:
            if row["decision_id"] not in decisions:
                errors.append(f"trigger_unknown_decision:{row['trigger_id']}")
        for row in signal_rows:
            decision = decisions.get(row["decision_id"])
            if decision is None:
                errors.append(f"signal_unknown_decision:{row['signal_id']}")
            elif row["available_at"] > decision["dec_ts"]:
                errors.append(f"signal_after_decision:{row['signal_id']}")
        for decision_id, decision in decisions.items():
            snapshot_ids = [row["signal_snapshot_id"] for row in signal_rows if row["decision_id"] == decision_id]
            if len(snapshot_ids) != len(set(snapshot_ids)):
                errors.append(f"signal_duplicate_snapshot:{decision_id}")
            expected_signal_set_hash = _signal_set_hash(snapshot_ids)
            if decision["signal_set_hash"] != expected_signal_set_hash:
                errors.append(f"signal_set_membership:{decision_id}")
        hedge_executions = {row["hedge_id"]: row for row in rows["hedge_executions"]}
        for row in rows["inventory_series"]:
            for field_name, known in (
                ("decision_id", decisions),
                ("order_id", orders),
                ("fill_id", fills),
                ("hedge_id", hedge_executions),
            ):
                if row[field_name] is not None and row[field_name] not in known:
                    errors.append(f"inventory_{field_name}_join:{row['row_id']}")
        for row in outcome_rows:
            if row["start_decision_id"] not in decisions:
                errors.append(f"outcome_unknown_decision:{row['row_id']}")
            if row["end_ts"] < row["start_ts"]:
                errors.append(f"outcome_time_order:{row['row_id']}")
            expected_total = (
                float(row["maker_capture"])
                + float(row["quoted_leg_price_pnl"])
                + float(row["hedge_leg_price_pnl"])
                - float(row["hedge_execution_shortfall"])
                + float(row["fees_rebates"])
            )
            if not math.isclose(float(row["episode_total"]), expected_total, rel_tol=0.0, abs_tol=1e-9):
                errors.append(f"outcome_waterfall_total:{row['row_id']}")
            route = row["route_transitions"]
            route_steps = tuple(route.split("->"))
            errors.extend(
                self._outcome_inventory_errors(
                    row,
                    rows["inventory_series"],
                    rows["fills"],
                    rows["hedge_executions"],
                    decisions,
                    route_steps,
                )
            )
            if route == "no_trade":
                if rows["fills"] or any(row["filled_qty"] > 0 for row in rows["hedge_executions"]):
                    errors.append(f"outcome_false_no_trade:{row['row_id']}")
                if not math.isclose(float(row["inventory_time"]), 0.0, rel_tol=0.0, abs_tol=1e-12):
                    errors.append(f"outcome_no_trade_inventory:{row['row_id']}")
            else:
                if rows["fills"] and "fill" not in route_steps:
                    errors.append(f"outcome_missing_fill_route:{row['row_id']}")
                if any(row["filled_qty"] > 0 for row in rows["hedge_executions"]) and "hedge" not in route_steps:
                    errors.append(f"outcome_missing_hedge_route:{row['row_id']}")
                if float(row["inventory_time"]) > 0.0 and "inventory" not in route_steps:
                    errors.append(f"outcome_missing_inventory_route:{row['row_id']}")
        for row in rows.get("label_outcomes", ()):
            label_id = row["label_row_id"]
            decision = decisions.get(row["decision_id"]) if row["decision_id"] is not None else None
            order = orders.get(row["order_id"]) if row["order_id"] is not None else None
            fill = fills.get(row["fill_id"]) if row["fill_id"] is not None else None
            feature_cutoff_ts = _sealed_timestamp(row["feature_cutoff_ts"], "label feature_cutoff_ts")
            anchor_ts = _sealed_timestamp(row["anchor_ts"], "label anchor_ts")
            outcome_end_ts = _sealed_timestamp(row["outcome_end_ts"], "label outcome_end_ts")
            label_finalised_at = _sealed_timestamp(row["label_finalised_at"], "label label_finalised_at")
            if row["decision_id"] is not None and decision is None:
                errors.append(f"label_unknown_decision:{label_id}")
            if row["order_id"] is not None and order is None:
                errors.append(f"label_unknown_order:{label_id}")
            if row["fill_id"] is not None and fill is None:
                errors.append(f"label_unknown_fill:{label_id}")
            if decision is not None and order is not None and order["decision_id"] != decision["decision_id"]:
                errors.append(f"label_order_decision_join:{label_id}")
            if decision is not None and fill is not None and fill["decision_id"] != decision["decision_id"]:
                errors.append(f"label_fill_decision_join:{label_id}")
            if order is not None and fill is not None and fill["order_id"] != order["order_id"]:
                errors.append(f"label_fill_order_join:{label_id}")
            if order is not None and row["side"] != order["side"]:
                errors.append(f"label_order_side:{label_id}")
            if fill is not None and row["side"] != fill["side"]:
                errors.append(f"label_fill_side:{label_id}")
            if row["episode_id"] is not None and row["episode_id"] not in outcomes_by_episode_id:
                errors.append(f"label_unknown_episode:{label_id}")
            if feature_cutoff_ts > anchor_ts:
                errors.append(f"label_feature_after_anchor:{label_id}")
            if outcome_end_ts < anchor_ts:
                errors.append(f"label_outcome_before_anchor:{label_id}")
            if label_finalised_at < outcome_end_ts:
                errors.append(f"label_finalised_before_outcome:{label_id}")
        return errors

    def _outcome_inventory_errors(
        self,
        outcome: Mapping[str, Any],
        inventory_rows: tuple[Mapping[str, Any], ...],
        fills: tuple[Mapping[str, Any], ...],
        hedge_executions: tuple[Mapping[str, Any], ...],
        decisions: Mapping[str, Mapping[str, Any]],
        route_steps: tuple[str, ...],
    ) -> list[str]:
        """Reconcile inventory from execution facts, then validate duration and route.

        ``inventory_series`` is a projection, not an authority. Each material
        state transition must name the fill or hedge execution that produced
        it, and the resulting positions and scaled exposure must agree with
        the transition reconstructed while sealing the research artifact.
        """
        outcome_id = outcome["row_id"]
        start_ts = outcome["start_ts"]
        end_ts = outcome["end_ts"]
        # At an equal receive-time the replay matches passive trades before it
        # schedules the next decision, then executes scheduled hedges. EOD
        # retains its terminal position after any EOD execution.
        event_order = {"fill": 0, "decision": 1, "hedge": 2, "eod": 3}
        relevant = sorted(
            (row for row in inventory_rows if start_ts <= row["ts"] <= end_ts),
            key=lambda row: (
                row["ts"],
                event_order[row["event_source"]],
                row["event_source"] == "eod" and row["fill_id"] is None and row["hedge_id"] is None,
                row["row_id"],
            ),
        )
        errors: list[str] = []
        start_rows = [
            row
            for row in relevant
            if row["ts"] == start_ts
            and row["event_source"] == "decision"
            and row["decision_id"] == outcome["start_decision_id"]
        ]
        if len(start_rows) != 1:
            errors.append(f"outcome_inventory_start:{outcome_id}")
            return errors
        if not any(row["ts"] == end_ts and row["event_source"] == "eod" for row in relevant):
            errors.append(f"outcome_inventory_end:{outcome_id}")
            return errors
        start_decision = decisions.get(outcome["start_decision_id"])
        if start_decision is None:
            errors.append(f"outcome_unknown_decision:{outcome_id}")
            return errors

        expected_q = int(start_decision["inventory_q"])
        expected_h = int(start_decision["inventory_h"])
        fills_by_id = {row["fill_id"]: row for row in fills}
        hedges_by_id = {row["hedge_id"]: row for row in hedge_executions}
        seen_fill_ids: set[str] = set()
        seen_hedge_ids: set[str] = set()
        state_exposure: list[tuple[Mapping[str, Any], float]] = []

        for row in relevant:
            source = row["event_source"]
            fill_id = row["fill_id"]
            hedge_id = row["hedge_id"]
            if source == "decision":
                if fill_id is not None or hedge_id is not None or row["order_id"] is not None:
                    errors.append(f"inventory_decision_link:{row['row_id']}")
            elif source == "fill":
                if fill_id is None or hedge_id is not None:
                    errors.append(f"inventory_fill_link:{row['row_id']}")
                else:
                    fill = fills_by_id.get(fill_id)
                    if fill is None:
                        errors.append(f"inventory_fill_join:{row['row_id']}")
                    else:
                        if (
                            fill["liquidity_role"] != "maker"
                            or fill["order_id"] != row["order_id"]
                            or _sealed_timestamp(fill["fill_ts"], "fill timestamp") != _sealed_timestamp(row["ts"], "inventory timestamp")
                        ):
                            errors.append(f"inventory_fill_identity:{row['row_id']}")
                        elif fill_id in seen_fill_ids:
                            errors.append(f"inventory_duplicate_fill_state:{outcome_id}:{fill_id}")
                        else:
                            expected_q, expected_h = self._apply_inventory_fill(expected_q, expected_h, fill)
                            seen_fill_ids.add(fill_id)
            elif source == "hedge":
                if hedge_id is None or fill_id is not None:
                    errors.append(f"inventory_hedge_link:{row['row_id']}")
                else:
                    hedge = hedges_by_id.get(hedge_id)
                    if hedge is None:
                        errors.append(f"inventory_hedge_join:{row['row_id']}")
                    else:
                        if (
                            hedge["product"] != self.hedge_pair.hedge_product
                            or _sealed_timestamp(hedge["completion_ts"], "hedge completion timestamp")
                            != _sealed_timestamp(row["ts"], "inventory timestamp")
                        ):
                            errors.append(f"inventory_hedge_identity:{row['row_id']}")
                        elif hedge_id in seen_hedge_ids:
                            errors.append(f"inventory_duplicate_hedge_state:{outcome_id}:{hedge_id}")
                        else:
                            expected_q, expected_h = self._apply_inventory_hedge(expected_q, expected_h, hedge)
                            seen_hedge_ids.add(hedge_id)
            else:  # eod
                if fill_id is not None and hedge_id is not None:
                    errors.append(f"inventory_eod_link:{row['row_id']}")
                elif fill_id is not None:
                    fill = fills_by_id.get(fill_id)
                    if fill is None:
                        errors.append(f"inventory_fill_join:{row['row_id']}")
                    elif (
                        fill["liquidity_role"] != "taker"
                        or fill["order_id"] != row["order_id"]
                        or _sealed_timestamp(fill["fill_ts"], "fill timestamp")
                        != _sealed_timestamp(row["ts"], "inventory timestamp")
                    ):
                        errors.append(f"inventory_eod_fill_identity:{row['row_id']}")
                    elif fill_id in seen_fill_ids:
                        errors.append(f"inventory_duplicate_fill_state:{outcome_id}:{fill_id}")
                    else:
                        expected_q, expected_h = self._apply_inventory_fill(expected_q, expected_h, fill)
                        seen_fill_ids.add(fill_id)
                elif hedge_id is not None:
                    hedge = hedges_by_id.get(hedge_id)
                    if hedge is None:
                        errors.append(f"inventory_hedge_join:{row['row_id']}")
                    elif (
                        hedge["product"] != self.hedge_pair.hedge_product
                        or _sealed_timestamp(hedge["completion_ts"], "hedge completion timestamp")
                        != _sealed_timestamp(row["ts"], "inventory timestamp")
                    ):
                        errors.append(f"inventory_eod_hedge_identity:{row['row_id']}")
                    elif hedge_id in seen_hedge_ids:
                        errors.append(f"inventory_duplicate_hedge_state:{outcome_id}:{hedge_id}")
                    else:
                        expected_q, expected_h = self._apply_inventory_hedge(expected_q, expected_h, hedge)
                        seen_hedge_ids.add(hedge_id)

            if int(row["q"]) != expected_q or int(row["h"]) != expected_h:
                errors.append(f"inventory_position_reconciliation:{row['row_id']}")
            expected_beta = float(self.hedge_mapping.quoted_risk_weight) / float(self.hedge_mapping.hedge_risk_weight)
            if not math.isclose(float(row["beta_t"]), expected_beta, rel_tol=0.0, abs_tol=1e-12):
                errors.append(f"inventory_beta_mapping:{row['row_id']}")
            expected_exposure = expected_q * expected_beta + expected_h
            if not math.isclose(float(row["exposure_risk_scaled"]), expected_exposure, rel_tol=0.0, abs_tol=1e-12):
                errors.append(f"inventory_exposure_reconciliation:{row['row_id']}")
            expected_residual_risk = self.hedge_mapping.residual_risk(expected_q, expected_h)
            if not math.isclose(
                float(row["residual_risk"]), expected_residual_risk, rel_tol=0.0, abs_tol=1e-12
            ):
                errors.append(f"inventory_residual_reconciliation:{row['row_id']}")
            state_exposure.append((row, expected_exposure))

        expected_fill_ids = {
            row["fill_id"]
            for row in fills
            if start_ts <= row["fill_ts"] <= end_ts and row["fill_qty"] > 0
        }
        expected_hedge_ids = {
            row["hedge_id"]
            for row in hedge_executions
            if start_ts <= row["completion_ts"] <= end_ts and row["filled_qty"] > 0
        }
        for fill_id in sorted(expected_fill_ids - seen_fill_ids):
            errors.append(f"inventory_missing_fill_state:{outcome_id}:{fill_id}")
        for hedge_id in sorted(expected_hedge_ids - seen_hedge_ids):
            errors.append(f"inventory_missing_hedge_state:{outcome_id}:{hedge_id}")

        expected_duration = 0.0
        inventory_seen = False
        for (current, exposure), (following, _) in zip(state_exposure, state_exposure[1:]):
            current_ts = _sealed_timestamp(current["ts"], "inventory timestamp")
            following_ts = _sealed_timestamp(following["ts"], "inventory timestamp")
            if current_ts > following_ts:
                errors.append(f"outcome_inventory_order:{outcome_id}")
                return errors
            risk_open = not math.isclose(exposure, 0.0, rel_tol=0.0, abs_tol=1e-12)
            inventory_seen = inventory_seen or risk_open
            if risk_open:
                expected_duration += (following_ts - current_ts).total_seconds()
        if not math.isclose(float(outcome["inventory_time"]), expected_duration, rel_tol=0.0, abs_tol=1e-9):
            errors.append(f"outcome_inventory_duration:{outcome_id}")
        allowed_steps = {"quote", "fill", "inventory", "hedge", "eod", "cancel", "no_trade"}
        if not route_steps or any(step not in allowed_steps for step in route_steps) or len(route_steps) != len(set(route_steps)):
            errors.append(f"outcome_route_shape:{outcome_id}")
            return errors
        if route_steps == ("no_trade",):
            return errors
        if "no_trade" in route_steps:
            errors.append(f"outcome_route_no_trade_mixed:{outcome_id}")
        if "quote" in route_steps and route_steps[0] != "quote":
            errors.append(f"outcome_route_quote_order:{outcome_id}")
        if "fill" in route_steps and ("quote" not in route_steps or route_steps.index("fill") < route_steps.index("quote")):
            errors.append(f"outcome_route_fill_order:{outcome_id}")
        if "cancel" in route_steps and ("quote" not in route_steps or route_steps[-1] != "cancel"):
            errors.append(f"outcome_route_cancel_order:{outcome_id}")
        if "eod" in route_steps and route_steps[-1] != "eod":
            errors.append(f"outcome_route_eod_order:{outcome_id}")
        if "cancel" in route_steps and route_steps != ("quote", "cancel"):
            errors.append(f"outcome_route_cancel_activity:{outcome_id}")
        if "inventory" in route_steps and "fill" in route_steps and route_steps.index("inventory") < route_steps.index("fill"):
            errors.append(f"outcome_route_inventory_order:{outcome_id}")
        if "hedge" in route_steps and ("inventory" not in route_steps or route_steps.index("hedge") < route_steps.index("inventory")):
            errors.append(f"outcome_route_hedge_order:{outcome_id}")
        eod_activity = any(
            row["filled_qty"] > 0 and ":eod:" in row["decision_id"] for row in hedge_executions
        ) or any(row["fill_qty"] > 0 and ":eod:" in row["decision_id"] for row in fills)
        if eod_activity and "eod" not in route_steps:
            errors.append(f"outcome_missing_eod_route:{outcome_id}")
        if not eod_activity and "eod" in route_steps:
            errors.append(f"outcome_false_eod_route:{outcome_id}")
        if inventory_seen and "inventory" not in route_steps:
            errors.append(f"outcome_missing_inventory_route:{outcome_id}")
        if not inventory_seen and "inventory" in route_steps:
            errors.append(f"outcome_false_inventory_route:{outcome_id}")
        return errors

    def _apply_inventory_fill(self, quoted_position: int, hedge_position: int, fill: Mapping[str, Any]) -> tuple[int, int]:
        """Apply one fill-table fact to the inventory projection."""
        delta = int(fill["fill_qty"]) * (1 if fill["side"] == "buy" else -1)
        if fill["product"] == self.hedge_pair.quoted_product:
            return quoted_position + delta, hedge_position
        if fill["product"] == self.hedge_pair.hedge_product:
            return quoted_position, hedge_position + delta
        raise FoundationContractError("research fill product is outside the declared hedge pair")

    def _apply_inventory_hedge(self, quoted_position: int, hedge_position: int, hedge: Mapping[str, Any]) -> tuple[int, int]:
        """Apply one hedge-execution fact to the inventory projection."""
        delta = int(hedge["filled_qty"]) * (1 if hedge["side"] == "buy" else -1)
        return quoted_position, hedge_position + delta

    def _order_fill_errors(
        self,
        order_rows: tuple[Mapping[str, Any], ...],
        fill_rows: tuple[Mapping[str, Any], ...],
    ) -> list[str]:
        """Reconcile immutable fill facts to each declared order lifecycle."""
        orders = {row["order_id"]: row for row in order_rows}
        fills_by_order: dict[str, list[Mapping[str, Any]]] = {}
        for row in fill_rows:
            if row["order_id"] in orders:
                fills_by_order.setdefault(row["order_id"], []).append(row)

        errors: list[str] = []
        for order_id, order in orders.items():
            # Hedge-leg executions are represented by the separate
            # ``hedge_executions`` contract, not by ``fills``. Quoted-leg EOD
            # orders still enter this check when they have a fill row.
            if order["order_role"] == "hedge" and order_id not in fills_by_order:
                continue
            requested_qty = int(order["requested_qty"])
            if requested_qty <= 0:
                errors.append(f"order_requested_quantity:{order_id}")
                continue

            cumulative_qty = 0
            for fill in sorted(
                fills_by_order.get(order_id, ()),
                key=lambda row: (
                    _sealed_timestamp(row["fill_ts"], "fill timestamp"),
                    int(row["feed_seq"]),
                    int(row["book_seq"]),
                    row["row_id"],
                ),
            ):
                fill_qty = int(fill["fill_qty"])
                if fill_qty <= 0:
                    errors.append(f"fill_quantity:{fill['fill_id']}")
                    continue
                cumulative_qty += fill_qty
                if int(fill["cumulative_fill_qty"]) != cumulative_qty:
                    errors.append(f"fill_cumulative_progression:{fill['fill_id']}")
                if cumulative_qty > requested_qty:
                    errors.append(f"order_fill_quantity_exceeds_requested:{order_id}")

            final_status = order["final_status"]
            allowed_statuses = (
                frozenset({"filled"})
                if cumulative_qty == requested_qty
                else frozenset({"cancelled", "expired", "rejected", "failed"})
                if cumulative_qty == 0
                else frozenset({"partial", "cancelled", "expired", "failed"})
            )
            if final_status not in allowed_statuses:
                errors.append(f"order_fill_final_status:{order_id}")
        return errors

    def _load_validated_rows(
        self, tables: tuple[str, ...]
    ) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], list[str]]:
        """Load every durable row and validate its persisted wire representation."""
        rows: dict[str, tuple[Mapping[str, Any], ...]] = {}
        errors: list[str] = []
        for table in tables:
            values: list[Mapping[str, Any]] = []
            path = self.run_dir / "tables" / f"{table}.jsonl"
            try:
                with path.open("rb") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            errors.append(f"sealed_schema_json:{table}:{line_number}")
                            continue
                        row_errors = self._sealed_row_errors(table, row, line_number)
                        errors.extend(row_errors)
                        if not row_errors:
                            values.append(row)
            except OSError:
                errors.append(f"sealed_schema_unreadable:{table}")
            rows[table] = tuple(values)
        return rows, errors

    def _sealed_row_errors(self, table: str, row: object, line_number: int) -> list[str]:
        """Return fail-closed schema errors for one JSONL row read at sealing."""
        prefix = f"sealed_schema:{table}:{line_number}"
        if not isinstance(row, Mapping):
            return [f"{prefix}:row_not_mapping"]

        errors: list[str] = []
        expected_fields = _IDENTITY_FIELDS | _REQUIRED_FIELDS[table]
        missing = expected_fields - set(row)
        unexpected = set(row) - expected_fields
        if missing:
            errors.append(f"{prefix}:missing_fields:{','.join(sorted(missing))}")
        if unexpected:
            errors.append(f"{prefix}:unexpected_fields:{','.join(sorted(unexpected))}")
        if missing:
            return errors

        identity = {
            "run_id": self.run_id,
            "schema_version": RESEARCH_TELEMETRY_SCHEMA_VERSION,
            "table": table,
            "session_date": self.session_date.isoformat(),
            "pair_id": self.hedge_pair.pair_id,
            "quoted_product": self.hedge_pair.quoted_product,
            "hedge_product": self.hedge_pair.hedge_product,
            "hedge_mapping_id": self.hedge_pair.hedge_mapping_id,
            "hedge_mapping_version": self.hedge_pair.hedge_mapping_version,
        }
        for field_name, expected in identity.items():
            if row[field_name] != expected:
                errors.append(f"{prefix}:identity:{field_name}")
        row_id = row["row_id"]
        row_prefix = f"{self.run_id}:{table}:"
        if not isinstance(row_id, str) or not row_id.startswith(row_prefix) or len(row_id) == len(row_prefix):
            errors.append(f"{prefix}:identity:row_id")

        parsed = dict(row)
        try:
            for field_name in _timestamp_fields(table):
                value = parsed[field_name]
                parsed[field_name] = None if value is None else _sealed_timestamp(value, field_name)
            self._validate_row(table, parsed)
            _json_bytes(row)
        except FoundationContractError as exc:
            errors.append(f"{prefix}:validation:{exc}")
        return errors

    def _ensure_open(self) -> None:
        if self._finalized is not None:
            raise FoundationContractError("research telemetry run is finalized and immutable")

    @staticmethod
    def _append(path: Path, row: Mapping[str, Any]) -> None:
        payload = _json_bytes(row) + b"\n"
        with path.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


def _timestamp_fields(table: str) -> tuple[str, ...]:
    return {
        "decisions": ("exchange_ts", "recv_ts", "dec_ts"),
        "book_events": ("exchange_ts", "recv_ts"),
        "book_snapshots": ("exchange_ts", "recv_ts"),
        "orders": ("submit_ts", "timeout_ts", "cancel_ts"),
        "fills": ("fill_ts",),
        "hedge_executions": ("submit_ts", "completion_ts", "deadline_ts"),
        "trigger_evaluations": ("eval_ts",),
        "signal_snapshots": ("available_at",),
        "outcome_pnl": ("start_ts", "end_ts"),
        "inventory_series": ("ts",),
        "label_outcomes": ("anchor_ts", "feature_cutoff_ts", "outcome_end_ts", "label_finalised_at"),
    }[table]


def _snapshot_midpoint(row: Mapping[str, Any]) -> float:
    """Return the top-of-book midpoint from one validated research snapshot."""
    try:
        levels = row["top_k_levels"]
        bid = float(levels["bids"][0]["price"])
        ask = float(levels["asks"][0]["price"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FoundationContractError("research book snapshot lacks a usable top-of-book midpoint") from exc
    midpoint = (bid + ask) / 2.0
    if not math.isfinite(midpoint):
        raise FoundationContractError("research book snapshot midpoint must be finite")
    return midpoint


def _require_aware_or_null(value: object, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FoundationContractError(f"{field_name} must be a timezone-aware datetime or None")


def _sealed_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise FoundationContractError(f"{field_name} must be a sealed ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FoundationContractError(f"{field_name} must be a sealed ISO-8601 string") from exc
    _require_aware_or_null(parsed, field_name)
    return parsed


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        _require_aware_or_null(value, "research telemetry datetime")
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise FoundationContractError("research telemetry floats must be finite")
        return value
    raise FoundationContractError(f"research telemetry value is not canonicalizable: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FoundationContractError("research telemetry values must be canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _signal_set_hash(snapshot_ids: list[str]) -> str:
    if any(not isinstance(snapshot_id, str) or not snapshot_id for snapshot_id in snapshot_ids):
        raise FoundationContractError("research signal snapshot identities must be non-empty strings")
    return _sha256_bytes(_json_bytes(sorted(snapshot_ids)))


__all__ = [
    "RESEARCH_MANIFEST_SCHEMA_VERSION",
    "RESEARCH_S0_TABLES",
    "RESEARCH_TELEMETRY_SCHEMA_VERSION",
    "S0_SEMANTIC_COMPLIANCE_VERSION",
    "ResearchTelemetryEmitter",
    "ResearchTelemetryResult",
]
