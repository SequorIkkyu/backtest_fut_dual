"""Field-level research telemetry contract tests."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from common.foundation_contracts import FoundationContractError, HedgeMappingSpec
from common.research_telemetry import ResearchTelemetryEmitter
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def _rows(fixture):
    now = BASE_TS + timedelta(milliseconds=5)
    end = now + timedelta(seconds=3)
    signal_set_hash = "sha256:" + hashlib.sha256(
        json.dumps(["signal-snapshot-1"], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "decisions": {
            "decision_id": "decision-1",
            "exchange_ts": BASE_TS,
            "recv_ts": now,
            "dec_ts": now,
            "feed_seq": 3,
            "quoted_book_seq": 1,
            "hedge_book_seq": 1,
            "quoted_snapshot_id": "quoted-1",
            "hedge_snapshot_id": "hedge-1",
            "side": "buy",
            "action": "quote",
            "quote_price": 78000.0,
            "size": 1,
            "quote_age_ms": 3.0,
            "queue_ahead": 2,
            "reservation_price": 78002.5,
            "skew": 0.0,
            "inventory_q": 0,
            "inventory_h": 0,
            "residual_risk": 0.0,
            "cap_state": "within_cap",
            "capacity_reserved": 1,
            "signal_set_hash": signal_set_hash,
            "block_reason": None,
            "cancel_reason": None,
            "trigger_priority": None,
            "hysteresis_state": None,
        },
        "book_events": {
            "product": fixture.quoted_spec.product,
            "feed_seq": 1,
            "book_seq": 1,
            "exchange_ts": BASE_TS,
            "recv_ts": BASE_TS + timedelta(milliseconds=2),
            "level": 1,
            "side": "bid",
            "price": 78000.0,
            "event_type": "modify",
            "qty_delta": 2,
            "displayed_qty_after": 2,
            "ofi_delta": 2.0,
        },
        "book_snapshots": {
            "snapshot_id": "quoted-1",
            "decision_id": "decision-1",
            "product": fixture.quoted_spec.product,
            "feed_seq": 1,
            "book_seq": 1,
            "exchange_ts": BASE_TS,
            "recv_ts": BASE_TS + timedelta(milliseconds=2),
            "snapshot_reason": "decision",
            "top_k_levels": {"bids": [{"price": 78000.0, "quantity": 2}], "asks": [{"price": 78005.0, "quantity": 2}]},
            "book_hash": "sha256:quoted",
        },
        "orders": {
            "order_id": "maker-1",
            "decision_id": "decision-1",
            "product": fixture.quoted_spec.product,
            "side": "buy",
            "order_role": "maker",
            "submit_ts": now,
            "timeout_ts": None,
            "cancel_ts": None,
            "price": 78000.0,
            "requested_qty": 1,
            "reserved_capacity": 1,
            "queue_ahead_submit": 2,
            "final_status": "filled",
            "cancel_reason": None,
            "expiry_reason": None,
        },
        "fills": {
            "fill_id": "fill-1",
            "order_id": "maker-1",
            "decision_id": "decision-1",
            "product": fixture.quoted_spec.product,
            "side": "buy",
            "fill_ts": now + timedelta(seconds=1),
            "feed_seq": 3,
            "book_seq": 1,
            "fill_price": 78000.0,
            "fill_qty": 1,
            "cumulative_fill_qty": 1,
            "queue_ahead_fill": 0,
            "liquidity_role": "maker",
            "fee_rebate": 0.1,
        },
        "hedge_executions": {
            "hedge_id": "hedge-1",
            "decision_id": "decision-1",
            "trigger_id": None,
            "product": fixture.hedge_spec.product,
            "side": "sell",
            "submit_ts": now,
            "completion_ts": now + timedelta(seconds=2),
            "trigger_class": "risk_cap",
            "target_before": 0,
            "target_after": -1,
            "requested_qty": 1,
            "filled_qty": 1,
            "depth_levels_consumed": 1,
            "vwap": 77980.0,
            "hedge_touch": 77980.0,
            "mid_at_decision": 77982.5,
            "cost_vs_mid": 2.5,
            "basis_at_fill": 0.0,
            "residual_risk_after": 0.0,
            "retry_count": 0,
            "deadline_ts": None,
            "disposition": "filled",
        },
        "trigger_evaluations": {
            "trigger_id": "trigger-1",
            "decision_id": "decision-1",
            "eval_ts": now,
            "feed_seq": 3,
            "quoted_book_seq": 1,
            "hedge_book_seq": 1,
            "trigger_class": "risk_cap",
            "inputs": {"residual_risk": 1.0},
            "fired": True,
            "target": -1,
            "reason": "cap",
            "hysteresis_state": "armed",
            "cooldown_ms": 0,
        },
        "signal_snapshots": {
            "signal_id": "inventory-signal",
            "signal_snapshot_id": "signal-snapshot-1",
            "decision_id": "decision-1",
            "model_version": "1",
            "feature_version": "1",
            "source": "fixture",
            "score": 0.25,
            "regime": "normal",
            "calibration_bucket": "base",
            "available_at": now,
            "age_ms": 0,
            "feature_snapshot_hash": "sha256:feature",
            "feature_coverage": 1.0,
        },
        "outcome_pnl": {
            "episode_id": None,
            "start_ts": now,
            "end_ts": end,
            "start_decision_id": "decision-1",
            "end_disposition": "flat",
            "maker_capture": 0.0,
            "quoted_leg_price_pnl": 0.0,
            "hedge_leg_price_pnl": 0.0,
            "hedge_execution_shortfall": 2.5,
            "fees_rebates": 0.1,
            "residual_basis_attribution": 0.0,
            "episode_total": -2.4,
            "inventory_time": 1.0,
            "route_transitions": "quote->fill->inventory->hedge",
            "eod_result": "flat",
            "reconciliation_residual": 0.0,
        },
        "inventory_series": {
            "ts": now,
            "feed_seq": 3,
            "quoted_book_seq": 1,
            "hedge_book_seq": 1,
            "event_source": "decision",
            "decision_id": "decision-1",
            "order_id": None,
            "fill_id": None,
            "hedge_id": None,
            "q": 0,
            "h": 0,
            "beta_t": 1.0,
            "basis": 0.0,
            "residual_risk": 0.0,
            "exposure_risk_scaled": 0.0,
        },
    }


def _hedge_mapping(fixture) -> HedgeMappingSpec:
    return HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0)


def _label_outcome(rows):
    return {
        "label_row_id": "label-1",
        "label_id": "maker-fill-markout",
        "label_version": "1",
        "decision_id": "decision-1",
        "order_id": "maker-1",
        "fill_id": "fill-1",
        "episode_id": None,
        "anchor_ts": rows["fills"]["fill_ts"],
        "feature_cutoff_ts": rows["fills"]["fill_ts"],
        "outcome_end_ts": rows["outcome_pnl"]["end_ts"],
        "label_finalised_at": rows["outcome_pnl"]["end_ts"] + timedelta(seconds=1),
        "horizon_type": "time",
        "horizon_value": 2.0,
        "side": "buy",
        "quote_price": 78000.0,
        "requested_qty": 1,
        "fill_fraction": 1.0,
        "terminal_event": "full_fill",
        "censor_reason": None,
        "value": 1.0,
        "value_unit": "ticks",
        "threshold": None,
        "quantile": None,
        "allocation_version": "1",
        "replay_version": "1",
        "policy_id": "fixture-policy",
        "construction_hash": "sha256:construction",
        "source_telemetry_hash": "sha256:source",
    }


def _emit_rows(emitter, rows, fixture) -> None:
    for table, fields in rows.items():
        emitter.emit(table, table, fields)
        if table == "book_snapshots":
            hedge = dict(fields)
            hedge.update(
                {
                    "snapshot_id": "hedge-1",
                    "product": fixture.hedge_spec.product,
                    "book_hash": "sha256:hedge",
                }
            )
            emitter.emit(table, "hedge-1", hedge)
        elif table == "inventory_series":
            active = dict(fields)
            active.update(
                {
                    "ts": rows["fills"]["fill_ts"],
                    "event_source": "fill",
                    "order_id": "maker-1",
                    "fill_id": "fill-1",
                    "q": 1,
                    "residual_risk": 1.0,
                    "exposure_risk_scaled": 1.0,
                }
            )
            emitter.emit(table, "fill-state", active)
            hedged = dict(active)
            hedged.update(
                {
                    "ts": rows["hedge_executions"]["completion_ts"],
                    "event_source": "hedge",
                    "order_id": None,
                    "fill_id": None,
                    "hedge_id": "hedge-1",
                    "h": -1,
                    "residual_risk": 0.0,
                    "exposure_risk_scaled": 0.0,
                }
            )
            emitter.emit(table, "hedge-state", hedged)
            terminal = dict(fields)
            terminal.update(
                {
                    "ts": rows["outcome_pnl"]["end_ts"],
                    "event_source": "eod",
                    "decision_id": None,
                    "order_id": None,
                    "fill_id": None,
                    "hedge_id": None,
                    "q": 1,
                    "h": -1,
                }
            )
            emitter.emit(table, "eod", terminal)


def test_research_telemetry_requires_all_registered_s0_fields_and_tables():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary),
            "research-schema-run",
            _hedge_mapping(fixture),
            date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        _emit_rows(emitter, rows, fixture)
        result = emitter.finalize()

    assert result.eligible
    assert not result.errors


def test_research_telemetry_rejects_missing_fields_and_unregistered_signals():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(Path(temporary), "research-schema-invalid", _hedge_mapping(fixture), date(2025, 1, 2))
        invalid = _rows(fixture)["decisions"]
        invalid.pop("action")
        try:
            emitter.emit("decisions", "missing-action", invalid)
        except FoundationContractError as exc:
            assert "missing required decisions fields" in str(exc)
        else:
            raise AssertionError("research decisions must include every declared field")

        signal = _rows(fixture)["signal_snapshots"]
        try:
            emitter.emit("signal_snapshots", "unregistered", signal)
        except FoundationContractError as exc:
            assert "not registered" in str(exc)
        else:
            raise AssertionError("research telemetry must reject unregistered consumed signals")


def test_research_telemetry_rejects_declared_type_and_enum_violations():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(Path(temporary), "research-schema-semantic", _hedge_mapping(fixture), date(2025, 1, 2))
        invalid_type = _rows(fixture)["decisions"]
        invalid_type["feed_seq"] = "not-an-integer"
        try:
            emitter.emit("decisions", "bad-type", invalid_type)
        except FoundationContractError as exc:
            assert "feed_seq must be an integer" in str(exc)
        else:
            raise AssertionError("research telemetry must reject invalid declared types")

        invalid_enum = _rows(fixture)["decisions"]
        invalid_enum["action"] = "fabricated_action"
        try:
            emitter.emit("decisions", "bad-enum", invalid_enum)
        except FoundationContractError as exc:
            assert "action must be one of" in str(exc)
        else:
            raise AssertionError("research telemetry must reject invalid declared enums")


def test_research_telemetry_revalidates_persisted_rows_before_sealing():
    """A post-emission enum mutation must not be sealed as eligible research evidence."""
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-sealed-row", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        _emit_rows(emitter, _rows(fixture), fixture)

        decisions_path = emitter.run_dir / "tables" / "decisions.jsonl"
        decision = json.loads(decisions_path.read_text())
        decision["action"] = "fabricated_action"
        decisions_path.write_text(json.dumps(decision, sort_keys=True, separators=(",", ":")) + "\n")

        result = emitter.finalize()

    assert not result.eligible
    assert any(
        error.startswith("sealed_schema:decisions:1:validation:") and "action must be one of" in error
        for error in result.errors
    )


def test_research_telemetry_reconciles_order_fill_quantities_and_final_status():
    """Research sealing reconstructs per-order fills before accepting final status."""
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-order-fill", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["fills"].update({"fill_qty": 2, "cumulative_fill_qty": 2})
        rows["hedge_executions"].update({"target_after": -2, "requested_qty": 2, "filled_qty": 2})
        _emit_rows(emitter, rows, fixture)

        inventory_path = emitter.run_dir / "tables" / "inventory_series.jsonl"
        inventory = [json.loads(line) for line in inventory_path.read_text().splitlines()]
        for row in inventory:
            if row["row_id"].endswith(":fill-state"):
                row.update({"q": 2, "residual_risk": 2.0, "exposure_risk_scaled": 2.0})
            elif row["row_id"].endswith(":hedge-state"):
                row.update({"q": 2, "h": -2})
            elif row["row_id"].endswith(":eod"):
                row.update({"q": 2, "h": -2})
        inventory_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in inventory))

        result = emitter.finalize()

    assert not result.eligible
    assert "order_fill_quantity_exceeds_requested:maker-1" in result.errors
    assert "order_fill_final_status:maker-1" in result.errors


def test_research_telemetry_rejects_filled_order_reported_as_cancelled():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-filled-cancelled", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["orders"]["final_status"] = "cancelled"
        _emit_rows(emitter, rows, fixture)
        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == ("order_fill_final_status:maker-1",)


def test_research_telemetry_rejects_nonprogressing_order_fill_cumulative_quantity():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-fill-cumulative", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["orders"].update({"requested_qty": 3, "final_status": "partial"})
        _emit_rows(emitter, rows, fixture)

        fills_path = emitter.run_dir / "tables" / "fills.jsonl"
        fills = [json.loads(line) for line in fills_path.read_text().splitlines()]
        second_fill = dict(fills[0])
        second_fill.update(
            {
                "row_id": f"{emitter.run_id}:fills:z-fill-2",
                "fill_id": "fill-2",
                "cumulative_fill_qty": 1,
            }
        )
        fills.append(second_fill)
        fills_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in fills))

        result = emitter.finalize()

    assert not result.eligible
    assert "fill_cumulative_progression:fill-2" in result.errors


def test_research_telemetry_rejects_non_reconciling_outcome_waterfall_at_finalization():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-outcome", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["outcome_pnl"]["episode_total"] = 99.0
        _emit_rows(emitter, rows, fixture)
        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == ("outcome_waterfall_total:research-schema-outcome:outcome_pnl:outcome_pnl",)


def test_research_telemetry_rejects_hedge_target_that_conflicts_with_its_fired_trigger():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-trigger", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["hedge_executions"]["trigger_id"] = "trigger-1"
        rows["hedge_executions"]["target_after"] = 0
        _emit_rows(emitter, rows, fixture)
        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == ("hedge_trigger_target:hedge-1",)


def test_research_telemetry_rejects_round3_snapshot_queue_and_outcome_counterexamples():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-round3", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["book_snapshots"]["decision_id"] = None
        rows["orders"]["queue_ahead_submit"] = None
        rows["outcome_pnl"].update({"inventory_time": 0.0, "route_transitions": "no_trade"})
        _emit_rows(emitter, rows, fixture)
        result = emitter.finalize()

    assert not result.eligible
    assert "decision_snapshot_missing_decision:quoted-1" in result.errors
    assert "decision_snapshot_link:decision-1:quoted_snapshot_id" in result.errors
    assert "maker_queue_ahead_missing:maker-1" in result.errors
    assert "outcome_false_no_trade:research-schema-round3:outcome_pnl:outcome_pnl" in result.errors


def test_research_telemetry_rejects_false_inventory_duration_and_signal_membership():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-round4", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["outcome_pnl"]["inventory_time"] = 2.0
        rows["outcome_pnl"]["route_transitions"] = "quote->fill->inventory->eod->hedge"
        rows["decisions"]["signal_set_hash"] = "sha256:" + hashlib.sha256(b'["missing-snapshot"]').hexdigest()
        _emit_rows(emitter, rows, fixture)
        result = emitter.finalize()

    assert not result.eligible
    assert "outcome_inventory_duration:research-schema-round4:outcome_pnl:outcome_pnl" in result.errors
    assert "outcome_route_eod_order:research-schema-round4:outcome_pnl:outcome_pnl" in result.errors
    assert "outcome_false_eod_route:research-schema-round4:outcome_pnl:outcome_pnl" in result.errors
    assert "signal_set_membership:decision-1" in result.errors


def test_research_telemetry_rejects_maker_fill_with_forged_zero_inventory_projection():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-inventory-reconciliation", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["hedge_executions"]["filled_qty"] = 0
        rows["outcome_pnl"].update({"inventory_time": 0.0, "route_transitions": "quote->fill"})
        _emit_rows(emitter, rows, fixture)

        inventory_path = emitter.run_dir / "tables" / "inventory_series.jsonl"
        forged = [json.loads(line) for line in inventory_path.read_text().splitlines()]
        for row in forged:
            row.update({"q": 0, "h": 0, "beta_t": 2.0, "residual_risk": 0.0, "exposure_risk_scaled": 0.0})
        inventory_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in forged))
        result = emitter.finalize()

    assert not result.eligible
    assert "inventory_position_reconciliation:research-schema-inventory-reconciliation:inventory_series:fill-state" in result.errors
    assert "inventory_beta_mapping:research-schema-inventory-reconciliation:inventory_series:fill-state" in result.errors
    assert "inventory_exposure_reconciliation:research-schema-inventory-reconciliation:inventory_series:fill-state" in result.errors


def test_research_telemetry_rejects_duplicate_singleton_business_ids_at_seal_time():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-duplicate-execution-facts", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        _emit_rows(emitter, _rows(fixture), fixture)

        for table, changes in (
            ("decisions", {"reservation_price": 1.0}),
            ("book_snapshots", {"book_hash": "sha256:forged"}),
            ("fills", {"fill_price": 1.0, "fee_rebate": -999.0}),
            ("hedge_executions", {"vwap": 1.0}),
            ("trigger_evaluations", {"reason": "forged"}),
        ):
            table_path = emitter.run_dir / "tables" / f"{table}.jsonl"
            persisted = [json.loads(line) for line in table_path.read_text().splitlines()]
            forged = dict(persisted[0])
            forged.update({"row_id": f"{emitter.run_id}:{table}:forged-duplicate", **changes})
            persisted.append(forged)
            table_path.write_text(
                "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in persisted)
            )

        result = emitter.finalize()

    assert not result.eligible
    assert set(result.errors) == {
        "duplicate_decision_id:decision-1",
        "duplicate_snapshot_id:quoted-1",
        "duplicate_fill_id:fill-1",
        "duplicate_hedge_id:hedge-1",
        "duplicate_trigger_id:trigger-1",
    }


def test_research_telemetry_requires_exactly_one_aggregate_outcome_at_seal_time():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-outcome-aggregate", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        _emit_rows(emitter, _rows(fixture), fixture)

        outcome_path = emitter.run_dir / "tables" / "outcome_pnl.jsonl"
        persisted = [json.loads(line) for line in outcome_path.read_text().splitlines()]
        duplicate = dict(persisted[0])
        duplicate["row_id"] = f"{emitter.run_id}:outcome_pnl:duplicate-aggregate"
        persisted.append(duplicate)
        outcome_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in persisted))

        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == ("outcome_aggregate_cardinality:2",)


def test_research_telemetry_fails_closed_when_outcome_rows_are_unavailable():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-outcome-unavailable", _hedge_mapping(fixture), date(2025, 1, 2),
        )

        errors = emitter._cross_table_errors({})

    assert errors == ["outcome_aggregate_cardinality:0"]


def test_research_telemetry_requires_an_aggregate_outcome_at_seal_time():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-outcome-no-aggregate", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        rows["outcome_pnl"]["episode_id"] = "episode-1"
        _emit_rows(emitter, rows, fixture)

        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == ("outcome_aggregate_cardinality:0",)


def test_research_telemetry_rejects_duplicate_outcome_episode_ids_at_seal_time():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-outcome-episodes", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        _emit_rows(emitter, _rows(fixture), fixture)

        outcome_path = emitter.run_dir / "tables" / "outcome_pnl.jsonl"
        persisted = [json.loads(line) for line in outcome_path.read_text().splitlines()]
        episode = dict(persisted[0])
        episode.update({"row_id": f"{emitter.run_id}:outcome_pnl:episode-1", "episode_id": "episode-1"})
        duplicate = dict(episode)
        duplicate["row_id"] = f"{emitter.run_id}:outcome_pnl:episode-1-duplicate"
        persisted.extend((episode, duplicate))
        outcome_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in persisted))

        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == ("duplicate_episode_id:episode-1",)


def test_research_telemetry_seals_emitted_label_outcomes_in_the_manifest():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-label-manifest", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        _emit_rows(emitter, rows, fixture)
        emitter.emit("label_outcomes", "label-1", _label_outcome(rows))

        result = emitter.finalize()
        manifest = json.loads((emitter.run_dir / "meta" / "research_manifest.json").read_text())

    assert result.eligible
    assert manifest["tables"]["label_outcomes"].startswith("sha256:")


def test_research_telemetry_revalidates_persisted_label_outcomes_before_sealing():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-label-sealed-row", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        _emit_rows(emitter, rows, fixture)
        emitter.emit("label_outcomes", "label-1", _label_outcome(rows))

        label_path = emitter.run_dir / "tables" / "label_outcomes.jsonl"
        persisted = [json.loads(line) for line in label_path.read_text().splitlines()]
        persisted[0]["terminal_event"] = "forged"
        label_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in persisted))

        result = emitter.finalize()

    assert not result.eligible
    assert any(error.startswith("sealed_schema:label_outcomes:1:validation:") for error in result.errors)


def test_research_telemetry_rejects_forged_inventory_residual_risk_magnitude():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-residual-risk", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        _emit_rows(emitter, _rows(fixture), fixture)

        inventory_path = emitter.run_dir / "tables" / "inventory_series.jsonl"
        inventory = [json.loads(line) for line in inventory_path.read_text().splitlines()]
        fill_state = next(row for row in inventory if row["row_id"].endswith(":fill-state"))
        fill_state["residual_risk"] = 999.0
        inventory_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in inventory))

        result = emitter.finalize()

    assert not result.eligible
    assert result.errors == (
        "inventory_residual_reconciliation:research-schema-residual-risk:inventory_series:fill-state",
    )


def test_research_telemetry_rejects_replayed_fill_and_hedge_inventory_transitions():
    fixture = make_dual_book_fixture()
    with TemporaryDirectory() as temporary:
        emitter = ResearchTelemetryEmitter(
            Path(temporary), "research-schema-replayed-inventory", _hedge_mapping(fixture), date(2025, 1, 2),
            registered_signal_ids=frozenset({"inventory-signal"}),
        )
        rows = _rows(fixture)
        _emit_rows(emitter, rows, fixture)

        inventory_path = emitter.run_dir / "tables" / "inventory_series.jsonl"
        forged = [json.loads(line) for line in inventory_path.read_text().splitlines()]
        fill_state = next(row for row in forged if row["row_id"].endswith(":fill-state"))
        hedge_state = next(row for row in forged if row["row_id"].endswith(":hedge-state"))
        terminal_state = next(row for row in forged if row["row_id"].endswith(":eod"))

        replayed_fill = dict(fill_state)
        replayed_fill.update(
            {
                "row_id": f"{emitter.run_id}:inventory_series:fill-state-replayed",
                "q": 2,
                "residual_risk": 1.0,
                "exposure_risk_scaled": 2.0,
            }
        )
        hedge_state.update({"q": 2, "residual_risk": 1.0, "exposure_risk_scaled": 1.0})
        replayed_hedge = dict(hedge_state)
        replayed_hedge.update(
            {
                "row_id": f"{emitter.run_id}:inventory_series:hedge-state-replayed",
                "h": -2,
                "residual_risk": 0.0,
                "exposure_risk_scaled": 0.0,
            }
        )
        terminal_state.update({"q": 2, "h": -2})
        forged.extend((replayed_fill, replayed_hedge))
        inventory_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in forged))

        result = emitter.finalize()

    assert not result.eligible
    outcome_id = "research-schema-replayed-inventory:outcome_pnl:outcome_pnl"
    assert f"inventory_duplicate_fill_state:{outcome_id}:fill-1" in result.errors
    assert f"inventory_duplicate_hedge_state:{outcome_id}:hedge-1" in result.errors
